"""MCP stdio proxy that marks all tool results as untrusted.

Usage:
    python -m summon_claude.mcp_untrusted_proxy \\
        --source "Google Workspace" \\
        -- workspace-mcp --tools gmail calendar drive

Everything after ``--`` is the downstream MCP server command + args.
The proxy forwards all JSON-RPC messages bidirectionally, wrapping
tool call results with untrusted data markers from security.py.

This module is intentionally standalone — no imports from sessions/
or slack/ packages. Only depends on security.py for mark_untrusted().
"""

import asyncio
import json
import logging
import sys
from typing import Any

from summon_claude.security import mark_untrusted

logger = logging.getLogger(__name__)

# Maximum text length per content item. Prevents a single tool result
# (e.g., a large Google Doc) from bloating the session context.
_MAX_CONTENT_CHARS = 100_000

# workspace-mcp returns an auth URL when scopes are insufficient.
# The URL targets workspace-mcp's own callback server which isn't running
# in our context. Replace with actionable CLI guidance.
_SCOPE_ERROR_NEEDLE = "ACTION REQUIRED: Google Authentication Needed"
_SCOPE_ERROR_REPLACEMENT = (
    "Google Workspace scope error: this tool requires write access "
    "that was not granted during authentication.\n"
    "The user should run: summon auth google login\n"
    "and grant write access to the relevant service when prompted."
)


def _rewrite_scope_error(text: str) -> str:
    """Replace workspace-mcp's auth-URL error with CLI guidance."""
    if _SCOPE_ERROR_NEEDLE in text:
        return _SCOPE_ERROR_REPLACEMENT
    return text


def _mark_tool_result(message: dict[str, Any], source: str) -> dict[str, Any]:
    """Wrap tool result content with untrusted data markers.

    Only modifies successful responses with a ``result.content`` array
    containing text items. Errors and non-tool responses pass through
    unchanged.
    """
    result = message.get("result")
    if not isinstance(result, dict):
        return message

    content = result.get("content")
    if not isinstance(content, list):
        return message

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            original = _rewrite_scope_error(str(item.get("text", "")))
            if len(original) > _MAX_CONTENT_CHARS:
                original = original[:_MAX_CONTENT_CHARS] + "\n[truncated by security proxy]"
            item["text"] = mark_untrusted(original, source)
        elif item_type == "resource":
            resource = item.get("resource", {})
            if isinstance(resource, dict) and "text" in resource:
                text = str(resource["text"])
                if len(text) > _MAX_CONTENT_CHARS:
                    text = text[:_MAX_CONTENT_CHARS] + "\n[truncated by security proxy]"
                resource["text"] = mark_untrusted(text, source)
        elif item_type not in ("image",):
            logger.warning("Unmarked MCP content type: %s", item_type)

    return message


async def _relay_to_child(
    parent_reader: asyncio.StreamReader,
    child_stdin: asyncio.StreamWriter,
) -> None:
    """Forward messages from SDK to downstream server."""
    while True:
        line = await parent_reader.readline()
        if not line:
            break
        child_stdin.write(line)
        await child_stdin.drain()
    child_stdin.close()


async def _relay_to_parent(
    child_stdout: asyncio.StreamReader,
    source: str,
) -> None:
    """Forward messages from downstream server to SDK, marking results."""
    while True:
        line = await child_stdout.readline()
        if not line:
            break
        parsed: Any = None
        try:
            parsed = json.loads(line)
            marked = _mark_tool_result(parsed, source)
            sys.stdout.buffer.write(json.dumps(marked).encode() + b"\n")
            sys.stdout.buffer.flush()
        except json.JSONDecodeError:
            # SECURITY: non-JSON lines are wrapped as untrusted rather than
            # passed through raw. MCP protocol is strictly JSON-RPC, so
            # non-JSON content is unexpected and should not be trusted.
            logger.debug("Non-JSON line from downstream — wrapping as untrusted")
            fallback = mark_untrusted(line.decode(errors="replace"), source)
            sys.stdout.buffer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "result": {"content": [{"type": "text", "text": fallback}]},
                    }
                ).encode()
                + b"\n"
            )
            sys.stdout.buffer.flush()
        except Exception as exc:
            # SECURITY: fail-closed — any marking failure wraps raw content.
            # parsed was assigned by json.loads before marking failed, so
            # we can extract the request id for proper SDK correlation.
            logger.warning("Failed to process downstream message: %s", exc)
            fallback = mark_untrusted(line.decode(errors="replace"), source)
            msg_id = parsed.get("id") if isinstance(parsed, dict) else None
            sys.stdout.buffer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"content": [{"type": "text", "text": fallback}]},
                    }
                ).encode()
                + b"\n"
            )
            sys.stdout.buffer.flush()


async def _relay_stderr(child_stderr: asyncio.StreamReader) -> None:
    """Forward downstream server stderr to parent stderr."""
    while True:
        line = await child_stderr.readline()
        if not line:
            break
        sys.stderr.buffer.write(line)
        sys.stderr.buffer.flush()


async def run_proxy(source: str, command: list[str]) -> int:
    """Run the MCP proxy.

    Args:
        source: Human-readable source label for marking.
        command: Downstream MCP server command + args.

    Returns:
        Exit code from the downstream server.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # All three streams are guaranteed non-None because we passed PIPE above.
    # Assign to local vars so pyright narrows past the None check.
    child_in = process.stdin
    child_out = process.stdout
    child_err = process.stderr
    if child_in is None or child_out is None or child_err is None:
        raise RuntimeError("Subprocess streams unavailable — PIPE not set up correctly")

    parent_reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(parent_reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    tasks = [
        asyncio.create_task(_relay_to_child(parent_reader, child_in)),
        asyncio.create_task(_relay_to_parent(child_out, source)),
        asyncio.create_task(_relay_stderr(child_err)),
    ]

    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()

    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    return process.returncode or 0


def main() -> None:
    """CLI entry point: parse args and run proxy."""
    args = sys.argv[1:]

    if "--" in args:
        sep = args.index("--")
        proxy_args = args[:sep]
        command = args[sep + 1 :]
    else:
        proxy_args = args
        command = []

    source = "External MCP"
    if "--source" in proxy_args:
        idx = proxy_args.index("--source")
        if idx + 1 < len(proxy_args):
            source = proxy_args[idx + 1]

    if not command:
        command = proxy_args

    if not command:
        sys.stderr.write(
            "Usage: python -m summon_claude.mcp_untrusted_proxy "
            "--source 'Label' -- <server-command> [args...]\n"
        )
        sys.exit(1)

    exit_code = asyncio.run(run_proxy(source, command))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
