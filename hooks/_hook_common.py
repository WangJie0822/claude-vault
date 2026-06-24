"""claude-vault plugin hook utilities — slimmed copy.

Provides only the three functions consumed by this plugin's hooks:
  - read_stdin_json
  - run_subprocess
  - fail_open

Source reference: ~/.claude/hooks/_hook_common.py (read-only, not modified).
No private paths or personal identifiers are included here.
"""
from __future__ import annotations

import json
import subprocess
import sys


def read_stdin_json(stream=None):
    """Read text stream and json.loads; returns {} on empty/invalid/non-dict input.

    stream: injectable text stream for testing; defaults to sys.stdin.
    """
    if stream is None:
        stream = sys.stdin
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        # Tolerate UTF-8 BOM (U+FEFF) prepended by some tools
        try:
            data = json.loads(raw.lstrip(chr(0xFEFF)))
        except Exception:
            return {}
    # Hook payload is always a JSON object; arrays/scalars/null are treated as invalid
    return data if isinstance(data, dict) else {}


def run_subprocess(args, **kwargs):
    """Enforce list-form + shell=False subprocess.run wrapper.

    - args must be a non-empty list/tuple; strings are rejected to prevent shell injection.
    - shell=True is explicitly forbidden.
    - Default timeout=30s to prevent hanging hooks from blocking the session.
      TimeoutExpired is an Exception subclass and will be caught by fail_open.
    - Other kwargs (capture_output, text, cwd, env, ...) are forwarded as-is.
    """
    if isinstance(args, (str, bytes)):
        raise TypeError("run_subprocess requires list-form args (no strings)")
    if not args:
        raise ValueError("run_subprocess args must not be empty")
    if kwargs.get("shell"):
        raise ValueError("run_subprocess forbids shell=True")
    kwargs["shell"] = False
    kwargs.setdefault("timeout", 30)
    return subprocess.run(list(args), **kwargs)


def fail_open(main_fn):
    """Wrap a hook entry point so it never blocks the session.

    - main_fn returns normally  -> sys.exit(0)
    - main_fn raises Exception  -> swallowed -> sys.exit(0)
    - main_fn raises SystemExit / KeyboardInterrupt -> propagated (not caught)
    """
    try:
        main_fn()
    except Exception:
        pass
    sys.exit(0)
