"""Detect an enabled legacy plugin before the renamed plugin takes ownership.

2.0.0 起只检查 Claude Code 的 settings；1.x 另读 `~/.codex/config.toml` 的那一半已随
Codex 支持一并移除（连同为 Python < 3.11 准备的 tomllib 降级解析）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

LEGACY_PLUGIN_NAME = "claude-vault"
# 祖先遍历的深度上限。不设上限时，任意一个共享上层目录（工作区根、甚至盘符根）
# 里的 `.claude/settings.json` 会影响其下**全部**项目；配合下面「读不出不再让路」，
# 影响面被限制在可解释的范围内。
_MAX_PARENTS = 8


class CoexistCheck(NamedTuple):
    """共存检测结果。

    `yield_ownership` 为 True 时调用方应让路（暂停注入）。`note` 是给用户看的
    补充说明——**「读不出某层配置」与「确实检测到旧插件启用」必须分开表达**：
    旧实现把前者也说成后者，用户会去找一个根本不存在的插件。
    """
    yield_ownership: bool
    note: str = ""


def _iter_claude_settings(home: Path, cwd: Path | None):
    yield home / ".claude" / "settings.json"
    # 用户级同样有 settings.local.json。漏掉它时，若用户在那里启用了旧插件，
    # 共存检测完全看不见 —— 两个插件会同时注入，正是本机制要防的事。
    yield home / ".claude" / "settings.local.json"
    if cwd is None:
        return
    current = cwd.resolve() if cwd.exists() else cwd.absolute()
    for depth, parent in enumerate((current, *current.parents)):
        if depth >= _MAX_PARENTS:
            break
        yield parent / ".claude" / "settings.json"
        yield parent / ".claude" / "settings.local.json"


def _claude_legacy_enabled(home: Path, cwd: Path | None = None) -> CoexistCheck:
    unreadable: list[str] = []
    for path in dict.fromkeys(_iter_claude_settings(home, cwd)):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            # **读不出 != 旧插件启用。** 旧实现在此 `return True`，于是任意一层
            # （常是手工编辑、被 gitignore 的 settings.local.json）语法错误就能让
            # 整个插件停摆，还附一条「检测到旧 claude-vault 仍启用」的**事实错误**
            # 提示，把用户引向一个不存在的插件。Claude Code 自己也解析不了这份
            # 配置，因此其中的插件同样不会被它启用——跳过该层才是与宿主一致的行为。
            unreadable.append(str(path))
            continue
        enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(enabled, dict) and any(
            isinstance(key, str) and key.split("@", 1)[0] == LEGACY_PLUGIN_NAME
            and value is True
            for key, value in enabled.items()
        ):
            return CoexistCheck(True, "")
    if unreadable:
        return CoexistCheck(False, "以下配置无法解析，已跳过：" + "、".join(unreadable[:3]))
    return CoexistCheck(False, "")


def check_legacy_plugin(*, home: Path | None = None,
                        cwd: Path | None = None) -> CoexistCheck:
    return _claude_legacy_enabled(home or Path.home(), cwd)


def legacy_plugin_enabled(*, home: Path | None = None,
                          cwd: Path | None = None) -> bool:
    """布尔便捷入口（doctor 与既有用例在用）。与 `check_legacy_plugin` 一样只收关键字
    参数：1.x 按位置传 runtime 的调用会 TypeError。"""
    return check_legacy_plugin(home=home, cwd=cwd).yield_ownership
