import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))


def test_no_spawn_when_auto_disabled(monkeypatch, tmp_path):
    import session_end_enqueue as m
    called = {"spawn": False}
    monkeypatch.setattr(m, "spawn_enqueue", lambda *a, **k: called.__setitem__("spawn", True))
    monkeypatch.setattr(m, "_auto_enabled", lambda: False)  # 见 Step 3 新增 helper
    m.main_with(session_id="s1", cwd=str(tmp_path))
    assert called["spawn"] is False


# ---------------------------------------------------------------------------
# C1 fix: _auto_enabled() reads config from user-state path (AUTO_SKILL_ROOT)
# ---------------------------------------------------------------------------

def test_auto_enabled_reads_user_state_path_enabled(monkeypatch, tmp_path):
    """AUTO_SKILL_ROOT 指向含 auto.enabled=true 的 config.json 时返回 True（C1 实证）。"""
    # 写 config.json 到 tmp 用户态目录
    config = {"auto": {"enabled": True}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    # 让 _auto_config 模块从插件目录可导入
    plugin_root = str(ROOT)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", plugin_root)
    monkeypatch.setenv("AUTO_SKILL_ROOT", str(tmp_path))

    # 重新导入以清除模块缓存（避免已缓存的 _auto_config 影响结果）
    import importlib
    import session_end_enqueue as m
    importlib.reload(m)

    result = m._auto_enabled()
    assert result is True, "C1: _auto_enabled() should return True when config.json has auto.enabled=true"


def test_auto_enabled_reads_user_state_path_disabled(monkeypatch, tmp_path):
    """AUTO_SKILL_ROOT 指向含 auto.enabled=false 的 config.json 时返回 False。"""
    config = {"auto": {"enabled": False}}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    monkeypatch.setenv("AUTO_SKILL_ROOT", str(tmp_path))

    import importlib
    import session_end_enqueue as m
    importlib.reload(m)

    result = m._auto_enabled()
    assert result is False


def test_auto_enabled_missing_config_returns_false(monkeypatch, tmp_path):
    """AUTO_SKILL_ROOT 下无 config.json 时返回 False（fail-open）。"""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    monkeypatch.setenv("AUTO_SKILL_ROOT", str(tmp_path))  # tmp_path 内无 config.json

    import importlib
    import session_end_enqueue as m
    importlib.reload(m)

    result = m._auto_enabled()
    assert result is False
