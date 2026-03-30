"""Debounced permission handler — batches tool approval requests and posts to Slack.

Permission check flow (handle() steps):
  0.  AskUserQuestion  → intercepted, rendered as Slack interactive UI
  0b. Write gate       → enforces read-only default; SDK deny,
                         safe-dir bypass, containment check, CWD containment
  1.  SDK deny         → always honored unconditionally
  2.  Static allowlist → _AUTO_APPROVE_TOOLS (Read, Grep, Glob, …)
  2b. GitHub deny-list → _GITHUB_MCP_REQUIRE_APPROVAL always sent to Slack
  2c. GitHub allowlist → exact names and get_/list_/search_ prefixes
  2d. Summon MCP       → summon-cli/summon-slack/summon-canvas tools
  2e. Session cache    → tools approved for the session lifetime
  2f. Arg cache        → per-argument exact-match (Bash cmd, file path, etc.)
  2g. Auto-classifier  → Sonnet classifier (only active after worktree entry)
  3.  SDK allow        → secondary, after static lists
  4.  Slack HITL       → interactive approve/deny/approve-for-session buttons
"""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from summon_claude.config import SummonConfig
from summon_claude.sessions.classifier import extract_classifier_context
from summon_claude.sessions.response import get_tool_primary_arg
from summon_claude.slack.client import sanitize_for_mrkdwn
from summon_claude.slack.router import ThreadRouter

if TYPE_CHECKING:
    from summon_claude.sessions.classifier import SummonAutoClassifier

logger = logging.getLogger(__name__)

_AUTO_APPROVE_TOOLS = frozenset(
    [
        "Read",
        "Cat",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "LSP",
        "ListFiles",
        "GetSymbolsOverview",
        "FindSymbol",
        "FindReferencingSymbols",
    ]
)

# GitHub MCP tools that are always auto-approved (read-only operations)
_GITHUB_MCP_AUTO_APPROVE = frozenset(
    [
        "mcp__github__pull_request_read",
        "mcp__github__get_file_contents",
    ]
)

# GitHub MCP tool name prefixes that are auto-approved
_GITHUB_MCP_AUTO_APPROVE_PREFIXES = (
    "mcp__github__get_",
    "mcp__github__list_",
    "mcp__github__search_",
)

# Summon's own MCP tools — always auto-approved.
# Internal tools from the session's own MCP servers (summon-cli,
# summon-slack, summon-canvas), already scoped to session permissions.
_SUMMON_MCP_AUTO_APPROVE_PREFIXES = (
    "mcp__summon-cli__",
    "mcp__summon-slack__",
    "mcp__summon-canvas__",
)

# GitHub MCP tools that ALWAYS require Slack approval — never auto-approved,
# even if SDK suggestions say "allow". Defense-in-depth against broad
# allowedTools patterns in settings.json bypassing HITL.
_GITHUB_MCP_REQUIRE_APPROVAL = frozenset(
    [
        # Destructive operations
        "mcp__github__merge_pull_request",
        "mcp__github__delete_branch",
        "mcp__github__close_pull_request",
        "mcp__github__close_issue",
        "mcp__github__update_pull_request_branch",
        "mcp__github__push_files",
        "mcp__github__create_or_update_file",
        # Visible-to-others actions (notify reviewers, trigger CI, auto-merge, etc.)
        "mcp__github__pull_request_review_write",
        "mcp__github__create_pull_request",
        "mcp__github__create_issue",
        "mcp__github__add_issue_comment",
    ]
)

# Google Workspace MCP (workspace-mcp) — read-only prefixes are auto-approved,
# everything else requires Slack approval. Prefix-based rather than enumerated
# so new write tools added by workspace-mcp are fail-closed (require approval).
_GOOGLE_MCP_PREFIX = "mcp__workspace__"
_GOOGLE_MCP_AUTO_APPROVE_PREFIXES = (
    "mcp__workspace__get_",
    "mcp__workspace__list_",
    "mcp__workspace__search_",
    "mcp__workspace__query_",
    "mcp__workspace__read_",
    "mcp__workspace__check_",
    "mcp__workspace__debug_",
    "mcp__workspace__inspect_",
)

_PERMISSION_TIMEOUT_S = 600  # 10 minutes

# Tools that write to the filesystem — gated until containment is active.
# MultiEdit is included defensively even though it's not currently in
# the codebase (harmless if unused).
_WRITE_GATED_TOOLS = frozenset(
    [
        "Write",
        "Edit",
        "str_replace_editor",  # SDK alias for Edit
        "MultiEdit",
        "NotebookEdit",
        "Bash",
    ]
)

# File-path argument keys per tool, in priority order (for safe-dir lookup).
# Matches the tuple-fallback pattern in response.py's _TOOL_PATH_KEYS.
# Bash has no reliable file path — always gate unless containment is active.
_WRITE_TOOL_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path", "path"),
    "Edit": ("file_path", "path"),
    "str_replace_editor": ("path", "file_path"),  # SDK alias for Edit
    "MultiEdit": ("file_path", "path"),
    "NotebookEdit": ("notebook_path",),
}


