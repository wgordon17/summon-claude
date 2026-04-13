#!/usr/bin/env python3
"""Generate docs/reference/environment-variables.md from CONFIG_OPTIONS.

Replaces content between ``<!-- config:GROUP -->`` / ``<!-- /config:GROUP -->``
markers with a generated markdown table.  Prose and headings outside markers
are left untouched.

Usage::

    uv run python scripts/generate_env_docs.py          # regenerate
    uv run python scripts/generate_env_docs.py --check   # exit 1 if stale
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_PATH = _REPO_ROOT / "docs" / "reference" / "environment-variables.md"

# Matches  <!-- config:NAME -->\n...\n<!-- /config:NAME -->
_CONFIG_BLOCK_RE = re.compile(
    r"(<!-- config:(\S+) -->\n).*?(<!-- /config:\2 -->)",
    re.DOTALL,
)

# Human-readable default overrides for fields whose Python default is None or
# whose rendered form in the doc differs from str(py_default).
# Keys are SUMMON_* env var names. Values are the string to put in the Default
# column, or None for secret 3-column tables (no Default column).
# Cross-ref: FUZZY_DEFAULTS in tests/docs/test_env_vars.py covers the same set.
_DEFAULT_OVERRIDES: dict[str, str | None] = {
    # Credentials — 3-column table, no Default column
    "SUMMON_SLACK_BOT_TOKEN": None,
    "SUMMON_SLACK_APP_TOKEN": None,
    "SUMMON_SLACK_SIGNING_SECRET": None,
    # None -> descriptive prose
    "SUMMON_DEFAULT_MODEL": "_(Claude's default)_",
    "SUMMON_SCRIBE_ENABLED": "_auto-detect_",
    "SUMMON_SCRIBE_CWD": "_(data dir)/scribe_",
    "SUMMON_SCRIBE_MODEL": "_(inherits default model)_",
    "SUMMON_SCRIBE_IMPORTANCE_KEYWORDS": "_(empty)_",
    "SUMMON_SCRIBE_QUIET_HOURS": "_(empty)_",
    "SUMMON_SCRIBE_GOOGLE_ENABLED": "auto-detect",
    "SUMMON_SCRIBE_SLACK_ENABLED": "_auto-detect_",
    "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS": "_(empty)_",
    "SUMMON_GLOBAL_PM_CWD": "_(data dir)_",
    "SUMMON_GLOBAL_PM_MODEL": "_(inherit)_",
}


# Guard: every secret field must have repr=False in SummonConfig
def _check_secret_repr() -> None:
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    prefix = SummonConfig.model_config.get("env_prefix", "SUMMON_")
    repr_false = {
        f"{prefix}{field_name.upper()}"
        for field_name, field_info in SummonConfig.model_fields.items()
        if not field_info.repr
    }
    secret_by_input_type = {opt.env_key for opt in CONFIG_OPTIONS if opt.input_type == "secret"}
    mismatches = secret_by_input_type - repr_false
    if mismatches:
        raise RuntimeError(
            f"input_type='secret' but repr=True in SummonConfig: {sorted(mismatches)}"
        )


def _slugify(group: str) -> str:
    """Convert a group name to a marker slug (lowercase, spaces to hyphens)."""
    return group.lower().replace(" ", "-")


def _derive_type(opt: Any, field_info: Any) -> str:  # type: ignore[type-arg]
    """Derive the doc Type column value from a ConfigOption and its FieldInfo."""
    # Secret fields always render as "secret"
    if opt.input_type == "secret":
        return "secret"

    # Static choices — render as "choice: `v1`, `v2`..."
    if opt.choices:
        vals = ", ".join(f"`{c}`" for c in opt.choices)
        return f"choice: {vals}"

    # choices_fn only (no static choices) — render as "text" to avoid calling fn
    if opt.choices_fn:
        return "text"

    # Derive from Python annotation
    ann = field_info.annotation
    if hasattr(ann, "__args__"):
        # Optional[X] / X | None
        non_none = [a for a in ann.__args__ if a is not type(None)]
        py_type = non_none[0].__name__ if non_none else "str"
    else:
        py_type = getattr(ann, "__name__", str(ann))

    type_map = {"str": "text", "int": "integer", "bool": "boolean"}
    return type_map.get(py_type, py_type)


def _derive_default(opt: Any, field_info: Any) -> str | None:  # type: ignore[type-arg]
    """Return the Default column string, or None for 3-column (secret) tables."""
    if opt.env_key in _DEFAULT_OVERRIDES:
        return _DEFAULT_OVERRIDES[opt.env_key]

    default = field_info.default
    pydantic_undefined = "PydanticUndefined"
    if str(default) == pydantic_undefined:
        return None  # Required field; 3-col table or no default shown

    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if default is None:
        return "_(empty)_"
    if default == "":
        return "``"
    return f"`{default}`"


def get_group_tables() -> dict[str, str]:
    """Return ``{marker_slug: table_markdown}`` for each CONFIG_OPTIONS group."""
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    # Group options preserving order
    groups: dict[str, list] = {}
    for opt in CONFIG_OPTIONS:
        slug = _slugify(opt.group)
        groups.setdefault(slug, []).append(opt)

    tables: dict[str, str] = {}
    for slug, opts in groups.items():
        # Determine if this group is all-secret (3-col table)
        all_secret = all(o.input_type == "secret" for o in opts)

        if all_secret:
            header = "| Config Key | Type | Description |\n|------------|------|-------------|"
        else:
            header = (
                "| Config Key | Type | Default | Description |\n"
                "|------------|------|---------|-------------|"
            )

        rows: list[str] = [header]
        for opt in opts:
            field_info = SummonConfig.model_fields.get(opt.field_name)
            if field_info is None:
                continue

            key_col = f"`{opt.env_key}`"
            type_col = _derive_type(opt, field_info)
            desc_col = opt.help_text

            if all_secret:
                rows.append(f"| {key_col} | {type_col} | {desc_col} |")
            else:
                default_col = _derive_default(opt, field_info)
                if default_col is None:
                    default_col = ""
                rows.append(f"| {key_col} | {type_col} | {default_col} | {desc_col} |")

        tables[slug] = "\n".join(rows)

    return tables


def generate(content: str, tables: dict[str, str]) -> str:
    """Replace config blocks in *content* with generated tables."""

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        marker = m.group(2)
        if marker not in tables:
            return m.group(0)  # leave unknown markers unchanged
        table = tables[marker]
        return f"{m.group(1)}{table}\n{m.group(3)}"

    return _CONFIG_BLOCK_RE.sub(_replace, content)


def main() -> int:
    check_only = "--check" in sys.argv

    _check_secret_repr()  # fail fast if secret/repr invariant is violated

    tables = get_group_tables()
    content = _DOC_PATH.read_text(encoding="utf-8")
    updated = generate(content, tables)

    if check_only:
        if content == updated:
            print("environment-variables.md is up to date")  # noqa: T201
            return 0
        print(  # noqa: T201
            "environment-variables.md is stale — run: uv run python scripts/generate_env_docs.py"
        )
        return 1

    _DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"Updated {_DOC_PATH.relative_to(_REPO_ROOT)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
