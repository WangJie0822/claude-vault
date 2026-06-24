"""scripts/migrate_settings.py — remove old hook registrations from settings.json.

Targets the 4 hook scripts that are now bundled inside the claude-vault plugin.
Running without --apply is a safe dry-run: nothing is modified.

Claude Code settings.json hooks format (two supported layouts):

  Layout A — matcher+hooks nesting (common):
    "hooks": {
      "EventName": [
        { "matcher": "...", "hooks": [ {entry}, {entry}, ... ] }
      ]
    }

  Layout B — flat list of entries:
    "hooks": {
      "EventName": [ {entry}, {entry}, ... ]
    }

Both layouts are handled. An entry is matched if its "command" string or any
element of its "args" list contains one of the 4 TARGET_FILENAMES.

Usage:
  python3 scripts/migrate_settings.py                         # dry-run
  python3 scripts/migrate_settings.py --apply                 # backup + strip
  python3 scripts/migrate_settings.py --settings <path>       # custom settings file
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# The 4 hook script filenames that are now owned by the plugin.
# An entry is matched if any of these names appears in:
#   - the entry's top-level "command" string, OR
#   - any element of the entry's "args" list.
TARGET_FILENAMES: frozenset[str] = frozenset(
    {
        "session_start_load.py",
        "session_start_auto_notify.py",
        "prompt_submit_load.py",
        "session_end_enqueue.py",
    }
)


def _entry_matches(entry: object) -> bool:
    """Return True if *entry* references any TARGET_FILENAMES."""
    if not isinstance(entry, dict):
        return False
    # Check top-level "command" field
    command = entry.get("command", "")
    if isinstance(command, str):
        for name in TARGET_FILENAMES:
            if name in command:
                return True
    # Check each element of "args" list
    args = entry.get("args", [])
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str):
                for name in TARGET_FILENAMES:
                    if name in arg:
                        return True
    return False


def _describe_entry(entry: dict) -> str:
    """Return a human-readable description of a hook entry for output."""
    cmd = entry.get("command", "<no command>")
    args = entry.get("args", [])
    if args:
        args_str = " ".join(str(a) for a in args)
        return f"{cmd} {args_str}"
    return cmd


def _filter_entries(entries: list) -> tuple[list, list]:
    """Split *entries* into (surviving, matched).

    Returns:
        surviving: entries that do NOT match any target
        matched:   entries that DO match (to be removed)
    """
    surviving: list = []
    matched: list = []
    for entry in entries:
        if _entry_matches(entry):
            matched.append(entry)
        else:
            surviving.append(entry)
    return surviving, matched


def _process_group(group: object) -> tuple[object | None, list[dict]]:
    """Process a single group item from the event list.

    A group is either:
      - A dict with a "hooks" key (matcher+hooks layout): inner entries are filtered
      - A flat hook-entry dict (no "hooks" key): treated as a single entry
      - A list of entries (also handled for robustness)

    Returns:
        (new_group, matched_entries):
            new_group is None if the group becomes empty and should be dropped.
    """
    matched_all: list[dict] = []

    if isinstance(group, dict) and "hooks" in group and isinstance(group["hooks"], list):
        # Layout A: matcher+hooks nesting
        inner_entries: list = group["hooks"]
        surviving, matched = _filter_entries(inner_entries)
        matched_all.extend(matched)
        if not surviving:
            # All inner entries removed — drop the whole group
            return None, matched_all
        new_group = dict(group)
        new_group["hooks"] = surviving
        return new_group, matched_all

    elif isinstance(group, dict):
        # Layout B: the group dict IS the hook entry
        if _entry_matches(group):
            return None, [group]
        return group, []

    elif isinstance(group, list):
        # List of entries (robustness)
        surviving, matched = _filter_entries(group)
        matched_all.extend(matched)
        if not surviving:
            return None, matched_all
        return surviving, matched_all

    else:
        return group, []


def run_migration(
    settings_path: Path,
    *,
    apply: bool = False,
) -> int:
    """Core migration logic.

    Args:
        settings_path: Path to the settings.json file.
        apply:         If True, back up + write the modified file.
                       If False (default), dry-run only.

    Returns:
        0 on success / nothing-to-do, non-zero on error.
    """
    # --- Load -----------------------------------------------------------------
    if not settings_path.exists():
        print("nothing to migrate (already migrated?): settings.json not found at", settings_path)
        return 0

    try:
        raw_bytes = settings_path.read_bytes()
        text = raw_bytes.decode("utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read/parse {settings_path}: {exc}", file=sys.stderr)
        return 1

    hooks: dict = data.get("hooks", {})
    if not isinstance(hooks, dict):
        print("nothing to migrate (already migrated?): no 'hooks' dict found")
        return 0

    # --- Scan -----------------------------------------------------------------
    # Collect matched entries; rebuild a clean hooks dict in parallel.
    matched: list[tuple[str, dict]] = []   # (event_key, entry)
    new_hooks: dict = {}

    for event_key, groups in hooks.items():
        if not isinstance(groups, list):
            # Preserve non-list values as-is
            new_hooks[event_key] = groups
            continue

        new_groups: list = []
        for group in groups:
            new_group, group_matched = _process_group(group)
            for m in group_matched:
                matched.append((event_key, m))
            if new_group is not None:
                new_groups.append(new_group)

        if new_groups:
            new_hooks[event_key] = new_groups
        else:
            # Event key with all groups removed: keep key with empty list
            # so callers can check `hooks.get("SessionEnd", []) == []`
            new_hooks[event_key] = []

    # --- Nothing to do? -------------------------------------------------------
    if not matched:
        print("nothing to migrate (already migrated?): no target hook entries found")
        return 0

    # --- Dry-run report -------------------------------------------------------
    print(f"Found {len(matched)} hook entry/entries to remove:\n")
    for event_key, entry in matched:
        desc = _describe_entry(entry)
        print(f"  [{event_key}]  {desc}")

    if not apply:
        print(
            "\n--- DRY-RUN (no changes made) ---\n"
            "Next steps:\n"
            "  1. Review the entries listed above.\n"
            "  2. Run with --apply to remove them and back up the original:\n"
            "       python3 scripts/migrate_settings.py --apply\n"
            "  3. Restart Claude Code (or start a new session) after applying."
        )
        return 0

    # --- Apply ----------------------------------------------------------------
    # 1. Back up original (byte-for-byte copy)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = settings_path.with_name(f"{settings_path.name}.bak-{ts}")
    backup_path.write_bytes(raw_bytes)
    print(f"\nBackup saved to: {backup_path}")

    # 2. Write stripped settings
    new_data = dict(data)
    new_data["hooks"] = new_hooks
    settings_path.write_text(json.dumps(new_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated: {settings_path}")

    print(
        "\n--- DONE ---\n"
        "Now configure --plugin-dir and restart Claude Code:\n"
        "  claude --plugin-dir \"<plugin directory absolute path>\"\n"
        "Or add a wrapper to $PROFILE for persistence (see docs/MIGRATION.md)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove old vault-loader/summarize-session hook registrations from settings.json. "
            "Default: dry-run (no changes). Use --apply to modify the file."
        )
    )
    parser.add_argument(
        "--settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        metavar="PATH",
        help="Path to settings.json (default: ~/.claude/settings.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Back up settings.json and remove matched hook entries. Without this flag, only a dry-run report is printed.",
    )
    args = parser.parse_args(argv)
    return run_migration(Path(args.settings), apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