def _is_in_safe_dir(file_path: str, safe_dirs: list[str], project_root: Path | None) -> bool:
    """Return True if file_path resolves to within any of the safe_dirs.

    Security constraints:
    - project_root must be an absolute path; if missing or relative, returns False (fail-closed).
    - Both file_path and each safe dir are resolved via Path.resolve() before comparison
      to prevent symlink escapes.
    - project_root is used to resolve relative file paths only; it is not itself a safe dir.
    """
    if not project_root or not project_root.is_absolute():
        return False

    if not safe_dirs:
        return False

    try:
        fp = Path(file_path)
        resolved_file = (project_root / fp).resolve() if not fp.is_absolute() else fp.resolve()
    except (ValueError, OSError):
        return False

    for safe_dir in safe_dirs:
        if not safe_dir:
            continue
        try:
            resolved_safe = (project_root / safe_dir).resolve()
            if resolved_file.is_relative_to(resolved_safe):
                return True
        except (ValueError, OSError):
            continue

    return False


@dataclass
class PendingRequest:
    """A single tool use permission request waiting for user approval."""

    request_id: str
    tool_name: str
    input_data: dict[str, Any]
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False


@dataclass
class _BatchState:
    """Tracks in-flight permission batches awaiting user resolution."""

    events: dict[str, asyncio.Event] = field(default_factory=dict)
    decisions: dict[str, bool] = field(default_factory=dict)
    message_ts: dict[str, str] = field(default_factory=dict)
    # Tool names per batch — used to populate session-approve cache on approval
    tool_names: dict[str, list[str]] = field(default_factory=dict)
    # Input data per batch — used for per-argument session caching
    tool_inputs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class _AskUserState:
    """Tracks in-flight AskUserQuestion requests awaiting user answers."""

    events: dict[str, asyncio.Event] = field(default_factory=dict)
    questions: dict[str, list[dict]] = field(default_factory=dict)
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    # For "Other" free-text input: (request_id, question_index)
    pending_other: tuple[str, int] | None = None
    # For multi-select: toggled selections per question keyed by (request_id, question_idx)
    multi_selections: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    # ts of the interactive question message (for deletion on completion)
    message_ts: dict[str, str] = field(default_factory=dict)


