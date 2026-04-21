"""Sandbox abstractions for bug hunter VM isolation."""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# VM ID format: vm-[0-9a-f]{8}
_VM_ID_RE = re.compile(r"^vm-[0-9a-f]{8}$")

# Semver pattern: MAJOR.MINOR.PATCH with optional pre-release/build
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")

# Domain validation: hostname with TLD, optional port
_DOMAIN_RE = re.compile(r"^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(:\d+)?$")

# Env var name validation: must start with uppercase letter, then uppercase/digits/underscores
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

# Session name constant — all bug hunter sessions use this name (per SEC-D-015)
BUG_HUNTER_SESSION_NAME = "bug-hunter"

# TypeAlias for VM ID strings
VmHandle = str

# Default network allowlist — always included even when project overrides are set
_DEFAULT_NETWORK_ALLOWLIST = (
    "api.anthropic.com",
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "registry.npmjs.org",
)


class SandboxNotAvailableError(RuntimeError):
    """Raised when the sandbox backend (matchlock) is not installed."""


def validate_vm_id(vm_id: str) -> str:
    """Validate a VM ID matches the expected format.

    Args:
        vm_id: A VM ID string to validate.

    Returns:
        The vm_id unchanged if valid.

    Raises:
        ValueError: If the format does not match vm-[0-9a-f]{8}.
    """
    if not _VM_ID_RE.match(vm_id):
        raise ValueError(f"Invalid VM ID {vm_id!r}: must match vm-[0-9a-f]{{8}}")
    return vm_id


def _validate_domain(domain: str) -> bool:
    """Return True if domain matches the allowed hostname format."""
    return bool(_DOMAIN_RE.match(domain))


def _validate_env_var_name(name: str) -> bool:
    """Return True if name is a valid uppercase env var name."""
    return bool(_ENV_VAR_RE.match(name))


def validate_bug_hunter_secrets(secrets: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Validate and convert a secrets dict to the VmConfig tuple format.

    Keys must be env var names matching [A-Z][A-Z0-9_]+, values must be domains.
    Rejects everything that doesn't match — catches raw secrets of any format.

    Args:
        secrets: Dict mapping env var names to domains (e.g. {"MY_TOKEN": "api.example.com"}).

    Returns:
        Tuple of (env_var, domain) pairs suitable for VmConfig.secrets.

    Raises:
        ValueError: If any key is not a valid env var name or value is not a valid domain.
    """
    validated = []
    for key, value in secrets.items():
        if not _validate_env_var_name(key):
            raise ValueError(f"Invalid env var name {key!r}: must match [A-Z][A-Z0-9_]+")
        if not _validate_domain(value):
            raise ValueError(f"Invalid domain {value!r} for {key}: must be a valid domain")
        validated.append((key, value))
    return tuple(validated)


@dataclass(frozen=True)
class VmConfig:
    """Configuration for a sandboxed VM instance.

    All sequence fields use tuples (not lists) because this dataclass is frozen.
    """

    claude_code_version: str  # REQUIRED — forces explicit pinning
    workspace_path: str  # host-side workspace path

    image: str = "node:24-slim"
    pre_install: tuple[str, ...] = ()
    guest_workspace_path: str = "/workspace"
    memory_volume_path: str | None = None
    guest_memory_path: str = field(default="", init=False)
    network_allowlist: tuple[str, ...] = ()
    secrets: tuple[tuple[str, str], ...] = ()  # ((env_var, domain), ...)
    cpu: int = 4
    memory: str = "4G"

    def __post_init__(self) -> None:
        if self.claude_code_version != "latest" and not _SEMVER_RE.match(self.claude_code_version):
            raise ValueError(
                f"claude_code_version {self.claude_code_version!r} must be 'latest' or "
                "a semver string (expected MAJOR.MINOR.PATCH)"
            )
        derived = f"{self.guest_workspace_path}/.bug-hunter-memory/"
        object.__setattr__(self, "guest_memory_path", derived)


class SandboxBackend(ABC):
    """Abstract base class for sandbox backends."""

    @abstractmethod
    async def create_vm(self, config: VmConfig) -> VmHandle:
        """Create a new VM with the given configuration.

        Returns:
            A VmHandle identifying the created VM.
        """

    @abstractmethod
    async def destroy_vm(self, handle: VmHandle) -> None:
        """Destroy a VM and release its resources."""

    @abstractmethod
    async def exec_in_vm(
        self,
        handle: VmHandle,
        cmd: list[str],
        *,
        pty: bool = False,
    ) -> tuple[str, str, int]:
        """Execute a command in the VM.

        Args:
            handle: VM handle returned by create_vm.
            cmd: Command and arguments to execute.
            pty: Whether to allocate a pseudo-terminal.

        Returns:
            Tuple of (stdout, stderr, returncode).
        """

    @abstractmethod
    async def is_running(self, handle: VmHandle) -> bool:
        """Return True if the VM is currently running."""


def create_bug_hunter_vm_config(
    *,
    workspace_path: str,
    claude_code_version: str,
    memory_volume_path: str | None = None,
    network_allowlist: tuple[str, ...] | None = None,
    secrets: dict[str, str] | None = None,
) -> VmConfig:
    """Create a VmConfig with bug-hunter-specific defaults.

    Network allowlist: project overrides are ADDITIVE to defaults.
    Secrets: validated with domain format allowlist.

    Args:
        workspace_path: Host-side path to mount as the VM workspace (read-only).
        claude_code_version: Exact Claude Code version to install (semver).
        memory_volume_path: Host-side path for the persistent memory volume (read-write).
        network_allowlist: Additional domains to allow beyond the defaults.
        secrets: Dict mapping env var names to domains for credential proxy.
            The env vars must be set in the host environment.

    Raises:
        ValueError: If domain/env-var validation fails or a required env var is not set.
    """
    # Merge network allowlist (additive)
    merged_allowlist = list(_DEFAULT_NETWORK_ALLOWLIST)
    if network_allowlist:
        for domain in network_allowlist:
            if not _validate_domain(domain):
                raise ValueError(f"Invalid domain in network allowlist: {domain!r}")
            if domain not in merged_allowlist:
                merged_allowlist.append(domain)

    # Validate and merge secrets
    default_secrets: dict[str, str] = {"ANTHROPIC_API_KEY": "api.anthropic.com"}
    if secrets:
        validated = validate_bug_hunter_secrets(secrets)
        secret_dict = dict(default_secrets)
        secret_dict.update(dict(validated))
    else:
        secret_dict = dict(default_secrets)

    # Validate env vars exist in host environment
    for env_var in secret_dict:
        if os.environ.get(env_var) is None:
            raise ValueError(
                f"Environment variable {env_var!r} required for bug hunter "
                "credential proxy but not set in host environment"
            )

    # Vertex ADC detection (SEC-D-003)
    if os.environ.get("CLAUDE_CODE_USE_VERTEX"):
        gcloud_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if gcloud_creds and "application_default_credentials" in gcloud_creds:
            logger.warning(
                "Vertex ADC appears to use user credentials (%s). "
                "For production, use a least-privilege service account "
                "with only aiplatform.endpoints.predict permission.",
                gcloud_creds,
            )

    npm_version_tag = "latest" if claude_code_version == "latest" else claude_code_version
    return VmConfig(
        image="node:24-slim",
        pre_install=(f"npm install -g @anthropic-ai/claude-code@{npm_version_tag}",),
        claude_code_version=claude_code_version,
        workspace_path=workspace_path,
        memory_volume_path=memory_volume_path,
        network_allowlist=tuple(merged_allowlist),
        secrets=tuple(secret_dict.items()),
    )
