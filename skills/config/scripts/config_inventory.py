#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置清单：把 context-vault 的全部可配项连同「当前值 / 默认值 / 来源 / 说明」列出来。

**只读**。这一点是硬约束而不是风格：`_config_loader.load_config()` 在配置缺失时会
`mkdir` + `write_text` 写入 `_MINIMAL_STUB` 占位，读端一旦调它，「我只是想看看有哪些
配置项」就变成了在用户机器上建文件。本模块因此只 `json.loads` 原始文件，文件不存在
就报「未配置」，绝不创建。

三个数据来源各自单一，不互相推断（七路评审里 glm 那条：一个来源不该扛两个语义）：

| 数据 | 来源 |
|---|---|
| 可配键全集 + 默认值 | vault-loader 的 `DEFAULT_CONFIG`，按**绝对路径**加载 |
| 是否显式设置 | 原始 config.json 里该键在不在 |
| 当前生效值 | 用 vault-loader 自己的 `_deep_merge` 合并，保证与运行时逐字一致 |
| 说明文本 | vault-loader `SKILL.md` 锚点内的配置表（有守卫强制全键覆盖） |

**默认值不用 `import scripts._config_loader` 拿**：vault-loader 的 `scripts/` 是 regular
package，两个 skill 同时声明顶层包名 `scripts` 时归谁取决于 sys.path 顺序（实测
ModuleNotFoundError）；且仓库在 `_config_loader.py` 里明文写过「不引入硬性跨 skill 导入
依赖」。改用 importlib 按路径加载，不注册顶层包名。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_SKILLS = _HERE.parents[2]                 # <root>/skills
_PLUGIN_ROOT = _HERE.parents[3]            # <root>
_VL = _SKILLS / "vault-loader"

# 与 vault-loader SKILL.md 的机器契约一致；由 tests/test_contract.py 钉住两处不漂移。
ANCHOR_BEGIN = "<!-- config-table:begin -->"
ANCHOR_END = "<!-- config-table:end -->"

_META_KEYS = {"_config_version", "_comment"}
_OPAQUE_KEYS = {"keyword_to_tags"}

# 运行时不读的键：列出但单独标注。隐藏会让「我配了为什么没效果」永远查不出来。
DEPRECATED: dict[str, str] = {
    "user_prompt_submit.min_score": "已废弃，运行时不读；改用 `relevance.min_topical_score`",
    "user_prompt_submit.fulltext_threshold":
        "已废弃，运行时不读；改用 `relevance.fulltext_topical_threshold`",
    "runtimes.codex.enabled":
        "已失效（2.0.0 移除 Codex 支持），运行时不读；若曾用它关掉启动注入，改用 "
        "`session_start.enabled`",
}

# 已被新键取代、仍兼容读取的旧键（spec 2026-10-13 §5.3）。与 vault-loader `_fine_config.SUPERSEDED`
# 逐一相同（守卫 tests/test_inventory.py::test_superseded_matches_vault_loader）。
# 与 DEPRECATED 的区别：废弃键运行时不读；被取代的键仍然生效（新键没有显式写时取它的值），
# 所以清单照常显示当前值，只是不可写——写入要改写新键，config_set 会一并删掉旧键。
SUPERSEDED: dict[str, str] = {
    "relevance.session_topic": "relevance.fine_rank",
    "relevance.session_pin_max": "relevance.fine_pin_max",
}

# 控制「多少篇笔记被发往外部模型端点」的旋钮。类型都是普通 int，类型校验对它们
# 完全无效 —— 不标记就没有任何东西提醒用户「调大这个值等于多外发」。
PRIVACY_KNOBS = frozenset({
    "relevance.session_topic_top_n",
    "relevance.session_topic_project_top_k",
    "relevance.session_topic_pool_max",
    # 2.4.0 精排（spec fine-rank-per-turn §5.1「隐私旋钮」列）：决定每次调用送多少、送不送、送哪些
    "relevance.fine_catalog",
    "relevance.fine_catalog_max_chars",
    "relevance.fine_recent_prompts",
    "relevance.fine_session_max_bytes",
    "relevance.fine_exclude_tags",
})

