"""Matchlock VM backend — all operations via CLI subprocess (Decision 3)."""

from __future__ import annotations

import logging
import shutil

import anyio

from summon_claude.sandbox import (
    MATCHLOCK_INSTALL_HINT,
    MATCHLOCK_UPGRADE_HINT,
    SandboxBackend,
    SandboxNotAvailableError,
    VmConfig,
    VmHandle,
    validate_vm_id,
)

logger = logging.getLogger(__name__)

_MIN_VERSION = (0, 2, 9)


class MatchlockBackend(SandboxBackend):
    def __init__(self) -> None:
        cli_path = shutil.which("matchlock")
        if not cli_path:
            raise SandboxNotAvailableError(
                f"Bug hunter requires Matchlock. Install via:\n{MATCHLOCK_INSTALL_HINT}"
            )
        self._cli = cli_path
        self._version_checked = False
        # Version check deferred to create_vm (avoid blocking __init__)

    async def _run(self, *args: str) -> tuple[str, str, int]:
        """Run a matchlock CLI command and return (stdout, stderr, returncode)."""
        result = await anyio.run_process(
            [self._cli, *args],
            check=False,
        )
        return (
            result.stdout.decode().strip(),
            result.stderr.decode().strip(),
            result.returncode,
        )

    async def _check_version(self) -> None:
        """Verify matchlock CLI version >= 0.2.9."""
        if self._version_checked:
            return
        stdout, _, rc = await self._run("--version")
        if rc != 0:
            raise SandboxNotAvailableError("Failed to get matchlock version")
        # Parse version from output (e.g., "matchlock version 0.2.9")
        parts = stdout.split()
        version_str = parts[-1] if parts else ""
        try:
            version = tuple(int(x) for x in version_str.split(".")[:3])
        except (ValueError, IndexError):
            raise SandboxNotAvailableError(
                f"Cannot parse matchlock version from: {stdout!r}"
            ) from None
        if version < _MIN_VERSION:
            raise SandboxNotAvailableError(
                f"Bug hunter requires Matchlock v{'.'.join(str(x) for x in _MIN_VERSION)}+. "
                f"Found v{version_str}. Upgrade via:\n{MATCHLOCK_UPGRADE_HINT}"
            )
        self._version_checked = True

    async def create_vm(self, config: VmConfig) -> VmHandle:
        await self._check_version()

        cmd = [
            self._cli,
            "run",
            "--rm=false",
            "-d",  # detached
            "--timeout",
            "86400",
        ]

        # Network allowlist
        for domain in config.network_allowlist:
            cmd.extend(["--allow-host", domain])

        # Credential proxy
        for env_var, domain in config.secrets:
            cmd.extend(["--secret", f"{env_var}@{domain}"])

        # Workspace mount (read-only for project files)
        cmd.extend(["-v", f"{config.workspace_path}:{config.guest_workspace_path}:ro"])

        # Memory volume mount (read-write for agent memory)
        if config.memory_volume_path:
            cmd.extend(["-v", f"{config.memory_volume_path}:{config.guest_memory_path}:rw"])

        # CPU and memory
        cmd.extend(["--cpu", str(config.cpu)])
        cmd.extend(["--memory", config.memory])

        # Image
        cmd.append(config.image)

        result = await anyio.run_process(cmd, check=False)
        stdout_s = result.stdout.decode().strip()
        stderr_s = result.stderr.decode().strip()

        if result.returncode != 0:
            raise RuntimeError(f"matchlock run failed (rc={result.returncode}): {stderr_s}")

        # Parse VM ID from output
        vm_id = stdout_s.split()[-1] if stdout_s else ""
        vm_id = validate_vm_id(vm_id)

        try:
            # Run pre_install commands
            for install_cmd in config.pre_install:
                _, err, rc = await self._run("exec", vm_id, "--", "sh", "-c", install_cmd)
                if rc != 0:
                    logger.warning(
                        "pre_install command failed (rc=%d): %s — %s", rc, install_cmd, err
                    )

            # Create non-root user for Claude Code (Decision 7)
            _, useradd_err, useradd_rc = await self._run(
                "exec", vm_id, "--", "useradd", "-m", "claude"
            )
            if useradd_rc != 0:
                raise RuntimeError(f"useradd failed (rc={useradd_rc}): {useradd_err}")
        except Exception:
            logger.error("VM setup failed, destroying %s", vm_id)
            await self.destroy_vm(vm_id)
            raise

        logger.info(
            "Created VM %s (image=%s, cpu=%d, memory=%s)",
            vm_id,
            config.image,
            config.cpu,
            config.memory,
        )
        return vm_id

    async def destroy_vm(self, handle: VmHandle) -> None:
        validate_vm_id(handle)
        # matchlock kill + rm (no "stop" command — Decision 1)
        await self._run("kill", handle)
        await self._run("rm", handle)
        logger.info("Destroyed VM %s", handle)

    async def exec_in_vm(
        self,
        handle: VmHandle,
        cmd: list[str],
        *,
        pty: bool = False,
    ) -> tuple[str, str, int]:
        validate_vm_id(handle)
        args = ["exec"]
        if pty:
            args.append("-it")
        args.extend([handle, "--"])
        args.extend(cmd)
        return await self._run(*args)

    async def is_running(self, handle: VmHandle) -> bool:
        validate_vm_id(handle)
        stdout, _, rc = await self._run("list", "--running")
        if rc != 0:
            return False
        # Parse tab-separated output: ID | STATUS | IMAGE | CREATED | PID
        for line in stdout.splitlines():
            parts = line.split("\t")
            if parts and parts[0].strip() == handle:
                return True
        return False
