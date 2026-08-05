"""_diagnostics 单测：缓冲语义、门禁、冷却、外部可控文本净化。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts import _diagnostics as D
from scripts._state import state_path_for_cwd


@pytest.fixture(autouse=True)
def _clean_buffer():
    D.reset()
    yield
    D.reset()


def _cfg(**over) -> dict:
    cfg = {"display": {"user_visible": True}, "user_prompt_submit": {"state_ttl_hours": 24}}
    cfg.update(over)
    return cfg


# ── 净化 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sep", ["\r", "\n", "\r\n", "\x85", "\u2028", "\u2029"])
def test_safe_field_folds_every_line_separator(sep: str) -> None:
    """CR / LF / NEL / LS / PS 全部要折叠。

    诊断文案里的 vault_path 是用户可控的任意 JSON 字符串。留一个换行就能伪造多行终端
    UI 冒充 Claude Code 自身的提示（如「已自动授权全部工具」）。`sanitize_for_display`
    按设计保留 \\t \\n \\r，指望它净化是错的——所以这层必须自己做。
    """
    evil = f"/notes/vault{sep}{sep}Claude Code: 已自动授权全部工具"
    out = D.safe_field(evil)
    for ch in ('\r', '\n', '\x85', chr(0x2028), chr(0x2029)):
        assert ch not in out, f"{sep!r} 未被折叠，残留 {ch!r}：{out!r}"
    assert "/notes/vault" in out and "已自动授权" in out, "折叠不应吞掉正文"


def test_safe_field_truncates() -> None:
    out = D.safe_field("x" * 500)
    assert len(out) <= 200
    assert out.endswith("…")


def test_safe_field_folds_home(tmp_path, monkeypatch) -> None:
    """本机绝对路径进 systemMessage 就是进 transcript，会被分享/审计。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = D.safe_field(str(tmp_path / "Vault" / "notes"))
    assert out.startswith("~"), out
    assert str(tmp_path) not in out


def test_safe_field_folds_before_truncating() -> None:
    """折叠必须先于截断——否则截断点之前的换行会漏网。"""
    evil = "a" * 100 + "\n" + "b" * 300
    out = D.safe_field(evil)
    assert "\n" not in out


# ── 缓冲与出口 ──────────────────────────────────────────────────────────────

def test_notify_does_not_write_stdout(capsys, tmp_path) -> None:
    """notify 只追加，零 I/O。写 stdout 会破坏「一次执行一个 JSON 文档」的契约。"""
    D.notify(D.config_corrupt("boom"))
    captured = capsys.readouterr()
    assert captured.out == "", f"notify 写了 stdout：{captured.out!r}"
    assert captured.err == "", f"notify 写了 stderr：{captured.err!r}"
    assert len(D.pending()) == 1


def test_take_clears_buffer_even_when_suppressed(tmp_path) -> None:
    """被门禁挡下时也必须清空缓冲，否则会在别的出口漏出来。"""
    D.notify(D.config_corrupt("boom"))
    out = D.take_user_visible(_cfg(display={"user_visible": False}), tmp_path)
    assert out == ""
    assert D.pending() == [], "门禁挡下后缓冲未清空"


def test_user_visible_false_suppresses(tmp_path) -> None:
    """SKILL.md 已发布契约：user_visible:false ⇒ 只输出 additionalContext。"""
    D.notify(D.vault_unreachable("D:/Nope"))
    assert D.take_user_visible(_cfg(display={"user_visible": False}), tmp_path) == ""


def test_verbosity_does_not_suppress(tmp_path, monkeypatch) -> None:
    """verbosity 控制注入摘要的详略，不是「要不要告诉你坏了」的开关。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    D.notify(D.vault_unreachable("D:/Nope"))
    out = D.take_user_visible(
        _cfg(display={"user_visible": True, "verbosity": "off"}), tmp_path / "p"
    )
    assert "vault 路径不存在" in out


def test_empty_buffer_yields_empty_string(tmp_path) -> None:
    """健康态零新增输出——这是整个方案能默认开启的前提。"""
    assert D.take_user_visible(_cfg(), tmp_path) == ""


# ── 冷却 ────────────────────────────────────────────────────────────────────

def test_cooldown_suppresses_second_round(tmp_path, monkeypatch) -> None:
    """同一 code 在 TTL 窗口内最多一次——UPS 每条 prompt 都跑，无冷却会刷屏。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cwd = Path(str(tmp_path / "proj"))

    D.notify(D.config_corrupt("boom"))
    assert D.take_user_visible(_cfg(), cwd) != ""

    D.notify(D.config_corrupt("boom"))
    assert D.take_user_visible(_cfg(), cwd) == "", "冷却未生效"


