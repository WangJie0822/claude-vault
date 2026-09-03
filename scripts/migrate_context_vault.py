#!/usr/bin/env python3
"""Non-destructively migrate 0.9.x Claude data into ~/.context-vault.

Dry-run is the default. Apply copies data through a staging directory and
never removes legacy files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_vault.atomic import atomic_write_json, lease_lock
from context_vault.paths import (
    canonical_config,
    context_home,
    default_vault,
    legacy_default_vault,
    legacy_loader_config,
)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _reject_link_tree(path: Path) -> None:
    if not path.exists():
        return
    if _is_link_or_reparse(path):
        raise ValueError(f"migration refuses symlink/junction source: {path}")
    if path.is_dir():
        for child in path.rglob("*"):
            if _is_link_or_reparse(child):
                raise ValueError(f"migration refuses symlink/junction source: {child}")


def _read_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"config root must be object: {path}")
    return data


def _normalized(path: str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def inspect(home: Path) -> dict:
    loader_path = legacy_loader_config(home)
    summary_path = home / ".claude" / "skills" / "summarize-session" / "config.json"
    loader = _read_object(loader_path)
    summary = _read_object(summary_path)
    loader_vault = loader.get("vault_path") if isinstance(loader.get("vault_path"), str) else ""
    summary_vault = (summary.get("default_vault_path")
                     if isinstance(summary.get("default_vault_path"), str) else "")
    conflict = bool(loader_vault and summary_vault
                    and _normalized(loader_vault) != _normalized(summary_vault))
    old_metrics = home / ".claude" / "vault-loader-metrics"
    old_manifest = home / ".claude" / "skills" / "summarize-session" / "summarized-sessions.json"
    state_files = list((home / ".claude" / "projects").glob("*/vault-loader-state.json"))
    # 0.9.x 零配置用户盘上是 `_MINIMAL_STUB`（不含 vault_path），其笔记实际落在
    # legacy 默认路径下。只报 `loader_vault: ""` 会让 dry-run 看起来「没什么可迁的」，
    # 而迁移随后把 canonical 指向一个全新空目录——用户没有任何机会察觉。
    legacy_install = loader_path.exists() or summary_path.exists()
    implicit_vault = ""
    if legacy_install and not loader_vault and not summary_vault:
        candidate = legacy_default_vault(home)
        implicit_vault = str(candidate)
    return {
        "loader_config": str(loader_path) if loader_path.exists() else "",
        "summary_config": str(summary_path) if summary_path.exists() else "",
        "loader_vault": loader_vault,
        "summary_vault": summary_vault,
        "loader_vault_implicit": implicit_vault,
        "vault_conflict": conflict,
        "metrics_present": old_metrics.is_dir(),
        "session_manifest_present": old_manifest.is_file(),
        "legacy_state_files": len(state_files),
    }


def _build_config(home: Path, report: dict) -> dict:
    loader = _read_object(legacy_loader_config(home))
    # 顺序即优先级：显式配置 > 0.9.x 隐式默认（有 legacy 安装痕迹时）> 全新安装默认。
    # 漏掉隐式那一档，等于把「装过 0.9.x 但没改过配置」的用户迁到一个空目录。
    selected = (report["loader_vault"] or report["summary_vault"]
                or report.get("loader_vault_implicit") or str(default_vault(home)))
    config = dict(loader)
    config.pop("_comment", None)
    config["_config_version"] = 2
    config["vault_path"] = selected
    config["runtimes"] = {"claude": {"enabled": True}, "codex": {"enabled": True}}
    return config


def apply_migration(home: Path) -> dict:
    report = inspect(home)
    if report["vault_conflict"]:
        raise ValueError("legacy vault paths conflict; align them before migration")
    target_home = context_home(home)
    if target_home.exists() and _is_link_or_reparse(target_home):
        raise ValueError("canonical home must not be a symlink or junction")
    target_home.mkdir(parents=True, exist_ok=True)
    migration_state = target_home / "migration.json"
    # A large metrics tree may take minutes to copy. Do not let the generic
    # short-operation stale lease admit a second migration mid-copy.
    with lease_lock(migration_state, timeout=5.0, stale_after=3600.0):
        if canonical_config(home).exists():
            raise ValueError(
                "canonical config already exists; migration refuses to overwrite it"
            )
        migration_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        staging = target_home / ".migration-staging" / migration_id
        staging.mkdir(parents=True, exist_ok=False)
        # `staging.mkdir` 与 try 之间不得再有可失败的操作：那里抛出会绕过下面的
        # 清理分支、留下残留目录。状态写入本身就会失败（它走 os.replace，与迁移
        # 的其余步骤同源），所以必须放在 try 内。
        try:
            atomic_write_json(migration_state, {
                "schema": 1, "status": "staging", "migration_id": migration_id,
                "legacy_preserved": True,
            })
            atomic_write_json(staging / "config.json", _build_config(home, report))
            old_metrics = home / ".claude" / "vault-loader-metrics"
            if old_metrics.is_dir():
                _reject_link_tree(old_metrics)
                shutil.copytree(old_metrics, staging / "metrics" / "claude", dirs_exist_ok=True)
            old_manifest = (home / ".claude" / "skills" / "summarize-session" /
                            "summarized-sessions.json")
            if old_manifest.is_file():
                manifest = _read_object(old_manifest)
                sessions = manifest.get("sessions", [])
                upgraded = {
                    "schema": 2,
                    "sessions": [{"runtime": "claude-legacy", "id": sid}
                                 for sid in sessions if isinstance(sid, str)],
                    "updated": manifest.get("updated", ""),
                }
                atomic_write_json(staging / "sessions" / "summarized-sessions.json", upgraded)
            # Legacy injection state is session-scoped and has no safe mapping
            # to the new runtime/session namespace. Preserve it in place rather
            # than copying dead state that no consumer can read.

            # Data is copied first while the canonical config is absent, so
            # hooks continue using the intact legacy layout. Partial data is
            # safe to overwrite on retry; config is the final commit marker.
            for name in ("metrics", "sessions", "state"):
                source = staging / name
                if source.exists():
                    shutil.copytree(source, target_home / name, dirs_exist_ok=True)
            os.replace(staging / "config.json", canonical_config(home))
            atomic_write_json(migration_state, {
                "schema": 1, "status": "committed", "migration_id": migration_id,
                "legacy_preserved": True, "report": report,
            })
            # Only remove the exact staging tree created by this invocation;
            # legacy sources and committed canonical data are never deleted.
            shutil.rmtree(staging)
            report["status"] = "committed"
            report["canonical_config"] = str(canonical_config(home))
            return report
        except Exception:
            # ⚠️ 状态写入必须自带兜底：它内部也走 `os.replace`，与刚失败的那步
            # 同源（磁盘满、权限、文件占用）。裸调的话它会**再抛一次**，于是下面
            # 的清理永远不执行——这不是理论风险，补测试时第一次就撞上了。
            try:
                atomic_write_json(migration_state, {
                    "schema": 1, "status": "failed", "migration_id": migration_id,
                    "legacy_preserved": True,
                })
            except Exception:       # noqa: BLE001 — 记不下状态也不能妨碍清理与原始异常
                pass
            # 失败分支必须清掉**本次**的 staging：它含 metrics 全量副本（连同
            # `.salt` 与不可再生的 `annotations.jsonl`），而每次重试都会新建一个
            # migration_id 目录 ⇒ 反复失败会在 canonical home 下永久堆积多份敏感
            # 数据副本，且全仓库没有任何代码路径会清理它们。
            # `ignore_errors=True`：清理失败不得掩盖真正的迁移异常。
            shutil.rmtree(staging, ignore_errors=True)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="migrate Context Vault data non-destructively")
    parser.add_argument("--apply", action="store_true", help="copy into ~/.context-vault")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = apply_migration(args.home) if args.apply else inspect(args.home)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if not args.apply:
        report["status"] = "dry-run"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
