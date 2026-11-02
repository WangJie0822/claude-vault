#!/bin/sh
# Dedicated entry: the host is never inferred from environment variable names.
_cv_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd) || exit 0
CLAUDE_PLUGIN_ROOT=$_cv_root
export CLAUDE_PLUGIN_ROOT
/bin/sh "$_cv_root/hooks/run-hook.cmd" context_vault/codex_entry.py
exit 0