class PermissionHandler:
    """Handles tool permission requests with debounced Slack interactive messages.

    Read-only by default: write-gated tools (Write, Edit, Bash, etc.) are
    denied until containment is active (worktree entry or CWD containment).
    Safe-dir exceptions allow configured directories to bypass the requirement.

    Safe tools (Read, Grep, Glob, WebSearch, WebFetch) are auto-approved.
    Permission messages are posted as normal messages and deleted after
    the user clicks Approve/Deny/Approve-for-session. The debounce window
    (default 2000ms, configurable) batches rapid requests into one message.
    """

    def __init__(  # noqa: PLR0913
        self,
        router: ThreadRouter,
        config: SummonConfig,
        authenticated_user_id: str,
        project_root: str = "",
        classifier: SummonAutoClassifier | None = None,
        classifier_configured: bool = False,
    ) -> None:
        self._router = router
        self._authenticated_user_id = authenticated_user_id
        self._debounce_ms = config.permission_debounce_ms

        # Write gate state
        self._project_root: Path | None = Path(project_root) if project_root else None
        self._safe_dirs: list[str] = [
            d.strip() for d in config.safe_write_dirs.split(",") if d.strip()
        ]
        self._in_containment = False
        self._containment_root: Path | None = None
        self._is_git_repo: bool = True
        self._write_access_granted = False

        # Pending requests waiting for batched approval
        self._pending: dict[str, PendingRequest] = {}
        self._pending_preamble: str = ""
        self._batch_task: asyncio.Task | None = None
        self._batch_lock = asyncio.Lock()

        # Per-batch tracking (events, decisions)
        self._batch = _BatchState()

        # Session-lifetime per-tool approval cache (bare tool name)
        self._session_approved_tools: set[str] = set()

        # Per-argument session cache: tool_name → set of approved primary args.
        # Bash: exact command strings.  File tools outside CWD: exact paths.
        self._session_approved_tool_args: dict[str, set[str]] = {}

        # AskUserQuestion tracking
        self._ask_user = _AskUserState()

        # Auto-mode classifier — starts disabled, activates on worktree entry
        # if classifier_configured is True.
        self._classifier = classifier
        self._classifier_configured = classifier_configured
        self._classifier_enabled = False
        self._context_history: deque[dict[str, Any]] = deque(maxlen=20)
        self._recent_approved: deque[str] = deque(maxlen=20)

    def record_context(
        self,
        role: str,
        content: str = "",
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
    ) -> None:
        """Record conversation context for classifier evaluation."""
        entry: dict[str, Any] = {"role": role, "content": content}
        if tool_name is not None:
            entry["tool_name"] = tool_name
        if tool_input is not None:
            entry["tool_input"] = tool_input
        self._context_history.append(entry)

    @property
    def in_worktree(self) -> bool:
        """Whether the session is in a worktree."""
        return self._in_worktree

    @property
    def classifier_enabled(self) -> bool:
        """Whether the auto-mode classifier is active."""
        return self._classifier_enabled

    def set_classifier_enabled(self, enabled: bool) -> None:
        """Toggle classifier on/off. Resets fallback counters when enabling."""
        if enabled and not self._in_worktree:
            return  # Cannot enable classifier before worktree entry
        self._classifier_enabled = enabled
        if enabled and self._classifier is not None:
            self._classifier.reset_counters()

    async def handle(  # noqa: PLR0912
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Main entry point for the can_use_tool callback."""
        # 0. Intercept AskUserQuestion — route to Slack interactive UI
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(input_data)

        # 0b. Write gate — enforce read-only default until containment is active.
        # Handles: SDK deny, safe-dir bypass, containment check, CWD containment.
        if tool_name in _WRITE_GATED_TOOLS:
            result = await self._check_write_gate(tool_name, input_data, context)
            if result is not None:
                return result

        # 1. Check SDK suggestions for deny — always honor denials unconditionally
        if _sdk_suggests_deny(context, tool_name):
            return PermissionResultDeny(message="Denied by permission rules")

        # 2. Static auto-approve list is the primary gate for allowing tools
        if tool_name in _AUTO_APPROVE_TOOLS:
            logger.debug("Auto-approving tool: %s", tool_name)
            return PermissionResultAllow()

        # 2b. Restricted GitHub MCP tools always require Slack approval —
        # checked before auto-approve so deny-list takes precedence over prefixes
        if tool_name in _GITHUB_MCP_REQUIRE_APPROVAL:
            logger.info("Restricted GitHub MCP tool requires approval: %s", tool_name)
            return await self._request_approval(tool_name, input_data, context)

        # 2c. GitHub MCP auto-approve: exact names and prefix matches
        if tool_name in _GITHUB_MCP_AUTO_APPROVE or tool_name.startswith(
            _GITHUB_MCP_AUTO_APPROVE_PREFIXES
        ):
            logger.debug("Auto-approving GitHub MCP tool: %s", tool_name)
            return PermissionResultAllow()

        # 2d. Google Workspace MCP (workspace-mcp): read-only prefixes auto-approved,
        # all write/modify/create/send/manage tools require Slack HITL approval.
        if tool_name.startswith(_GOOGLE_MCP_PREFIX):
            if tool_name.startswith(_GOOGLE_MCP_AUTO_APPROVE_PREFIXES):
                logger.debug("Auto-approving Google Workspace read tool: %s", tool_name)
                return PermissionResultAllow()
            logger.info("Google Workspace write tool requires approval: %s", tool_name)
            return await self._request_approval(tool_name, input_data, context)

        # 2e. Summon's own MCP tools — always auto-approved.
        # These are internal tools provided by the session's own MCP servers
        # (summon-cli, summon-slack, summon-canvas) and already scoped to
        # the session's permissions.
        if tool_name.startswith(_SUMMON_MCP_AUTO_APPROVE_PREFIXES):
            logger.debug("Auto-approving summon MCP tool: %s", tool_name)
            return PermissionResultAllow()

        # 2f. Session-lifetime cached approvals (defense-in-depth:
        # GitHub require-approval tools are never session-cached)
        if (
            tool_name in self._session_approved_tools
            and tool_name not in _GITHUB_MCP_REQUIRE_APPROVAL
        ):
            logger.debug("Session-approved tool: %s", tool_name)
            return PermissionResultAllow()

        # 2g. Per-argument cache — exact match on full arg (Bash command,
        # file path outside CWD).  Uses _get_cacheable_arg (not
        # get_tool_primary_arg) to avoid truncation collisions.
        # Defense-in-depth: GitHub require-approval tools excluded (same as 2f).
        cacheable_arg = _get_cacheable_arg(tool_name, input_data)
        if (
            cacheable_arg
            and tool_name not in _GITHUB_MCP_REQUIRE_APPROVAL
            and cacheable_arg in self._session_approved_tool_args.get(tool_name, set())
        ):
            logger.debug("Session-approved %s arg: %s", tool_name, cacheable_arg)
            return PermissionResultAllow()

        # 2h. Auto-mode classifier (only active after worktree entry)
        if self._classifier_enabled and self._classifier is not None and self._in_worktree:
            context_text = extract_classifier_context(self._context_history)
            classify_result = await self._classifier.classify(
                tool_name,
                input_data,
                context_text,
                recent_approvals=list(self._recent_approved),
            )
            if classify_result.decision == "allow":
                logger.info("Classifier approved %s", tool_name)
                self._recent_approved.append(tool_name)
                return PermissionResultAllow()
            if classify_result.decision == "block":
                logger.info("Classifier blocked %s: %s", tool_name, classify_result.reason)
                # Generic message — don't leak classifier reasoning to outer Claude
                return PermissionResultDeny(message="Blocked by auto-mode policy")
            if classify_result.decision == "fallback_exceeded":
                self._classifier_enabled = False
                logger.warning("Classifier fallback threshold exceeded, pausing")
                try:
                    await self._router.post_to_main(
                        ":warning: Auto-mode classifier paused (too many blocks). "
                        "Falling back to manual approval. Use `!auto on` to re-enable."
                    )
                except Exception:
                    logger.debug("Failed to post classifier fallback notice")
            # "uncertain" falls through to SDK suggestions → Slack HITL

        # Record tool call context (after classifier, before HITL — avoids
        # duplicating the pending tool in the classifier's own context window)
        self.record_context("tool_call", tool_name=tool_name, tool_input=input_data)

        # 3. Check SDK suggestions for allow — secondary, after static allowlist.
        # Defense-in-depth: write-gated tools that fell through CWD containment
        # (outside containment root or Bash) must go to HITL, not SDK allow.  This
        # prevents allowedTools config from bypassing CWD containment — same
        # principle as the GitHub deny-list overriding SDK suggestions.
        _write_gated_fallthrough = tool_name in _WRITE_GATED_TOOLS and self._write_access_granted
        if _sdk_suggests_allow(context, tool_name) and not _write_gated_fallthrough:
            return PermissionResultAllow()

        # 4. Request user approval via Slack
        logger.info("Permission required for tool: %s", tool_name)
        return await self._request_approval(tool_name, input_data, context)

    @property
    def in_containment(self) -> bool:
        """Return True if directory containment is currently active."""
        return self._in_containment

    def notify_containment_active(
        self, containment_root: Path, *, is_git_repo: bool = True
    ) -> None:
        """Activate CWD-based containment for non-git or bare-directory sessions.

        SC-04: No-op if containment is already active (anti-widening guard).
        SC-01: containment_root is resolved eagerly at call time.

        Args:
            containment_root: The directory to use as containment root.
            is_git_repo: False for non-git directories; affects denial messages.
        """
        if self._in_containment:
            logger.warning(
                "notify_containment_active called but containment already active (root=%s) — "
                "ignoring to prevent widening",
                self._containment_root,
            )
            return
        self._in_containment = True
        self._is_git_repo = is_git_repo
        self._containment_root = containment_root.resolve()
        if is_git_repo:
            logger.info("Directory containment active — root=%s", self._containment_root)
        else:
            logger.info("Directory containment active (non-git) — root=%s", self._containment_root)

    def notify_entered_worktree(self, worktree_name: str = "") -> None:
        """Called by response consumer when EnterWorktree tool use is detected.

        Worktree entry always narrows containment — the worktree directory is a
        subdirectory of the project root, so the effective write boundary shrinks.
        Defense-in-depth: logs a warning if the candidate would widen containment.

        Args:
            worktree_name: Name from the EnterWorktree input (e.g. "feature-x").
                Used to compute the worktree root for CWD containment checks.
        """
        self._in_containment = True
        if worktree_name and self._project_root:
            # Reject names with path separators or traversal components
            if "/" in worktree_name or "\\" in worktree_name or ".." in worktree_name:
                logger.warning(
                    "Suspicious worktree name rejected: %r — "
                    "containment root unchanged (all writes require HITL)",
                    worktree_name,
                )
                # Fail-closed: no CWD auto-approve, all writes go to HITL
            else:
                candidate = (self._project_root / ".claude" / "worktrees" / worktree_name).resolve()
                expected_parent = (self._project_root / ".claude" / "worktrees").resolve()
                if candidate.is_relative_to(expected_parent):
                    # Anti-widening guard: candidate must be narrower than (or equal to)
                    # the current root. Widening would expand the write-allowed surface.
                    if self._containment_root is not None:
                        current = self._containment_root
                        if candidate.is_relative_to(current) or candidate == current:
                            # Narrowing or same: safe to update
                            self._containment_root = candidate
                        else:
                            logger.warning(
                                "notify_entered_worktree: candidate %s would widen "
                                "containment root %s — keeping current root",
                                candidate,
                                current,
                            )
                    else:
                        self._containment_root = candidate
                else:
                    logger.warning(
                        "Worktree path escaped expected parent: %s — containment root unchanged",
                        candidate,
                    )
        # Activate auto-classifier if configured
        if self._classifier_configured and self._classifier is not None:
            self._classifier_enabled = True
            self._classifier.reset_counters()
            logger.info("Auto-mode classifier activated on worktree entry")

        logger.info(
            "Worktree entry detected — write gate can be unlocked (root=%s)",
            self._containment_root,
        )

    def _is_within_containment(self, file_path: str) -> bool:
        """Return True if *file_path* resolves to within the containment root.

        Symlinks in existing path components are resolved to prevent escapes.
        Non-existent components are resolved lexically (``os.path.abspath``
        semantics) — best-effort guard, not a kernel guarantee.
        Returns False (fail-closed) when the containment root is unknown.
        """
        if not self._containment_root:
            return False
        if not file_path or not file_path.strip():
            return False  # reject empty/whitespace-only paths
        try:
            fp = Path(file_path)
            resolved = (
                (self._containment_root / fp).resolve() if not fp.is_absolute() else fp.resolve()
            )
            return resolved.is_relative_to(self._containment_root)
        except (ValueError, OSError):
            return False

    async def _check_write_gate(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> PermissionResultAllow | PermissionResultDeny | None:
        """Apply write gate — called for every tool in _WRITE_GATED_TOOLS.

        Returns a PermissionResult to short-circuit handle(), or None to
        continue the normal permission flow (steps 1-4).

        Decision tree:
        1. SDK deny → always honored unconditionally
        2. Safe-dir match → Allow immediately (user configured these dirs)
        3. No containment active → Deny with guidance
        4. First write in containment → one-time gate approval (sets _write_access_granted)
        5. Gate approved, file within containment root → Allow (CWD containment)
        6. Gate approved, file outside containment or Bash → fall through to arg cache / HITL
        """
        # 1. SDK deny — always honored unconditionally (before any allow path)
        if _sdk_suggests_deny(context, tool_name):
            return PermissionResultDeny(message="Denied by permission rules")

        # 2. Safe-dir bypass: takes precedence over containment requirement
        file_path = _extract_file_path(tool_name, input_data)
        if file_path and _is_in_safe_dir(file_path, self._safe_dirs, self._project_root):
            logger.debug("Safe-dir write allowed: %s → %s", tool_name, file_path)
            return PermissionResultAllow()

        # 3. No containment active: hard deny
        if not self._in_containment:
            logger.info("Write gate: denying %s (no active containment)", tool_name)
            if self._is_git_repo:
                return PermissionResultDeny(
                    message="Write access requires a worktree. "
                    "Use EnterWorktree to create an isolated copy first."
                )
            return PermissionResultDeny(
                message="Write access requires a supported working directory. "
                "Start a session in a project directory."
            )

        # 4. First write in containment → one-time gate approval
        if not self._write_access_granted:
            logger.info("Write gate: requiring approval for %s (in containment)", tool_name)
            if not self._is_git_repo:
                # SC-02: non-git warning inline in gate approval message text
                non_git_warning = (
                    f":warning: *No version control detected.* "
                    f"Edits cannot be automatically rolled back. "
                    f"Containment root: `{self._containment_root}`. "
                    f"Consider `git init` or backups."
                )
                result = await self._request_approval(
                    tool_name, input_data, context, preamble=non_git_warning
                )
            else:
                result = await self._request_approval(tool_name, input_data, context)
            if isinstance(result, PermissionResultAllow):
                self._write_access_granted = True
            return result

        # 5. Gate approved — CWD containment for file-targeting tools
        if file_path and self._is_within_containment(file_path):
            logger.debug("Write within containment: %s → %s", tool_name, file_path)
            return PermissionResultAllow()

        # 6. Outside CWD or Bash → fall through to arg cache (step 2f) or HITL (step 4)
        return None

    async def _request_approval(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
        *,
        preamble: str = "",
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Queue a permission request and wait for user approval."""
        request_id = str(uuid.uuid4())
        req = PendingRequest(
            request_id=request_id,
            tool_name=tool_name,
            input_data=input_data,
        )

        async with self._batch_lock:
            self._pending[request_id] = req
            if preamble:
                self._pending_preamble = preamble

            # Start or reset the debounce timer
            if self._batch_task and not self._batch_task.done():
                self._batch_task.cancel()
            self._batch_task = asyncio.create_task(self._debounce_and_post())

        # Wait for this specific request to be resolved
        try:
            async with asyncio.timeout(_PERMISSION_TIMEOUT_S):
                await req.result_event.wait()
        except TimeoutError:
            logger.warning("Permission request timed out for tool %s", tool_name)
            await self._post_timeout_message()
            timeout_min = _PERMISSION_TIMEOUT_S // 60
            return PermissionResultDeny(
                message=f"Permission request timed out ({timeout_min} minutes)",
            )

        if req.approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user in Slack")

    async def _debounce_and_post(self) -> None:
        """Wait for the debounce window, then post a single batch message."""
        await asyncio.sleep(self._debounce_ms / 1000.0)

        async with self._batch_lock:
            if not self._pending:
                return
            batch = dict(self._pending)
            preamble = self._pending_preamble
            self._pending.clear()
            self._pending_preamble = ""

        batch_id = str(uuid.uuid4())
        batch_event = asyncio.Event()
        self._batch.events[batch_id] = batch_event
        self._batch.tool_names[batch_id] = [req.tool_name for req in batch.values()]
        self._batch.tool_inputs[batch_id] = [req.input_data for req in batch.values()]

        await self._post_approval_message(batch_id, batch, preamble=preamble)

        # Wait for user response
        try:
            async with asyncio.timeout(_PERMISSION_TIMEOUT_S):
                await batch_event.wait()
        except TimeoutError:
            approved = False
            msg_ts = self._batch.message_ts.pop(batch_id, None)
            if msg_ts:
                await self._router.client.delete_message(msg_ts)
        else:
            approved = self._batch.decisions.get(batch_id, False)

        # Resolve all requests in this batch
        for req in batch.values():
            req.approved = approved
            req.result_event.set()

        # Cleanup
        self._batch.events.pop(batch_id, None)
        self._batch.decisions.pop(batch_id, None)
        self._batch.message_ts.pop(batch_id, None)
        self._batch.tool_names.pop(batch_id, None)
        self._batch.tool_inputs.pop(batch_id, None)

    async def _post_approval_message(
        self, batch_id: str, batch: dict[str, PendingRequest], *, preamble: str = ""
    ) -> None:
        """Post the Slack interactive approval message for a batch of requests."""
        requests = list(batch.values())

        if len(requests) == 1:
            req = requests[0]
            summary = _format_request_summary(req)
            header_text = f"Claude wants to run:\n{summary}"
        else:
            summaries = "\n".join(
                f"{i + 1}. {_format_request_summary(r)}" for i, r in enumerate(requests)
            )
            header_text = f"Claude wants to perform {len(requests)} actions:\n{summaries}"

        if preamble:
            header_text = f"{preamble}\n\n{header_text}"

        approve_value = f"approve:{batch_id}"
        approve_session_value = f"approve_session:{batch_id}"
        deny_value = f"deny:{batch_id}"

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text},
            },
            {
                "type": "actions",
                "block_id": f"permission_{batch_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "permission_approve",
                        "value": approve_value,
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve for session",
                        },
                        "action_id": "permission_approve_session",
                        "value": approve_session_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "permission_deny",
                        "value": deny_value,
                    },
                ],
            },
        ]

        try:
            ref = await self._router.client.post_interactive(
                f"Permission required: {header_text[:100]}",
                blocks=blocks,
            )
            self._batch.message_ts[batch_id] = ref.ts
        except Exception as e:
            logger.error("Failed to post permission message: %s", e)
            # Auto-deny if we can't post
            self._batch.decisions[batch_id] = False
            if batch_id in self._batch.events:
                self._batch.events[batch_id].set()

    def _cache_session_approvals(self, batch_id: str) -> None:
        """Populate session caches for an approve_session action.

        Write-gated tools: cache per-argument (command or file path).
        GitHub require-approval tools: excluded (defense-in-depth).
        Other tools: cache bare tool name.
        """
        tool_names_list = self._batch.tool_names.get(batch_id, [])
        tool_inputs_list = self._batch.tool_inputs.get(batch_id, [])
        for i, name in enumerate(tool_names_list):
            if name in _GITHUB_MCP_REQUIRE_APPROVAL:
                continue
            if name in _WRITE_GATED_TOOLS:
                inp = tool_inputs_list[i] if i < len(tool_inputs_list) else {}
                arg = _get_cacheable_arg(name, inp)
                if arg:
                    self._session_approved_tool_args.setdefault(name, set()).add(arg)
            else:
                self._session_approved_tools.add(name)

    async def handle_action(
        self,
        value: str,
        user_id: str,
    ) -> None:
        """Handle a Slack interactive button click for permission approval/denial.

        Must be called AFTER ack() (the 3-second deadline is the caller's responsibility).
        Channel routing is handled by ``EventDispatcher.dispatch_action``.
        """
        if user_id != self._authenticated_user_id:
            logger.warning(
                "Permission action from unauthorized user %s (expected %s)",
                user_id,
                self._authenticated_user_id,
            )
            return

        is_session_approve = False
        if value.startswith("approve:"):
            batch_id = value[len("approve:") :]
            approved = True
        elif value.startswith("approve_session:"):
            batch_id = value[len("approve_session:") :]
            approved = True
            is_session_approve = True
            self._cache_session_approvals(batch_id)
        elif value.startswith("deny:"):
            batch_id = value[len("deny:") :]
            approved = False
        else:
            logger.warning("Unknown permission action value: %r", value)
            return

        self._batch.decisions[batch_id] = approved

        # Delete the interactive message (replaces ephemeral dismiss)
        msg_ts = self._batch.message_ts.pop(batch_id, None)
        if msg_ts:
            await self._router.client.delete_message(msg_ts)

        # Post a persistent confirmation to the turn thread
        # (include tool names since the interactive message is now deleted)
        tool_names = self._batch.tool_names.get(batch_id, [])
        tool_inputs = self._batch.tool_inputs.get(batch_id, [])
        tool_list = (
            _format_tool_list(tool_names, tool_inputs, is_session_approve)
            if tool_names
            else "tools"
        )
        session_suffix = " for session" if is_session_approve else ""
        status_text = ":white_check_mark: Approved" if approved else ":x: Denied"
        try:
            msg = f"{status_text}{session_suffix}: {tool_list}"
            await self._router.post_to_active_thread(msg)
        except Exception as e:
            logger.warning("Failed to post permission confirmation: %s", e)

        # Signal the waiting batch
        if batch_id in self._batch.events:
            self._batch.events[batch_id].set()

    async def _post_timeout_message(self) -> None:
        """Post a message indicating permission timed out."""
        try:
            await self._router.post_to_active_thread(
                f":hourglass: Permission request timed out after"
                f" {_PERMISSION_TIMEOUT_S // 60} minutes. Denied.",
            )
        except Exception as e:
            logger.warning("Failed to post timeout message: %s", e)

    # ------------------------------------------------------------------
    # AskUserQuestion handling
    # ------------------------------------------------------------------

    async def _handle_ask_user_question(
        self, input_data: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Render AskUserQuestion as Slack interactive buttons and wait for answers."""
        questions = input_data.get("questions", [])
        if not questions:
            return PermissionResultAllow(updated_input=input_data)

        request_id = str(uuid.uuid4())
        event = asyncio.Event()

        self._ask_user.events[request_id] = event
        self._ask_user.questions[request_id] = questions
        self._ask_user.answers[request_id] = {}
        self._ask_user.expected[request_id] = len(questions)

        blocks = _build_ask_user_blocks(request_id, questions)
        try:
            ref = await self._router.client.post_interactive(
                "Claude has a question for you",
                blocks=blocks,
            )
            self._ask_user.message_ts[request_id] = ref.ts
        except Exception as e:
            logger.error("Failed to post AskUserQuestion message: %s", e)
            self._cleanup_ask_user(request_id)
            return PermissionResultDeny(message="Failed to display question")

        try:
            async with asyncio.timeout(_PERMISSION_TIMEOUT_S):
                await event.wait()
        except TimeoutError:
            logger.warning("AskUserQuestion timed out")
            # Delete the question message on timeout
            msg_ts = self._ask_user.message_ts.get(request_id)
            if msg_ts:
                await self._router.client.delete_message(msg_ts)
            self._cleanup_ask_user(request_id)
            timeout_min = _PERMISSION_TIMEOUT_S // 60
            return PermissionResultDeny(
                message=f"Question timed out ({timeout_min} minutes)",
            )

        answers = dict(self._ask_user.answers.get(request_id, {}))
        self._cleanup_ask_user(request_id)

        return PermissionResultAllow(
            updated_input={
                "questions": questions,
                "answers": answers,
            }
        )

    async def handle_ask_user_action(
        self,
        value: str,
        user_id: str,
    ) -> None:
        """Handle a Slack button click for an AskUserQuestion option.

        Value format: ``{request_id}|{question_idx}|{option_idx_or_other_or_done}``
        """
        if user_id != self._authenticated_user_id:
            logger.warning(
                "Ask user action from unauthorized user %s (expected %s)",
                user_id,
                self._authenticated_user_id,
            )
            return

        parsed = _parse_ask_user_value(value)
        if parsed is None:
            return

        request_id, q_idx, opt_val = parsed

        if request_id not in self._ask_user.events:
            return

        questions = self._ask_user.questions.get(request_id, [])
        if q_idx >= len(questions):
            return

        question = questions[q_idx]

        if opt_val == "other":
            await self._handle_ask_other(request_id, q_idx, question)
        elif opt_val == "done":
            await self._handle_ask_done(request_id, q_idx, question)
        else:
            await self._handle_ask_option(request_id, q_idx, question, opt_val)

    async def _handle_ask_other(self, request_id: str, q_idx: int, question: dict) -> None:
        """Handle 'Other' button — set pending flag for free-text capture."""
        self._ask_user.pending_other = (request_id, q_idx)
        q_text = sanitize_for_mrkdwn(question.get("question", ""))
        await _post_quietly(
            self._router,
            f":pencil: Type your answer for: _{q_text}_",
        )

    async def _handle_ask_done(self, request_id: str, q_idx: int, question: dict) -> None:
        """Handle 'Done' button for multi-select — finalize toggled selections."""
        key = (request_id, q_idx)
        selections = self._ask_user.multi_selections.pop(key, [])
        answer = ", ".join(selections) if selections else ""
        q_text = question.get("question", "")
        header = sanitize_for_mrkdwn(question.get("header", ""))
        self._ask_user.answers[request_id][q_text] = answer
        await _post_quietly(
            self._router,
            f":white_check_mark: *{header}*: {sanitize_for_mrkdwn(answer)}",
        )
        await self._check_ask_user_complete(request_id)

    async def _handle_ask_option(
        self, request_id: str, q_idx: int, question: dict, opt_val: str
    ) -> None:
        """Handle a numbered option button click."""
        try:
            opt_idx = int(opt_val)
        except ValueError:
            return

        options = question.get("options", [])
        if opt_idx >= len(options):
            return

        label = options[opt_idx].get("label", "")
        q_text = question.get("question", "")
        header = sanitize_for_mrkdwn(question.get("header", ""))

        if question.get("multiSelect", False):
            await self._toggle_multi_select(request_id, q_idx, label, header)
        else:
            self._ask_user.answers[request_id][q_text] = label
            await _post_quietly(
                self._router,
                f":white_check_mark: *{header}*: {sanitize_for_mrkdwn(label)}",
            )
            await self._check_ask_user_complete(request_id)

    async def _toggle_multi_select(
        self, request_id: str, q_idx: int, label: str, header: str
    ) -> None:
        """Toggle a multi-select option and post feedback."""
        key = (request_id, q_idx)
        selections = self._ask_user.multi_selections.setdefault(key, [])
        safe_label = sanitize_for_mrkdwn(label)
        if label in selections:
            selections.remove(label)
            await _post_quietly(
                self._router,
                f":heavy_minus_sign: *{header}*: deselected _{safe_label}_",
            )
        else:
            selections.append(label)
            await _post_quietly(
                self._router,
                f":heavy_plus_sign: *{header}*: selected _{safe_label}_",
            )

    def has_pending_text_input(self) -> bool:
        """Return True if we're waiting for free-text input from the user (Other)."""
        return self._ask_user.pending_other is not None

    async def receive_text_input(self, text: str, *, user_id: str) -> None:
        """Receive free-text input from the user for an 'Other' answer.

        Args:
            text: The free-text answer.
            user_id: Slack user ID of the sender. Verified against session owner.
                     Required — callers must always provide identity context.
        """
        if not self._ask_user.pending_other:
            return

        if user_id != self._authenticated_user_id:
            logger.warning(
                "Free-text input from unauthorized user %s (expected %s)",
                user_id,
                self._authenticated_user_id,
            )
            return

        request_id, q_idx = self._ask_user.pending_other
        self._ask_user.pending_other = None

        questions = self._ask_user.questions.get(request_id, [])
        if q_idx >= len(questions):
            return

        question = questions[q_idx]
        question_text = question.get("question", "")
        header = question.get("header", "")

        self._ask_user.answers[request_id][question_text] = text
        safe_header = sanitize_for_mrkdwn(header)
        await _post_quietly(
            self._router,
            f":white_check_mark: *{safe_header}*: {sanitize_for_mrkdwn(text)}",
        )

        await self._check_ask_user_complete(request_id)

    async def _check_ask_user_complete(self, request_id: str) -> None:
        """If all questions for a request are answered, delete message and signal."""
        answers = self._ask_user.answers.get(request_id, {})
        expected = self._ask_user.expected.get(request_id, 0)
        if len(answers) >= expected:
            # Delete the interactive question message
            msg_ts = self._ask_user.message_ts.get(request_id)
            if msg_ts:
                await self._router.client.delete_message(msg_ts)
            event = self._ask_user.events.get(request_id)
            if event:
                event.set()

    def _cleanup_ask_user(self, request_id: str) -> None:
        """Remove all state for a completed or timed-out ask_user request."""
        self._ask_user.events.pop(request_id, None)
        questions = self._ask_user.questions.pop(request_id, [])
        self._ask_user.answers.pop(request_id, None)
        self._ask_user.expected.pop(request_id, None)
        self._ask_user.message_ts.pop(request_id, None)
        if self._ask_user.pending_other and self._ask_user.pending_other[0] == request_id:
            self._ask_user.pending_other = None
        # Clean up multi-select state for all questions in this request
        for i in range(len(questions)):
            self._ask_user.multi_selections.pop((request_id, i), None)


def _extract_file_path(tool_name: str, input_data: dict[str, Any]) -> str:
    """Extract the file path argument from a write-gated tool's input.

    Returns empty string for Bash and unknown tools (no file path to extract).
    """
    for key in _WRITE_TOOL_PATH_KEYS.get(tool_name, ()):
        path = input_data.get(key, "")
        if path:
            return path
    return ""


def _get_cacheable_arg(tool_name: str, input_data: dict[str, Any]) -> str:
    """Return the full, untruncated primary argument for session caching.

    Unlike ``get_tool_primary_arg`` (which truncates Bash commands for
    display), this returns the raw value to ensure exact-match fidelity.
    Two commands sharing the first 120 chars but differing after that
    must NOT collide in the cache.
    """
    if tool_name == "Bash":
        return input_data.get("command", "")
    return _extract_file_path(tool_name, input_data)


def _sdk_suggests_deny(context: ToolPermissionContext | None, tool_name: str) -> bool:
    """Return True if any SDK suggestion says to deny this tool."""
    if context is None:
        return False
    for suggestion in getattr(context, "suggestions", []) or []:
        if getattr(suggestion, "behavior", None) == "deny":
            logger.info("SDK suggestion: denying %s", tool_name)
            return True
    return False


def _sdk_suggests_allow(context: ToolPermissionContext | None, tool_name: str) -> bool:
    """Return True if any SDK suggestion says to allow this tool."""
    if context is None:
        return False
    for suggestion in getattr(context, "suggestions", []) or []:
        if getattr(suggestion, "behavior", None) == "allow":
            logger.info("SDK suggestion: approving %s", tool_name)
            return True
    return False


def _parse_ask_user_value(value: str) -> tuple[str, int, str] | None:
    """Parse an ask_user action value into (request_id, question_idx, opt_val)."""
    parts = value.split("|")
    if len(parts) != 3:
        logger.warning("Invalid ask_user action value: %r", value)
        return None
    request_id, q_idx_str, opt_val = parts
    try:
        q_idx = int(q_idx_str)
    except ValueError:
        return None
    return request_id, q_idx, opt_val


async def _post_quietly(router: ThreadRouter, text: str) -> None:
    """Post to the turn thread, swallowing errors."""
    try:
        await router.post_to_active_thread(text)
    except Exception as e:
        logger.warning("Failed to post ask_user feedback: %s", e)


def _build_ask_user_blocks(request_id: str, questions: list[dict]) -> list[dict]:
    """Build Slack Block Kit blocks for AskUserQuestion rendering."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":question: *Claude has a question for you*"},
        },
        {"type": "divider"},
    ]

    for i, q in enumerate(questions):
        header = q.get("header", "")
        question_text = q.get("question", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)

        # Question text (with multi-select hint)
        q_text = f"*{sanitize_for_mrkdwn(header)}*\n{sanitize_for_mrkdwn(question_text)}"
        if multi_select:
            q_text += "\n_Select multiple, then click Done_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": q_text}})

        # Option descriptions + markdown previews as context
        desc_parts = []
        for opt in options:
            label = opt.get("label", "")
            desc = opt.get("description", "")
            md_preview = opt.get("markdown", "")
            if desc:
                desc_parts.append(
                    f"\u2022 *{sanitize_for_mrkdwn(label)}*: {sanitize_for_mrkdwn(desc)}"
                )
            if md_preview:
                # Render markdown preview as a code block (monospace)
                # Escape backticks to prevent breaking out of the code block
                safe_preview = md_preview.strip().replace("`", "\u2019")
                preview_lines = safe_preview.splitlines()
                # Truncate long previews to keep Slack message manageable
                if len(preview_lines) > 8:
                    preview_lines = [*preview_lines[:8], "..."]
                preview_text = "\n".join(preview_lines)
                desc_parts.append(f"```{preview_text}```")
        if desc_parts:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "\n".join(desc_parts)}],
                }
            )

        # Option buttons
        elements = []
        for j, opt in enumerate(options):
            label = opt.get("label", f"Option {j + 1}")
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label[:75]},
                    "action_id": f"ask_user_{i}_{j}",
                    "value": f"{request_id}|{i}|{j}",
                }
            )

        # "Other" button
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Other"},
                "action_id": f"ask_user_{i}_other",
                "value": f"{request_id}|{i}|other",
            }
        )

        # "Done" button for multi-select
        if multi_select:
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Done"},
                    "style": "primary",
                    "action_id": f"ask_user_{i}_done",
                    "value": f"{request_id}|{i}|done",
                }
            )

        blocks.append(
            {
                "type": "actions",
                "block_id": f"ask_user_{request_id[:8]}_{i}",
                "elements": elements,
            }
        )

    return blocks


def _format_tool_list(
    tool_names: list[str],
    tool_inputs: list[dict[str, Any]],
    show_args: bool,
) -> str:
    """Format a human-readable tool list for confirmation messages.

    When *show_args* is True (approve-for-session), write-gated tools
    include their primary argument as a preview.  Display is truncated for
    readability; the actual cache (``_get_cacheable_arg``) stores the full
    untruncated value.
    """
    parts: list[str] = []
    for i, name in enumerate(tool_names):
        if show_args and name in _WRITE_GATED_TOOLS and i < len(tool_inputs):
            arg = get_tool_primary_arg(name, tool_inputs[i])
            if arg:
                safe_arg = sanitize_for_mrkdwn(arg, 80)
                parts.append(f"`{name}`: `{safe_arg}`")
                continue
        parts.append(f"`{name}`")
    return ", ".join(parts)


def _format_request_summary(req: PendingRequest) -> str:
    """Create a human-readable summary of a permission request."""
    tool = req.tool_name
    data = req.input_data

    arg = get_tool_primary_arg(tool, data)
    if arg:
        safe_arg = sanitize_for_mrkdwn(arg)
        return f"`{tool}`: `{safe_arg}`"

    # Generic fallback
    keys = list(data.keys())[:2]
    params = ", ".join(f"{k}={sanitize_for_mrkdwn(str(data[k]), 40)!r}" for k in keys)
    return f"`{tool}`({params})"
