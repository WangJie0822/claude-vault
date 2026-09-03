from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.migrate_context_vault import apply_migration, inspect


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_dry_run_detects_conflicting_legacy_vaults(tmp_path):
    _write(tmp_path / ".claude/skills/vault-loader/config.json", {"vault_path": str(tmp_path / "a")})
    _write(tmp_path / ".claude/skills/summarize-session/config.json",
           {"default_vault_path": str(tmp_path / "b")})
    assert inspect(tmp_path)["vault_conflict"] is True


def test_apply_is_non_destructive_and_upgrades_manifest(tmp_path):
    vault = tmp_path / "vault"
    loader = tmp_path / ".claude/skills/vault-loader/config.json"
    summary = tmp_path / ".claude/skills/summarize-session/config.json"
    manifest = tmp_path / ".claude/skills/summarize-session/summarized-sessions.json"
    _write(loader, {"vault_path": str(vault), "metrics": {"enabled": True}})
    _write(summary, {"default_vault_path": str(vault)})
    _write(manifest, {"sessions": ["s1"], "updated": "then"})
    metrics = tmp_path / ".claude/vault-loader-metrics/2026-08/s1.jsonl"
    metrics.parent.mkdir(parents=True)
    metrics.write_text('{"_schema":1}\n', encoding="utf-8")

    result = apply_migration(tmp_path)
    assert result["status"] == "committed"
    assert loader.exists() and summary.exists() and manifest.exists()
    canonical = json.loads((tmp_path / ".context-vault/config.json").read_text(encoding="utf-8"))
    assert canonical["_config_version"] == 2
    assert canonical["vault_path"] == str(vault)
    upgraded = json.loads((tmp_path / ".context-vault/sessions/summarized-sessions.json")
                          .read_text(encoding="utf-8"))
    assert upgraded["sessions"] == [{"runtime": "claude-legacy", "id": "s1"}]
    assert (tmp_path / ".context-vault/metrics/claude/2026-08/s1.jsonl").exists()
    state = json.loads((tmp_path / ".context-vault/migration.json").read_text(encoding="utf-8"))
    assert state["status"] == "committed"
    assert list((tmp_path / ".context-vault/.migration-staging").iterdir()) == []


def test_apply_refuses_to_overwrite_canonical_config(tmp_path):
    canonical = tmp_path / ".context-vault/config.json"
    _write(canonical, {"vault_path": "keep-me"})
    with pytest.raises(ValueError, match="refuses to overwrite"):
        apply_migration(tmp_path)
    assert json.loads(canonical.read_text(encoding="utf-8"))["vault_path"] == "keep-me"


def test_apply_failure_before_config_commit_is_retryable(monkeypatch, tmp_path):
    _write(tmp_path / ".claude/skills/vault-loader/config.json",
           {"vault_path": str(tmp_path / "vault")})
    metrics = tmp_path / ".claude/vault-loader-metrics/2026-08/s1.jsonl"
    metrics.parent.mkdir(parents=True)
    metrics.write_text('{}\n', encoding="utf-8")
    import scripts.migrate_context_vault as migration
    real_copytree = migration.shutil.copytree
    failed = False

    def fail_commit_copy(source, target, *args, **kwargs):
        nonlocal failed
        if not failed and ".migration-staging" in str(source):
            failed = True
            raise OSError("injected copy failure")
        return real_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(migration.shutil, "copytree", fail_commit_copy)
    with pytest.raises(OSError, match="injected"):
        apply_migration(tmp_path)
    assert not (tmp_path / ".context-vault/config.json").exists()
    monkeypatch.setattr(migration.shutil, "copytree", real_copytree)
    assert apply_migration(tmp_path)["status"] == "committed"


def test_failed_migration_cleans_up_its_staging(tmp_path, monkeypatch):
    """迁移失败必须清掉本次 staging。

    staging 含 metrics 全量副本（连同 `.salt` 与不可再生的 annotations.jsonl），
    而每次重试都新建一个 migration_id 目录 ⇒ 反复失败会在 canonical home 下永久
    堆积多份敏感数据副本，全仓库没有任何路径会清理它们。
    """
    import shutil as _shutil

    from scripts import migrate_context_vault as M

    home = tmp_path / "home"
    legacy = home / ".claude" / "skills" / "vault-loader"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"vault_path": str(tmp_path / "V")}),
                                        encoding="utf-8")
    metrics = home / ".claude" / "vault-loader-metrics"
    metrics.mkdir(parents=True)
    (metrics / ".salt").write_bytes(b"x" * 32)

    # 精确地只让「最终提交」那一步失败：staging 内的写入照常成功，模拟
    # 「数据已拷完、提交 canonical config 时磁盘满」。粗暴地 patch 掉全局
    # os.replace 会让失败提前到 staging 写入阶段，测不到目标场景。
    real_replace = M.os.replace

    def _fail_only_commit(src, dst, *a, **k):
        dst_s = str(dst)
        if dst_s.endswith("config.json") and ".migration-staging" not in dst_s:
            raise OSError("disk full")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(M.os, "replace", _fail_only_commit)
    with pytest.raises(OSError):
        M.apply_migration(home)
    assert not (home / ".context-vault" / "config.json").exists(), \
        "提交失败后 canonical config 不得存在（它是唯一的提交标记）"

    staging_root = home / ".context-vault" / ".migration-staging"
    leftovers = list(staging_root.iterdir()) if staging_root.exists() else []
    assert not leftovers, f"失败后残留 staging：{leftovers}"
    # legacy 数据一如既往不得被动过
    assert (metrics / ".salt").is_file()
