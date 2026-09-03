"""Detect an enabled legacy plugin before the renamed plugin takes ownership."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    # 不能让它成为硬依赖：wrapper 探到的解释器可能是 3.10 及以下（macOS Xcode CLT
    # 的 python3 是 3.9、Ubuntu 20.04 是 3.8）。硬 import 会让整个模块加载失败，
    # 连带把事件去重、共存检测、runtime 命名空间三项一起静默停用。
    tomllib = None                   # type: ignore[assignment]

LEGACY_PLUGIN_NAME = "claude-vault"
# 祖先遍历的深度上限。不设上限时，任意一个共享上层目录（工作区根、甚至盘符根）
# 里的 `.claude/settings.json` 会影响其下**全部**项目；配合下面「读不出不再让路」，
# 影响面被限制在可解释的范围内。
_MAX_PARENTS = 8

# tomllib 缺席时的降级判据：只认 `[plugins."<name>@<market>"]` 段内的 `enabled = true`。
_TOML_SECTION_RE = re.compile(r'^\s*\[plugins\."([^"\]]+)"\]\s*$', re.M)
_TOML_ENABLED_RE = re.compile(r'^\s*enabled\s*=\s*true\s*$', re.M)


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


def _codex_enabled_from_text(text: str) -> bool:
    """tomllib 缺席时的降级解析：段名匹配旧插件且段内出现 `enabled = true`。"""
    for match in _TOML_SECTION_RE.finditer(text):
        if match.group(1).split("@", 1)[0] != LEGACY_PLUGIN_NAME:
            continue
        nxt = _TOML_SECTION_RE.search(text, match.end())
        body = text[match.end():nxt.start() if nxt else len(text)]
        if _TOML_ENABLED_RE.search(body):
            return True
    return False


def _codex_legacy_enabled(home: Path) -> CoexistCheck:
    path = home / ".codex" / "config.toml"
    if not path.is_file():
        return CoexistCheck(False, "")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return CoexistCheck(False, f"无法读取 {path}，已跳过")
    if tomllib is None:
        return CoexistCheck(_codex_enabled_from_text(text), "")
    try:
        data = tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        # 与 Claude 侧同一口径：解析失败只跳过并说明，不冒充「检测到启用」。
        return CoexistCheck(_codex_enabled_from_text(text),
                            f"{path} 解析失败，已按文本形态降级判断")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return CoexistCheck(False, "")
    hit = any(
        isinstance(key, str) and key.split("@", 1)[0] == LEGACY_PLUGIN_NAME
        and isinstance(value, dict) and value.get("enabled") is True
        for key, value in plugins.items()
    )
    return CoexistCheck(hit, "")


def check_legacy_plugin(runtime: str, *, home: Path | None = None,
                        cwd: Path | None = None) -> CoexistCheck:
    base = home or Path.home()
    if runtime == "claude":
        return _claude_legacy_enabled(base, cwd)
    if runtime == "codex":
        return _codex_legacy_enabled(base)
    return CoexistCheck(False, "")


def legacy_plugin_enabled(runtime: str, *, home: Path | None = None,
                          cwd: Path | None = None) -> bool:
    """向后兼容的布尔入口（旧调用点与既有用例仍在用）。"""
    return check_legacy_plugin(runtime, home=home, cwd=cwd).yield_ownership
