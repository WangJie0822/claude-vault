from pathlib import Path
from scripts._vault_init import ensure_vault


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