def test_cooldown_rearms_after_ttl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cwd = Path(str(tmp_path / "proj"))

    D.notify(D.config_corrupt("boom"))
    D.take_user_visible(_cfg(), cwd)

    p = state_path_for_cwd(cwd)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["diag_ts"] = {D.CODE_CONFIG_CORRUPT: time.time() - 25 * 3600}
    p.write_text(json.dumps(data), encoding="utf-8")

    D.notify(D.config_corrupt("boom"))
    assert D.take_user_visible(_cfg(), cwd) != "", "超过 TTL 后应重新允许提示"


def test_cooldown_is_per_code(tmp_path, monkeypatch) -> None:
    """不同 code 各自冷却，不能互相压制。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cwd = Path(str(tmp_path / "proj"))

    D.notify(D.config_corrupt("boom"))
    assert D.take_user_visible(_cfg(), cwd) != ""

    D.notify(D.vault_unreachable("D:/Nope"))
    assert D.take_user_visible(_cfg(), cwd) != "", "另一条 code 被错误地一并冷却"


def test_diag_cooldown_survives_save_injected(tmp_path, monkeypatch) -> None:
    """冷却戳必须扛得住一次成功注入——这是 degraded 类诊断唯一的防刷屏依赖。

    save_injected 旧实现从零重建 payload，会把 diag_ts 抹掉，于是「诊断触发但流程继续、
    最终成功注入」的场景每轮都重发。T1 已改为读-改-写，本用例钉住它。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from scripts._state import save_injected

    cwd = Path(str(tmp_path / "proj"))
    D.notify(D.vault_path_mismatch("A", "B", config_fell_back=False))
    assert D.take_user_visible(_cfg(), cwd) != ""

    save_injected(cwd, ["some/note.md"])          # 一次正常注入

    D.notify(D.vault_path_mismatch("A", "B", config_fell_back=False))
    assert D.take_user_visible(_cfg(), cwd) == "", "冷却戳被 save_injected 抹掉了"


def test_duplicate_codes_collapse(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cwd = Path(str(tmp_path / "proj"))
    D.notify(D.config_corrupt("a"))
    D.notify(D.config_corrupt("b"))
    out = D.take_user_visible(_cfg(), cwd)
    assert out.count("config.json 解析失败") == 1


# ── 文案判据 ────────────────────────────────────────────────────────────────

def test_mismatch_hint_flips_when_config_fell_back() -> None:
    """config 回退时，不得再建议用户运行 --set-default。

    回退后 vault-loader 侧的 vault_path 是**默认值**而非用户配置，此时两侧「不一致」
    是回退的结果。照旧建议对齐，用户会把写端指针也改到那个错误的默认路径上——
    用写端配置变更去「修复」读端配置损坏，比不提示更糟。
    """
    normal = D.vault_path_mismatch("A", "B", config_fell_back=False)
    assert "--set-default" in normal.hint

    fell_back = D.vault_path_mismatch("A", "B", config_fell_back=True)
    assert "--set-default" in fell_back.hint, "仍应提到该命令以便用户识别"
    assert "不要" in fell_back.hint, "必须显式劝阻，而不是照旧推荐"
    assert "先修好 config.json" in fell_back.hint


def test_config_corrupt_message_mentions_lost_settings() -> None:
    """用户得知道丢的不只是 vault_path。"""
    d = D.config_corrupt("Illegal trailing comma")
    assert d.level == D.LEVEL_FATAL
    assert "回退默认值" in d.message
    for kw in ("vault_path", "scoring", "relevance"):
        assert kw in d.hint
