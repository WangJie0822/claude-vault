from pathlib import Path
from scripts._vault_init import ensure_vault, ensure_vault_if_default


def test_creates_missing_vault(tmp_path):
    vp = tmp_path / "knowledge-vault"
    ensure_vault(vp)
    assert vp.is_dir()
    assert (vp / ".meta").is_dir()


def test_idempotent(tmp_path):
    vp = tmp_path / "kv"
    ensure_vault(vp)
    ensure_vault(vp)  # 第二次不报错
    assert (vp / ".meta").is_dir()


def test_canonical_default_is_initialized(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    vp = tmp_path / ".context-vault" / "knowledge-vault"
    assert ensure_vault_if_default(vp) is True
    assert (vp / ".meta").is_dir()
