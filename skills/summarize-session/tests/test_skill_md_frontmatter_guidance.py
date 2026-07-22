# -*- coding: utf-8 -*-
"""守卫：keywords 要求必须出现在 SKILL.md 第四步的执行路径上。

背景：规范此前只存在于 references/note-format.md，SKILL.md 仅有一行软引用且
位置在写笔记操作之后，导致实际执行率 16%。本测试守护「要求在执行路径上」这一
结构性属性，而不仅仅是「文档里提过 keywords」。
"""
from pathlib import Path

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"


def _lines_without_comments(text: str) -> list[str]:
    """滤掉 markdown 注释行，避免守卫命中解释性文字而非真要求。"""
    return [l for l in text.splitlines() if not l.lstrip().startswith("<!--")]


def test_keywords_required_inside_step_four() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    lines = _lines_without_comments(text)

    step4_idx = next(i for i, l in enumerate(lines) if l.startswith("### 第四步"))
    step5_idx = next(i for i, l in enumerate(lines) if l.startswith("### 第五步"))
    assert step4_idx < step5_idx

    step4 = "\n".join(lines[step4_idx:step5_idx])
    assert "keywords" in step4, "第四步（写笔记的执行路径）必须就地写明 keywords 要求"


def test_keywords_requirement_precedes_note_creation() -> None:
    """要求必须出现在实际创建笔记的操作之前，否则等同于没写。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    lines = _lines_without_comments(text)
    step4_idx = next(i for i, l in enumerate(lines) if l.startswith("### 第四步"))
    step5_idx = next(i for i, l in enumerate(lines) if l.startswith("### 第五步"))
    step4 = lines[step4_idx:step5_idx]

    kw_pos = next((i for i, l in enumerate(step4) if "keywords" in l), None)
    create_pos = next((i for i, l in enumerate(step4) if "obsidian_cli.py" in l and "create" in l), None)
    assert kw_pos is not None, "第四步内未找到 keywords 要求"
    assert create_pos is not None, "第四步内未找到 create 操作"
    assert kw_pos < create_pos, "keywords 要求必须出现在 create 操作之前"
