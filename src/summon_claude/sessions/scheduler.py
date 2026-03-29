"""Per-session job scheduler backed by asyncio tasks and cronsim."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cronsim import CronSim

if TYPE_CHECKING:
    from summon_claude.sessions.registry import SessionRegistry

logger = logging.getLogger(__name__)


@dataclass
class ScheduledJob:
    """A scheduled job in the session scheduler."""

    id: str
    cron_expr: str
    prompt: str
    recurring: bool = True
    internal: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    max_lifetime_s: int = 86400  # 24h default, 0 = no limit for internal


def explain_cron(cron_expr: str) -> tuple[str, str]:
    """Return (human_readable, next_fire_time) for a cron expression.

    CronSim uses local time (matching scheduler's ``_run_job``).
    """
    now = datetime.now().astimezone()
    try:
        sim = CronSim(cron_expr, now)
        explain = sim.explain()
    except Exception:
        logger.debug("explain_cron: CronSim failed for %r", cron_expr, exc_info=True)
        return cron_expr, "—"
    try:
        nxt = next(sim)
        next_fire = nxt.strftime("%H:%M")
    except Exception:
        logger.debug("explain_cron: no future fire for %r", cron_expr, exc_info=True)
        next_fire = "—"
    return explain, next_fire


def sanitize_for_table(text: str, max_len: int = 80) -> str:
    """Sanitize text for markdown table cells (escape pipes, strip newlines)."""
    # Strip heading markers before flattening newlines so ^ matches line starts
    text = re.sub(r"^#{1,6}\s", "", text, flags=re.MULTILINE)
    text = text.replace("\n", " ").replace("\r", "")
    # Truncate before escaping so we never split a \| escape sequence
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text.replace("|", "\\|")


class SessionScheduler:
    """Per-session job scheduler using cronsim for cron parsing.

    Powers both PM scan timers (``internal=True``) and agent-facing
    CronCreate/CronDelete/CronList MCP tools.
    """

    _MAX_AGENT_JOBS = 10
    _MIN_INTERVAL_S = 60
    _MAX_PROMPT_LENGTH = 1000

    def __init__(
        self,
        event_queue: asyncio.Queue[dict[str, Any]],
        shutdown_event: asyncio.Event,
        *,
        registry: SessionRegistry | None = None,
        session_id: str | None = None,
        resume_from_session_id: str | None = None,
    ) -> None:
        if (registry is None) != (session_id is None):
            raise ValueError("registry and session_id must both be set or both be None")
        if resume_from_session_id is not None and registry is None:
            raise ValueError("resume_from_session_id requires registry")
        self._event_queue = event_queue
        self._shutdown_event = shutdown_event
        self._jobs: dict[str, ScheduledJob] = {}
        self.on_change: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._registry = registry
        self._session_id = session_id
        self._resume_from_session_id = resume_from_session_id

    async def create(
        self,
        cron_expr: str,
        prompt: str,
        *,
        recurring: bool = True,
        internal: bool = False,
        max_lifetime_s: int = 86400,
    ) -> ScheduledJob:
        """Create and start a new scheduled job."""
        # Validate: must be exactly 5 fields (reject @reboot, @yearly, etc.)
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            msg = f"Cron expression must have exactly 5 fields, got {len(parts)}: {cron_expr!r}"
            raise ValueError(msg)

        # Validate via CronSim (raises ValueError on invalid)
        # Use local time — cron expressions are interpreted in the user's timezone
        CronSim(cron_expr, datetime.now().astimezone())

        if not internal:
            self._check_min_interval(cron_expr)

            agent_count = sum(1 for j in self._jobs.values() if not j.internal)
            if agent_count >= self._MAX_AGENT_JOBS:
                msg = f"Maximum of {self._MAX_AGENT_JOBS} agent-created jobs reached"
                raise ValueError(msg)

            # Truncate long prompts
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                prompt = prompt[: self._MAX_PROMPT_LENGTH]
                logger.warning("Cron job prompt truncated to %d chars", self._MAX_PROMPT_LENGTH)

            # Strip system-reserved prefixes anywhere in prompt to prevent spoofing.
            # Regex handles double-bracket bypass (e.g. "[[SYSTEM:" → "[SYSTEM:").
            prompt = re.sub(r"\[+(?:SYSTEM|CRON):", "", prompt, flags=re.IGNORECASE)

        job = ScheduledJob(
            id=secrets.token_hex(8),
            cron_expr=cron_expr,
            prompt=prompt,
            recurring=recurring,
            internal=internal,
            max_lifetime_s=max_lifetime_s,
            created_at=datetime.now(UTC),
        )
        job.task = asyncio.create_task(self._run_job(job))
        self._jobs[job.id] = job

        if self._registry is not None and self._session_id is not None and not internal:
            try:
                await self._registry.save_scheduled_job(
                    session_id=self._session_id,
                    job_id=job.id,
                    cron_expr=job.cron_expr,
                    prompt=job.prompt,
                    recurring=job.recurring,
                    max_lifetime_s=job.max_lifetime_s,
                    created_at=job.created_at.isoformat(),
                )
            except Exception:
                logger.warning("Failed to persist cron job %s to DB", job.id, exc_info=True)

        if self.on_change:
            await self.on_change()

        return job

    async def delete(self, job_id: str) -> bool:
        """Delete a scheduled job. Refuses to delete internal jobs."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.internal:
            msg = "Cannot delete system-created jobs"
            raise ValueError(msg)

        if job.task and not job.task.done():
            job.task.cancel()
        self._jobs.pop(job_id, None)

        if self._registry is not None and self._session_id is not None:
            try:
                await self._registry.delete_scheduled_job(self._session_id, job_id)
            except Exception:
                logger.warning("Failed to delete cron job %s from DB", job_id, exc_info=True)

        if self.on_change:
            await self.on_change()

        return True

    def list_jobs(self) -> list[ScheduledJob]:
        """Return all jobs (including internal)."""
        return list(self._jobs.values())

    def cancel_all(self) -> None:
        """Cancel all asyncio tasks AND clear the jobs dict.

        Must clear ``_jobs`` so re-registration of internal jobs after
        compaction restart doesn't encounter dead entries.
        """
        for job in self._jobs.values():
            if job.task and not job.task.done():
                job.task.cancel()
        self._jobs.clear()

    def _check_min_interval(self, cron_expr: str) -> None:
        """Ensure minimum interval between fires for agent jobs."""
        now = datetime.now().astimezone()
        it = iter(CronSim(cron_expr, now))
        try:
            first = next(it)
            second = next(it)
        except StopIteration:
            return  # One-shot or no future fires
        interval = (second - first).total_seconds()
        if interval < self._MIN_INTERVAL_S:
            msg = (
                f"Minimum interval is {self._MIN_INTERVAL_S}s, "
                f"got {interval:.0f}s for {cron_expr!r}"
            )
            raise ValueError(msg)

    def _should_persist(self, job: ScheduledJob) -> bool:
        """Check if a job should be persisted/deleted in the DB."""
        return self._registry is not None and self._session_id is not None and not job.internal

    async def _delete_job_from_db(self, job_id: str, reason: str) -> None:
        """Best-effort delete of a scheduled job from DB."""
        if self._registry is None or self._session_id is None:
            return
        try:
            await self._registry.delete_scheduled_job(
                self._session_id,
                job_id,
            )
        except Exception:
            logger.warning(
                "Failed to delete %s job %s from DB",
                reason,
                job_id,
                exc_info=True,
            )

    async def _run_job(self, job: ScheduledJob) -> None:  # noqa: PLR0912
        """Run a scheduled job, firing at each cron match."""
        try:
            while not self._shutdown_event.is_set():
                # Local time for CronSim — cron expressions are in user's timezone
                now = datetime.now().astimezone()

                # Check lifetime expiry (created_at is UTC — subtraction works
                # across timezones because both are aware)
                if job.max_lifetime_s > 0:
                    elapsed = (now - job.created_at).total_seconds()
                    if elapsed >= job.max_lifetime_s:
                        logger.info("Job %s expired after %ds", job.id, job.max_lifetime_s)
                        if self._should_persist(job):
                            await self._delete_job_from_db(job.id, "expired")
                        self._jobs.pop(job.id, None)
                        if self.on_change:
                            await self.on_change()
                        return

                # Fresh iterator from now to avoid burst-fire after busy periods
                it = iter(CronSim(job.cron_expr, now))
                try:
                    next_fire = next(it)
                except StopIteration:
                    logger.info("Job %s has no future fire times", job.id)
                    if self._should_persist(job):
                        await self._delete_job_from_db(job.id, "exhausted")
                    self._jobs.pop(job.id, None)
                    if self.on_change:
                        await self.on_change()
                    return

                delay = (next_fire - now).total_seconds()
                if delay > 0:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=delay,
                        )
                        return  # Shutdown event was set
                    except TimeoutError:
                        pass  # Timer elapsed, fire the job

                # Inject synthetic event
                timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
                prefix = f"[SYSTEM:{job.id}]" if job.internal else f"[CRON:{job.id}]"
                event: dict[str, Any] = {
                    "type": "message",
                    "_synthetic": True,
                    "text": f"{prefix} [{timestamp}] {job.prompt}",
                }

                try:
                    self._event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Queue full (size=%d), dropping synthetic event for job %s",
                        self._event_queue.qsize(),
                        job.id,
                    )

                if not job.recurring:
                    if self._should_persist(job):
                        await self._delete_job_from_db(job.id, "one-shot")
                    self._jobs.pop(job.id, None)
                    if self.on_change:
                        await self.on_change()
                    return

                # Guard against tight loop if CronSim returns past/current time
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            # Do NOT delete DB rows here — cancel_all() fires this during compaction.
            # DB rows must survive so restore_from_db() can reload them.
            return
        except Exception:
            logger.exception("Scheduler job %s failed", job.id)

    async def restore_from_db(self) -> None:  # noqa: PLR0912
        """Reload non-expired agent jobs from DB into the in-memory scheduler.

        No-op if registry is None (memory-only mode).
        Skips job IDs already in self._jobs (idempotency guard).
        """
        if self._registry is None or self._session_id is None:
            return

        # Clean up expired rows first so we don't restore them
        try:
            await self._registry.delete_expired_scheduled_jobs(self._session_id)
        except Exception:
            logger.warning("Failed to delete expired scheduled jobs from DB", exc_info=True)

        # Load surviving jobs
        try:
            rows = await self._registry.list_scheduled_jobs(self._session_id)
        except Exception:
            logger.warning("Failed to load scheduled jobs from DB", exc_info=True)
            return

        # Fallback: if no jobs found and we have a resume_from_session_id, try the old session
        if not rows and self._resume_from_session_id:
            try:
                rows = await self._registry.list_scheduled_jobs(self._resume_from_session_id)
                if rows:
                    # FK migration must have failed — try to migrate now
                    try:
                        await self._registry.migrate_scheduled_jobs(
                            self._resume_from_session_id, self._session_id
                        )
                        rows = await self._registry.list_scheduled_jobs(self._session_id)
                    except Exception:
                        # Migration failed but rows are valid — proceed
                        # with data from old session (DB deletes on
                        # expiry may miss due to session_id mismatch,
                        # cleaned by CASCADE on purge)
                        logger.warning(
                            "Fallback FK migration failed from %s, restoring from old session data",
                            self._resume_from_session_id[:8],
                            exc_info=True,
                        )
            except Exception:
                logger.warning("Failed to load scheduled jobs from resume session", exc_info=True)
                return

        restored = 0
        for row in rows:
            job_id = row["id"]
            if job_id in self._jobs:
                continue  # idempotency guard

            try:
                # Validate cron expression
                CronSim(row["cron_expr"], datetime.now().astimezone())
                # Parse created_at from ISO 8601
                created_at = datetime.fromisoformat(row["created_at"])
                job = ScheduledJob(
                    id=job_id,
                    cron_expr=row["cron_expr"],
                    prompt=row["prompt"],
                    recurring=row["recurring"],
                    internal=False,
                    max_lifetime_s=row["max_lifetime_s"],
                    created_at=created_at,
                )
                job.task = asyncio.create_task(self._run_job(job))
                self._jobs[job_id] = job
                restored += 1
            except Exception:
                logger.warning(
                    "Failed to restore cron job %s from DB — skipping and deleting corrupt row",
                    job_id,
                    exc_info=True,
                )
                try:
                    await self._registry.delete_scheduled_job(self._session_id, job_id)
                except Exception:
                    logger.warning(
                        "Failed to delete corrupt job %s from DB",
                        job_id,
                        exc_info=True,
                    )

        if restored and self.on_change:
            await self.on_change()

        if restored:
            logger.info(
                "Restored %d cron job(s) from DB for session %s",
                restored,
                self._session_id,
            )