# 首版只读：不在 DEFAULT_CONFIG 里（无默认值、无类型判据），且 obsidian_cli 只认
# canonical 而写端走 resolve_config_path()，legacy 安装上两者分叉。
READONLY_EXTRA: dict[str, str] = {
    "work_log_dir": "工作日志目录；由 summarize-session 读取。不在 DEFAULT_CONFIG 中，本工具只读不写",
    "obsidian_cli.enabled": "是否启用 Obsidian CLI；**只认 canonical 配置文件**，"
                            "与本工具的路径解析可能分叉，故只读不写",
}

_UNDOCUMENTED = "（无说明：该键未出现在 vault-loader SKILL.md 的配置表里）"


def _load_module(path: Path, name: str):
    """按绝对路径加载单个模块，不注册顶层包名。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法按路径加载模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config_loader():
    return _load_module(_VL / "scripts" / "_config_loader.py", "_cv_config_loader")


def load_default_config() -> dict:
    """vault-loader 的 DEFAULT_CONFIG（深拷贝，调用方改不坏源）。"""
    from copy import deepcopy
    return deepcopy(_config_loader().DEFAULT_CONFIG)


def deep_merge(default: dict, override: dict) -> dict:
    """复用 vault-loader 自己的合并实现 —— 自己写一套就是第二份判据。"""
    return _config_loader()._deep_merge(default, override)


def flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    """摊平成「全路径键 -> 值」，下钻到叶子；元键剔除，用户自定义映射整体保留。"""
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if not prefix and k in _META_KEYS:
            continue
        if isinstance(v, dict) and key not in _OPAQUE_KEYS:
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _config_path(home: Path | None) -> Path:
    """与 vault-loader 运行时同一套路径解析（canonical 优先、legacy 回落）。"""
    if (_PLUGIN_ROOT / "context_vault" / "runtime.py").is_file():
        if str(_PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_ROOT))
    from context_vault.paths import resolve_config_path
    path, _fresh = resolve_config_path(home)
    return path


def read_raw_config_strict(home: Path | None = None) -> tuple[dict, Path, bool, bool]:
    """读原始配置，并分清「读不出」与「空」。返回 (原始 dict, 路径, 文件是否存在, 是否读出了 JSON 对象)。

    `utf-8-sig`：PowerShell 5.1 的 `Out-File -Encoding utf8` 与部分编辑器写出带 BOM 的 UTF-8，按
    `utf-8` 读会在首字符失败、被当成损坏（vault-loader 的 `_config_loader` 早已用 utf-8-sig）。
    写端（config_set）据第四项拒绝覆盖读不出的文件。**绝不创建文件。**
    """
    path = _config_path(home)
    if not path.is_file():
        return {}, path, False, True
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, path, True, False
    if not isinstance(data, dict):
        return {}, path, True, False
    return data, path, True, True


def read_raw_config(home: Path | None = None) -> tuple[dict, Path, bool]:
    """读原始配置。**绝不创建文件**；不存在或读不出一律返回空 dict（要分清两者用 read_raw_config_strict）。

    返回 (原始 dict, 路径, 文件是否存在)。
    """
    data, path, exists, _readable = read_raw_config_strict(home)
    return data, path, exists


def _descriptions() -> dict[str, str]:
    """从 vault-loader SKILL.md 的**锚点区间内**解析「键 -> 说明」。

    只在锚点内解析：全文解析会把「停用逃生阀」等别的表一起抓进来
    （实测抓到过 `VAULT_LOADER_DISABLE=1` 这种根本不是配置键的行）。
    """
    md = _VL / "SKILL.md"
    if not md.is_file():
        return {}
    text = md.read_text(encoding="utf-8")
    i, j = text.find(ANCHOR_BEGIN), text.find(ANCHOR_END)
    if i == -1 or j == -1 or i >= j:
        return {}
    table = text[i + len(ANCHOR_BEGIN):j]
    out: dict[str, str] = {}
    for m in re.finditer(r"^\|\s*`([^`|]+)`\s*\|([^|]*)\|\s*(.+?)\s*\|\s*$", table, re.M):
        out[m.group(1).strip()] = m.group(3).strip()
    return out


def build_inventory(home: Path | None = None) -> list[dict]:
    """生成配置清单。纯读，无任何写副作用。"""
    defaults = load_default_config()
    raw, path, exists = read_raw_config(home)
    merged = deep_merge(defaults, raw) if raw else defaults

    flat_default = flatten(defaults)
    flat_current = flatten(merged)
    flat_explicit = flatten(raw) if raw else {}
    desc = _descriptions()

    items: list[dict] = []
    for key in sorted(flat_default):
        deprecated = key in DEPRECATED
        superseded = key in SUPERSEDED
        text = DEPRECATED[key] if deprecated else (desc.get(key) or _UNDOCUMENTED)
        if superseded:
            text = (f"已由 `{SUPERSEDED[key]}` 取代，当前仍生效"
                    f"（新键没有显式写时取这里的值）。{text}")
        items.append({
            "key": key,
            "current": flat_current.get(key, flat_default[key]),
            "default": flat_default[key],
            "source": "explicit" if key in flat_explicit else "default",
            "desc": text,
            "status": "deprecated" if deprecated else "superseded" if superseded else "active",
            "privacy": key in PRIVACY_KNOBS,
            "writable": not deprecated and not superseded,
            "config_file": str(path),
        })

    for key, why in READONLY_EXTRA.items():
        items.append({
            "key": key,
            "current": flat_explicit.get(key, None),
            "default": None,
            "source": "explicit" if key in flat_explicit else "unset",
            "desc": why,
            "status": "external",
            "privacy": False,
            "writable": False,
            "config_file": str(path),
        })

    return items


def _fmt(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def render(items: list[dict], keyword: str | None = None, only_diff: bool = False) -> str:
    rows = items
    if only_diff:
        rows = [r for r in rows if r["source"] == "explicit"]
    if keyword:
        k = keyword.lower()
        rows = [r for r in rows if k in r["key"].lower() or k in r["desc"].lower()]

    n_explicit = sum(1 for r in items if r["source"] == "explicit")
    n_active = sum(1 for r in items if r["status"] == "active")
    path = items[0]["config_file"] if items else "(未知)"

    out = [f"配置文件：{path}",
           f"共 {n_active} 个可配项，其中 **显式设置 {n_explicit} 个**，其余走默认值。", ""]
    if not rows:
        out.append("（没有匹配的配置项）")
        return "\n".join(out)

    for r in rows:
        mark = []
        if r["source"] == "explicit":
            mark.append("已显式设置")
        if r["status"] == "deprecated":
            mark.append("⚠️ 已废弃")
        if r["status"] == "superseded":
            mark.append("↪ 已被取代（仍生效）")
        if r["status"] == "external":
            mark.append("外部键·只读")
        if r["privacy"]:
            mark.append("🔒 隐私旋钮（控制外发篇数）")
        if not r["writable"] and r["status"] not in ("deprecated", "superseded"):
            mark.append("只读")
        tag = f"  [{' · '.join(mark)}]" if mark else ""
        out.append(f"- {r['key']}{tag}")
        out.append(f"    当前 {_fmt(r['current'])}    默认 {_fmt(r['default'])}")
        out.append(f"    {r['desc']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="列出 context-vault 的全部配置项")
    ap.add_argument("keyword", nargs="?", help="只看键名或说明里含该词的项")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供编排层精确取值）")
    ap.add_argument("--diff", action="store_true", help="只列已显式设置的项")
    args = ap.parse_args(argv)

    items = build_inventory()
    if args.json:
        rows = items
        if args.diff:
            rows = [r for r in rows if r["source"] == "explicit"]
        if args.keyword:
            k = args.keyword.lower()
            rows = [r for r in rows if k in r["key"].lower() or k in r["desc"].lower()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render(items, args.keyword, args.diff))
    return 0


if __name__ == "__main__":
    # 输出含 🔒、− 与中文路径：控制台编码不是 UTF-8（中文 Windows 默认 GBK）时 print 会崩（同 vault-loader migrate_config）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 — 被替换成不支持 reconfigure 的流时保持原样
            pass
    sys.exit(main())
