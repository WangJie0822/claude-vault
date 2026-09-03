"""只服务交互式会话：其余场景 hook 直接返回。

判据与实测背景见 scripts/_entrypoint.py 的模块 docstring。
"""

import io
import json
import sys

import pytest

from scripts._entrypoint import ENTRYPOINT_ENV, is_supported_session


def test_interactive_cli_is_supported(monkeypatch):
    monkeypatch.setenv(ENTRYPOINT_ENV, "cli")
    assert is_supported_session() is True


def test_sdk_cli_is_not_supported(monkeypatch):
    """`claude -p` 及其派生（跨模型评审、批处理工具）。"""
    monkeypatch.setenv(ENTRYPOINT_ENV, "sdk-cli")
    assert is_supported_session() is False


def test_unknown_entrypoint_is_not_supported(monkeypatch):
    """白名单是严格的：没登记过的形态一律不注入。"""
    monkeypatch.setenv(ENTRYPOINT_ENV, "some-future-entry")
    assert is_supported_session() is False


def test_missing_env_is_supported(monkeypatch):
    """变量缺失按交互式处理 —— 否则 harness 改名会静默关掉整个插件。

    变异验证：把 `or "cli"` 去掉，本用例转红。
    """
    monkeypatch.delenv(ENTRYPOINT_ENV, raising=False)
    assert is_supported_session() is True


def test_empty_env_is_supported(monkeypatch):
    monkeypatch.setenv(ENTRYPOINT_ENV, "")
    assert is_supported_session() is True


# ── 接线：两个 hook 都必须在闸门处短路 ────────────────────────────────────
#
# 判据用「闸门之后的 load_config_ex 有没有被调用」，而不是「stdout 是否为空」：
# 后者在没有匹配笔记时也为空，对闸门零判别力。每条禁用用例都配一条 cli 阳性
# 对照 —— 否则「没调用」既可能是闸门生效，也可能是它在更早处就退出了。

HOOKS = [("prompt_submit_load", "UserPromptSubmit"),
         ("session_start_load", "SessionStart")]


def _run_hook(mod, tmp_path, monkeypatch, entrypoint, event):
    """跑一次 hook.main()，返回 load_config_ex 是否被调用到。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows 上 Path.home() 读它
    monkeypatch.setenv(ENTRYPOINT_ENV, entrypoint)
    reached = []
    orig = mod.load_config_ex

    def spy():
        reached.append(1)
        return orig()

    monkeypatch.setattr(mod, "load_config_ex", spy)
    payload = json.dumps({
        "cwd": str(tmp_path), "session_id": "s1", "prompt_id": "p1",
        "hook_event_name": event, "source": "startup",
        "prompt": "测试 关键词 注入",
    })
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = mod.main()
    assert rc == 0, "hook 必须始终 exit 0（fail-open 不变量）"
    return bool(reached)


@pytest.mark.parametrize("modname,event", HOOKS)
def test_hook_short_circuits_on_sdk_cli(tmp_path, monkeypatch, modname, event):
    """sdk-cli 会话必须在配置加载之前就返回。

    变异验证：删掉 hook 里那段闸门，本用例转红。
    """
    mod = __import__("scripts." + modname, fromlist=["x"])
    assert _run_hook(mod, tmp_path, monkeypatch, "sdk-cli", event) is False, \
        "%s 未在闸门处短路，sdk-cli 轮次仍会被注入并落 metrics" % modname


@pytest.mark.parametrize("modname,event", HOOKS)
def test_hook_proceeds_for_human_cli(tmp_path, monkeypatch, modname, event):
    """阳性对照：cli 会话必须照常往下走 —— 否则上一条的「没调用」毫无意义。"""
    mod = __import__("scripts." + modname, fromlist=["x"])
    assert _run_hook(mod, tmp_path, monkeypatch, "cli", event) is True, \
        "%s 把人类会话也拦掉了（误杀，且完全静默）" % modname
