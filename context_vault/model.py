"""CLI model backends used by optional, user-triggered enrichment tasks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


KEYWORDS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 8,
        }
    },
    "required": ["keywords"],
    "additionalProperties": False,
}


def call_claude(prompt: str, *, timeout: int = 60,
                model: str = "haiku") -> str | None:
    executable = shutil.which("claude")
    if executable is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        result = subprocess.run(
            [executable, "-p", "--model", model, "--tools", "",
             "--no-session-persistence"], input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def call_codex(prompt: str, *, timeout: int = 60,
               schema: dict | None = None) -> str | None:
    """Run an isolated Codex turn and return only its validated final message."""
    executable = shutil.which("codex")
    if executable is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="context-vault-codex-") as raw_tmp:
            tmp = Path(raw_tmp)
            schema_path = tmp / "output.schema.json"
            output_path = tmp / "last-message.json"
            schema_path.write_text(
                json.dumps(schema or KEYWORDS_SCHEMA, ensure_ascii=False), encoding="utf-8"
            )
            argv = [
                executable, "exec", "-",
                "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "--sandbox", "read-only", "--disable", "hooks",
                "--disable", "shell_tool", "--disable", "apps",
                "--disable", "browser_use", "--disable", "computer_use",
                "--disable", "multi_agent", "--disable", "code_mode_host",
                "--output-schema", str(schema_path),
                "--output-last-message", str(output_path),
                "--color", "never", "-C", str(tmp),
            ]
            result = subprocess.run(
                argv, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                env=env, shell=False,
            )
            if result.returncode != 0 or not output_path.is_file():
                return None
            return output_path.read_text(encoding="utf-8-sig")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None


def choose_backend(requested: str = "auto") -> str:
    if requested in {"claude", "codex"}:
        return requested
    if requested != "auto":
        raise ValueError(f"unsupported backend: {requested}")
    if os.environ.get("PLUGIN_ROOT"):
        return "codex"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    raise ValueError(
        "cannot infer model backend outside a hook; pass --backend claude or codex"
    )
