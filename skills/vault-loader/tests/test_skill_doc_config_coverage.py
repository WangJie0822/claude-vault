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
