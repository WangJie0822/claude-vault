# -*- coding: utf-8 -*-
"""读端：配置清单。

设计依据来自一次七路评审（本地四维 + 跨模型三路）对首版设计的否决，几条要害都
在这里有对应用例：

- **读端绝不能有写副作用**：`_config_loader.py` 的 `load_config` 在文件缺失时会
  `mkdir` + `write_text` 写占位。「只看一眼配置」变成在用户机器上建文件，是本
  skill 最不该犯的错。`test_reading_never_writes_anything` 钉住这条。
- **一个来源不扛两个语义**：「是否显式设置」只看原始 JSON 里键在不在；「当前生效值」
  用 vault-loader 自己的 `_deep_merge` 合并得出。两者分开取，不互相推断。
- **默认值不能靠 import 拿**：跨 skill import 实测 ModuleNotFoundError，且仓库
  明文拒绝硬性跨 skill 导入。改用 importlib 按绝对路径加载。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import config_inventory as inv

SKILL_ROOT = Path(__file__).resolve().parents[1]


def _write_config(home: Path, data: dict) -> Path:
    p = home / ".context-vault" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── 默认值来源 ─────────────────────────────────────────────────────────
def test_defaults_loaded_without_importing_scripts_package():
    """默认值按路径加载，且不把顶层包名 `scripts` 注册进 sys.modules。

    直接 `import scripts._config_loader` 会与 vault-loader 争用顶层包名（实测
    ModuleNotFoundError），这条钉住我们没走那条路。
    """
    before = set(sys.modules)
    defaults = inv.load_default_config()
    assert isinstance(defaults, dict) and defaults
    assert "relevance" in defaults and "metrics" in defaults
    newly = set(sys.modules) - before
    assert "scripts" not in newly, "不得注册顶层包名 scripts（会与 vault-loader 争用）"


def test_flatten_reaches_nested_leaves():
    flat = inv.flatten(inv.load_default_config())
    assert "relevance.confidence_bands.high" in flat, "必须下钻到嵌套叶子键"
    assert "_config_version" not in flat, "元键不是可配项"
    assert "keyword_to_tags" in flat, "用户自定义映射整体呈现，不下钻"


# ── 核心：读端无写副作用 ────────────────────────────────────────────────
def test_reading_never_writes_anything(_isolate_home):
    """配置不存在时，列清单不得在用户机器上创建任何文件或目录。

    `load_config` 会写 `_MINIMAL_STUB` 占位；读端一旦图省事调它，
    「我只是想看看有哪些配置项」就变成了写盘。
    """
    home = _isolate_home
    before = {p for p in home.rglob("*")}
    items = inv.build_inventory(home=home)
    after = {p for p in home.rglob("*")}
    assert after == before, f"读端产生了写副作用：新增 {sorted(after - before)}"
    assert items, "即使没有配置文件，也应列出全部可配键（全部走默认）"
    assert all(it["source"] == "default" for it in items if it["status"] == "active")


# ── 显式 vs 默认 ───────────────────────────────────────────────────────
def test_explicit_and_default_are_distinguished(_isolate_home):
    _write_config(_isolate_home, {"relevance": {"min_topical_score": 9}})
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}

    explicit = by_key["relevance.min_topical_score"]
    assert explicit["source"] == "explicit"
    assert explicit["current"] == 9
    assert explicit["default"] == 4, "默认值必须仍如实显示，供用户判断改了什么"

    untouched = by_key["relevance.use_tag_idf"]
    assert untouched["source"] == "default"
    assert untouched["current"] == untouched["default"]


def test_current_value_uses_the_same_merge_as_runtime(_isolate_home):
    """「当前生效值」必须用 vault-loader 自己的 `_deep_merge` 算，不能自己写一套。

    自己实现合并 = 第二份判据，与运行时漂移时没有任何信号。
    """
    _write_config(_isolate_home, {"relevance": {"confidence_bands": {"high": 9}}})
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}
    # 嵌套 dict 的部分覆盖：只给了 high，同段其它键仍取默认
    assert by_key["relevance.confidence_bands.high"]["current"] == 9
    assert by_key["relevance.confidence_bands.high"]["source"] == "explicit"


# ── 状态标注 ───────────────────────────────────────────────────────────
def test_deprecated_keys_are_flagged_not_hidden(_isolate_home):
    """废弃键要列出但单独标注。

    隐藏它们会让「我明明配了 min_score 为什么没效果」永远查不出来；
    混在生效项里则会误导用户去调一个运行时不读的值。
    """
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}
    dep = by_key["user_prompt_submit.min_score"]
    assert dep["status"] == "deprecated"
    assert dep["desc"], "废弃键也要说明为什么废弃、改用哪个键"


def test_retired_codex_switch_is_flagged_deprecated(_isolate_home):
    """2.0.0 移除 Codex 后 `runtimes.codex.enabled` 失效；迁移脚本给每台迁移过的机器都写过它，
    必须在清单里以「已废弃」出现，而不是消失或混在生效项里。"""
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}
    dep = by_key["runtimes.codex.enabled"]
    assert dep["status"] == "deprecated"
    # 1.x 里该键只关掉被误判成 Codex 的 SessionStart；等价替代只有 session_start.enabled。
    # runtimes.claude.enabled 会把 SessionStart 与 UPS 两个入口一起关掉，不得列为替代。
    assert "session_start.enabled" in dep["desc"]
    assert "runtimes.claude.enabled" not in dep["desc"]


def test_keys_without_documentation_are_marked_not_blank(_isolate_home):
    """无说明的键标注为「无说明」而非空白——空白读起来像「没说明是正常的」。"""
    items = inv.build_inventory(home=_isolate_home)
    for it in items:
        assert it["desc"], f"{it['key']} 的说明为空，应至少标注无说明"


def test_privacy_knobs_are_flagged(_isolate_home):
    """三个 int 键控制「多少篇笔记发往外部模型端点」，必须带隐私标记。

    它们的类型是普通 int，类型校验对其完全无效；不标记就没有任何东西
    提醒用户「调大这个值等于多外发」。
    """
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}
    for k in ("relevance.session_topic_top_n",
              "relevance.session_topic_project_top_k",
              "relevance.session_topic_pool_max"):
        assert by_key[k].get("privacy") is True, f"{k} 未标记为隐私旋钮"
    assert by_key["relevance.min_topical_score"].get("privacy") is not True


def test_readonly_keys_are_flagged(_isolate_home):
    """首版只读的键要标出来，否则用户会以为能改而反复尝试。"""
    by_key = {it["key"]: it for it in inv.build_inventory(home=_isolate_home)}
    assert by_key["relevance.min_topical_score"].get("writable") is True
    # obsidian_cli / work_log_dir 不在 DEFAULT_CONFIG 里，作为额外条目补入且只读
    extra = {it["key"] for it in inv.build_inventory(home=_isolate_home)
             if it.get("writable") is False}
    assert "obsidian_cli.enabled" in extra and "work_log_dir" in extra


# ── 端到端（真实入口）─────────────────────────────────────────────────
def test_cli_json_output_is_parseable(_isolate_home, monkeypatch):
    env = {**dict(__import__("os").environ),
           "HOME": str(_isolate_home), "USERPROFILE": str(_isolate_home)}
    r = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / "config_inventory.py"), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    data = json.loads(r.stdout)
    assert isinstance(data, list) and len(data) > 60


def test_cli_human_output_mentions_counts(_isolate_home):
    env = {**dict(__import__("os").environ),
           "HOME": str(_isolate_home), "USERPROFILE": str(_isolate_home)}
    r = subprocess.run(
        [sys.executable, str(SKILL_ROOT / "scripts" / "config_inventory.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    assert "显式" in r.stdout and "默认" in r.stdout
