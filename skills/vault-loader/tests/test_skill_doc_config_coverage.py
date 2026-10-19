"""防复发守卫：SKILL.md 必须文档化 `_SECTIONS` 列出的每个配置段的每一个可配键。

背景：Task 6 停止首跑全量物化 DEFAULT_CONFIG 后，新装用户盘上只剩
`_MINIMAL_STUB` 两键占位——「打开生成的 config.json 就能看到全部可配键」这个
affordance 随之消失。占位里的 `_comment` 明确写着「完整可配键见 SKILL.md」，
而 SKILL.md 是**分发物中唯一**能承载该责任的文档（spec / plan 在
`docs/superpowers/`，被 gitignore、不随插件走）。故新增可配键却不写进
SKILL.md，等于该键对所有用户不可发现。

当前守 `relevance`（本轮召回质量调参的集中地、键最多且新增最频繁，也是 F8
实际漏文档的那一段——16 个键只写了 3 个）与 `metrics`（Task 8 新增的顶层落盘
开关）。其它段如需同等守护，按同一模式扩展 `_SECTIONS` 即可。
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts._config_loader import DEFAULT_CONFIG

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

# 配置表的机器契约：解析与守卫共用这一对锚点界定的区间。
# 没有锚点时，判据只能是「全文任一表格行」，会把「停用逃生阀」等别的表一起算进来
# （实测曾抓到 `VAULT_LOADER_DISABLE=1` 这种根本不是配置键的行）。
_ANCHOR_BEGIN = "<!-- config-table:begin -->"
_ANCHOR_END = "<!-- config-table:end -->"

# 顶层元键不是可配项：占位文件自带，用户不该也不需要配。
_META_KEYS = {"_config_version", "_comment"}
# 用户自定义映射，值域由用户决定，不下钻逐键要求文档。
_OPAQUE_KEYS = {"keyword_to_tags"}

# 显式豁免：键仍在 DEFAULT_CONFIG 里（为兼容旧 config 而保留），但运行时不读，
# 故不要求它出现在「完整可配键清单」里——把废弃键列进清单反而会误导用户去调它。
# 两道防腐见 test_exempt_* ：清单项必须真在论域内、且必须确实没有表格行。
_EXEMPT: dict[str, str] = {
    "user_prompt_submit.min_score": "已废弃，运行时不读（改用 relevance.min_topical_score）",
    "user_prompt_submit.fulltext_threshold":
        "已废弃，运行时不读（改用 relevance.fulltext_topical_threshold）",
    "runtimes.codex.enabled": "已废弃，运行时不读（2.0.0 移除 Codex 支持）",
}


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _flatten(d: dict, prefix: str = "") -> dict:
    """把 DEFAULT_CONFIG 摊平成「全路径键 -> 默认值」。

    必须**下钻到叶子**：旧实现只遍历 `DEFAULT_CONFIG[section]` 的一级键，
    于是 `relevance.confidence_bands.high` 这类二级嵌套键从来没被守过。
    """
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if not prefix and k in _META_KEYS:
            continue
        if isinstance(v, dict) and key not in _OPAQUE_KEYS:
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _config_table(text: str) -> str:
    """取锚点之间的配置表区间；锚点缺失或顺序颠倒即抛错（不静默回落全文）。

    回落全文会让这道守卫在锚点被误删时**继续全绿**，而那正是它要防的失效。
    """
    i, j = text.find(_ANCHOR_BEGIN), text.find(_ANCHOR_END)
    assert i != -1, f"SKILL.md 缺少配置表起始锚点 {_ANCHOR_BEGIN}"
    assert j != -1, f"SKILL.md 缺少配置表结束锚点 {_ANCHOR_END}"
    assert i < j, "配置表锚点顺序颠倒"
    return text[i + len(_ANCHOR_BEGIN):j]


def _has_table_row(table: str, key: str) -> bool:
    """该键在配置表里是否有**表格首列**那一行。

    判据与将来的说明文本解析器**同一个**——旧判据是「反引号键名出现在全文任意
    位置」，宽到一句散文提及即可满足，于是「守卫全绿」与「能取到这一行的说明」
    是两回事。统一判据后这道缝就不存在了。
    """
    return re.search(r"^\|\s*`" + re.escape(key) + r"`\s*\|", table, re.M) is not None


def test_every_config_key_has_a_table_row():
    """**全部**可配键都必须在配置表里有一行，不是只守其中两段。

    旧实现的论域是 `_SECTIONS = ("relevance", "metrics")` —— 实测只覆盖 31/66 键，
    另外 35 个（`scoring`/`session_start`/`user_prompt_submit`/`display`/`runtimes`/
    顶层）新增时没有任何测试会红。变异实证：删掉 `scoring.prompt_keyword_hit`
    那一行，旧守卫全绿。
    """
    table = _config_table(_skill_text())
    missing = [k for k in sorted(_flatten(DEFAULT_CONFIG))
               if k not in _EXEMPT and not _has_table_row(table, k)]

    assert not missing, (
        "以下配置键在 SKILL.md 的配置表里没有对应行：\n  "
        + "\n  ".join(missing)
        + "\n\nSKILL.md 是分发物中唯一的完整可配键清单来源——首跑只写最小占位"
          "（`_MINIMAL_STUB`）后，用户无法再从生成的 config.json 里发现可配键，"
          "占位的 `_comment` 也直接指向 SKILL.md。新增配置键必须同步补一行表格"
          "（全路径键名放首列、给出默认值与一句作用说明）；确属废弃键则登记进 _EXEMPT。"
    )


def test_config_table_anchors_exist_and_are_paired():
    """锚点本身要有守卫：它是解析器与本守卫共用的边界，被误删时必须响亮失败。"""
    text = _skill_text()
    assert text.count(_ANCHOR_BEGIN) == 1, f"{_ANCHOR_BEGIN} 必须恰好出现一次"
    assert text.count(_ANCHOR_END) == 1, f"{_ANCHOR_END} 必须恰好出现一次"
    table = _config_table(text)
    assert len(table) > 500, "锚点之间的配置表区间异常短，锚点位置可能放错了"


def test_judgement_rejects_prose_mention():
    """判据必须是「表格行」，不能是「全文任意位置提及」——本次收紧的核心。

    没有这条时，把 `_has_table_row` 改回旧的 `f"`{key}`" in text` 形态，
    整套用例照样全绿，于是这次改动的唯一目的失去守卫。
    实测的那道缝就是：删掉表格行、只留一句散文提及，旧判据放行而解析器拿不到说明。
    """
    prose = "排障时可以临时关掉 `relevance.use_tag_idf`，见上文说明。\n"
    assert not _has_table_row(prose, "relevance.use_tag_idf"), (
        "散文里的反引号提及不得被当成表格行")
    row = "| `relevance.use_tag_idf` | `true` | 关闭 tag-IDF 加权 |\n"
    assert _has_table_row(row, "relevance.use_tag_idf"), "真正的表格行必须被认出"


def test_exempt_entries_are_real_config_keys():
    """防腐一：豁免项必须真在论域内。

    废弃键将来真被删除后，若豁免项还挂着，它就变成一条永远没人发现的死条目，
    而下一个同名新键会被它无声豁免掉。
    """
    known = set(_flatten(DEFAULT_CONFIG))
    stale = [k for k in _EXEMPT if k not in known]
    assert not stale, (
        f"_EXEMPT 里的这些键已不在 DEFAULT_CONFIG 中，应一并删除：{stale}")


def test_exempt_entries_have_no_table_row():
    """防腐二：豁免项不得已经有表格行。

    补了文档却还挂在豁免清单里时，这个键此后对覆盖率守卫永久隐身——
    删掉它那一行也不会有人发现。
    """
    table = _config_table(_skill_text())
    documented = [k for k in _EXEMPT if _has_table_row(table, k)]
    assert not documented, (
        f"这些键已在配置表里有行，应从 _EXEMPT 移除：{documented}")


def test_guard_actually_reads_skill_md():
    """守护守卫自身：SKILL.md 必须存在且非空，防止读空文件导致断言假绿。"""
    assert SKILL_MD.is_file(), f"SKILL.md 不存在：{SKILL_MD}"
    text = _skill_text()
    assert len(text) > 1000, "SKILL.md 内容异常短，守卫可能读到了错误的文件"
    # 反向自证：一个绝不会出现在配置表里的键名必须被判为「没有表格行」
    assert not _has_table_row(_config_table(text), "__definitely_not_a_real_key__")


def test_skill_md_documents_session_topic_ttl_coupling():
    """F6（整分支终审，2026-09-02）：`state_ttl_hours` 一行必须提及它同时约束
    `relevance.session_topic` 的主题词与提炼失败标记的有效期——`prompt_submit_load.py`
    的 `load_session_topic`/`has_recent_topic_attempt` 都直接读同一个
    `ups_cfg["state_ttl_hours"]`，不写清楚这个耦合，用户改这个值时不知道它还会
    影响主题预热多久重试一次。2.4.0 起这一行讲的是：同一个值约束 LLM 精排（旧键
    `session_topic` 同理）的主题词、首轮点名与精排状态文件；「提炼失败标记」的负缓存已退役，
    判据仍是该行要点名 `session_topic`。"""
    text = _skill_text()
    rows = [line for line in text.splitlines()
            if "user_prompt_submit.state_ttl_hours" in line]
    assert rows, "SKILL.md 未找到 user_prompt_submit.state_ttl_hours 这一行"
    assert "session_topic" in rows[0], (
        f"state_ttl_hours 一行未提及与 session_topic 的耦合：{rows[0]}")


def test_skill_md_session_topic_hit_row_does_not_overclaim():
    """F3（整分支终审，2026-09-02）：`scoring.session_topic_hit` 的文档措辞不得再
    断言"无法单独把笔记拉进召回集，只能锦上添花"——实测该说法只对精度闸门
    （`min_topical_score`）成立，对全文阈值（`fulltext_topical_threshold`）不成立：
    叠加 prompt 关键词已有的 topical 分，主题词可以把笔记推过全文阈值。"""
    text = _skill_text()
    rows = [line for line in text.splitlines() if "`scoring.session_topic_hit`" in line]
    assert rows, "SKILL.md 未找到 scoring.session_topic_hit 这一行"
    assert "只能锦上添花" not in rows[0], f"文档仍残留已被 F3 推翻的表述：{rows[0]}"
    assert "fulltext_topical_threshold" in rows[0], (
        "文档应提及可越过全文阈值这一已知且被测试锁定的行为")


def test_pin_header_names_a_real_config_key():
    """1A-L11：`PIN_HEADER` 是用户可见文案，里面嵌的配置键名必须真实存在于 DEFAULT_CONFIG。"""
    from scripts.prompt_submit_load import PIN_HEADER, PIN_SWITCH_KEY
    assert PIN_SWITCH_KEY in DEFAULT_CONFIG["relevance"]
    assert f"`relevance.{PIN_SWITCH_KEY}: false`" in PIN_HEADER
