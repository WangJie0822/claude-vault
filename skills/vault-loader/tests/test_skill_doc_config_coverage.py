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

from pathlib import Path

from scripts._config_loader import DEFAULT_CONFIG

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

# 需要被 SKILL.md 逐键文档化的配置段
_SECTIONS = ("relevance", "metrics")


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _mentions(text: str, section: str, key: str) -> bool:
    """键是否在 SKILL.md 中出现——接受「反引号裸键」与「反引号带段前缀」两种形式。

    两种都算数是因为文档里两种写法都自然：表格里常写 `relevance.use_tag_idf`
    做完整路径，正文里提到某个键时常只写 `use_tag_idf`。只要任一形式出现，
    读者就能检索到该键。
    """
    return f"`{key}`" in text or f"`{section}.{key}`" in text


def test_skill_md_documents_all_relevance_keys():
    text = _skill_text()
    missing: list[str] = []
    for section in _SECTIONS:
        for key in DEFAULT_CONFIG[section]:
            if not _mentions(text, section, key):
                missing.append(f"{section}.{key}")

    assert not missing, (
        "SKILL.md 未文档化以下配置键：\n  "
        + "\n  ".join(missing)
        + "\n\nSKILL.md 是分发物中唯一的完整可配键清单来源——首跑只写最小占位"
          "（`_MINIMAL_STUB`）后，用户无法再从生成的 config.json 里发现可配键，"
          "占位的 `_comment` 也直接指向 SKILL.md。新增配置键必须同步文档化"
          "（写进 SKILL.md「配置」节的对应表格，给出默认值与一句作用说明）。"
    )


def test_guard_actually_reads_skill_md():
    """守护守卫自身：SKILL.md 必须存在且非空，防止读空文件导致断言假绿。"""
    assert SKILL_MD.is_file(), f"SKILL.md 不存在：{SKILL_MD}"
    text = _skill_text()
    assert len(text) > 1000, "SKILL.md 内容异常短，守卫可能读到了错误的文件"
    # 反向自证：一个绝不会出现在文档里的键名必须被判为「未文档化」
    assert not _mentions(text, "relevance", "__definitely_not_a_real_key__")


def test_skill_md_documents_session_topic_ttl_coupling():
    """F6（整分支终审，2026-09-02）：`state_ttl_hours` 一行必须提及它同时约束
    `relevance.session_topic` 的主题词与提炼失败标记的有效期——`prompt_submit_load.py`
    的 `load_session_topic`/`has_recent_topic_attempt` 都直接读同一个
    `ups_cfg["state_ttl_hours"]`，不写清楚这个耦合，用户改这个值时不知道它还会
    影响主题预热多久重试一次。"""
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
    rows = [line for line in text.splitlines() if "scoring.session_topic_hit" in line]
    assert rows, "SKILL.md 未找到 scoring.session_topic_hit 这一行"
    assert "只能锦上添花" not in rows[0], f"文档仍残留已被 F3 推翻的表述：{rows[0]}"
    assert "fulltext_topical_threshold" in rows[0], (
        "文档应提及可越过全文阈值这一已知且被测试锁定的行为")
