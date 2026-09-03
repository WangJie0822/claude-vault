#!/usr/bin/env python3
"""Read-only installation, configuration, and migration diagnostics."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_vault.paths import canonical_config, context_home, legacy_loader_config
from context_vault.runtime import detect_runtime
from context_vault.coexist import legacy_plugin_enabled
from scripts.migrate_context_vault import inspect


_EMPTY_LEGACY = {
    "loader_config": "", "summary_config": "", "loader_vault": "", "summary_vault": "",
    "loader_vault_implicit": "", "vault_conflict": False, "metrics_present": False,
    "session_manifest_present": False, "legacy_state_files": 0,
}


def diagnose(home: Path, runtime: str = "auto") -> dict:
    """只读诊断。**任何一步都不得抛异常。**

    这是排障工具：它被调用的场景恰恰是「有东西坏了」。旧实现把 `inspect()`
    （畸形 legacy config 会抛 JSONDecodeError）与 `VERSION` 读取（缺文件会抛
    FileNotFoundError）都裸调，于是在最需要它的时候自己先崩掉一个 traceback。
    出错的部分降级为占位值并在 `errors` 里如实列出，其余诊断照常给出。
    """
    errors: list[str] = []
    version_path = ROOT / "VERSION"
    try:
        legacy = inspect(home)
    except Exception as exc:            # noqa: BLE001 — 排障工具不得因被诊断对象而崩
        legacy = dict(_EMPTY_LEGACY)
        errors.append(f"legacy 配置无法解析：{exc}")
    selected_runtime = detect_runtime({}).value if runtime == "auto" else runtime
    try:
        coexistence_risk = (
            legacy_plugin_enabled("claude", home=home)
            or legacy_plugin_enabled("codex", home=home)
            if selected_runtime == "unknown"
            else legacy_plugin_enabled(selected_runtime, home=home)
        )
    except Exception as exc:            # noqa: BLE001
        coexistence_risk = False
        errors.append(f"共存检测失败：{exc}")
    canonical = canonical_config(home)
    disabled = [
        str(path) for path in (
            context_home(home) / ".disabled",
            home / ".claude" / ".vault-loader-disabled",
        ) if path.exists()
    ]
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        version = "unknown"
        errors.append(f"VERSION 不可读：{exc}")
    return {
        "ok": not legacy["vault_conflict"] and not coexistence_risk and not errors,
        "errors": errors,
        "version": version,
        "runtime": selected_runtime,
        "plugin_root": str(ROOT),
        "manifests": {
            "claude": (ROOT / ".claude-plugin/plugin.json").is_file(),
            "codex": (ROOT / ".codex-plugin/plugin.json").is_file(),
        },
        "canonical_config": str(canonical) if canonical.exists() else "",
        "legacy_config": str(legacy_loader_config(home))
        if legacy_loader_config(home).exists() else "",
        "migration_needed": not canonical.exists() and any((
            legacy["loader_config"], legacy["summary_config"],
            legacy["metrics_present"], legacy["session_manifest_present"],
            legacy["legacy_state_files"],
        )),
        "vault_conflict": legacy["vault_conflict"],
        "coexistence_risk": coexistence_risk,
        "disabled_by": disabled,
        "environment": {
            "PLUGIN_ROOT": bool(os.environ.get("PLUGIN_ROOT")),
            "CLAUDE_PLUGIN_ROOT": bool(os.environ.get("CLAUDE_PLUGIN_ROOT")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Context Vault read-only doctor")
    parser.add_argument("--runtime", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = diagnose(args.home, args.runtime)
    except Exception as exc:            # noqa: BLE001 — 兜到最外层，绝不给用户 traceback
        print(json.dumps({"ok": False, "errors": [f"doctor 自身失败：{exc}"]},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
