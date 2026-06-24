"""扫描草稿、渲染统一 diff,供 --review-drafts 使用。"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n+", re.DOTALL)


@dataclass
class DraftInfo:
    path: Path
    target_path: Path
    source_session: str
    source_date: str
    type: str
    body: str


def _parse_draft(path: Path) -> DraftInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm_text = m.group(1)
    body = text[m.end():]
    fm = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    target = fm.get("target_path", "")
    if not target:
        return None
    return DraftInfo(
        path=path,
        target_path=Path(target),
        source_session=fm.get("source_session", ""),
        source_date=fm.get("source_date", ""),
        type=fm.get("type", ""),
        body=body,
    )


def find_drafts(drafts_root: Path) -> list[DraftInfo]:
    """扫描 auto-drafts/*/*.draft.md,解析所有草稿。"""
    if not drafts_root.exists():
        return []
    out = []
    for f in sorted(drafts_root.glob("*/*.draft.md")):
        info = _parse_draft(f)
        if info is not None:
            out.append(info)
    return out


def render_diff(info: DraftInfo) -> str:
    """渲染目标文件追加草稿正文后的 unified diff。"""
    if info.target_path.exists():
        try:
            original = info.target_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
    else:
        original = ""
    new_content = original
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += "\n" + info.body
    if not new_content.endswith("\n"):
        new_content += "\n"

    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"{info.target_path} (current)",
        tofile=f"{info.target_path} (after apply)",
        lineterm="",
    )
    return "".join(diff_lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="渲染所有草稿的 diff")
    parser.add_argument("--drafts-root",
                        default=str(Path.home() / ".claude/skills/summarize-session/auto-drafts"))
    args = parser.parse_args()
    drafts = find_drafts(Path(args.drafts_root))
    if not drafts:
        print("(no drafts pending)")
        return
    print(f"=== {len(drafts)} drafts pending ===\n")
    by_type: dict[str, list[DraftInfo]] = {}
    for d in drafts:
        by_type.setdefault(d.type, []).append(d)
    for t, items in by_type.items():
        print(f"## {t} ({len(items)} drafts)")
        for info in items:
            print(f"\n### {info.path.name}  (来自会话 {info.source_session}, {info.source_date})")
            print(f"目标: {info.target_path}")
            print("```diff")
            print(render_diff(info))
            print("```\n")
    print("\n直接编辑或删除 auto-drafts/ 下的草稿,然后运行 /summarize-session --apply-drafts 合入")


if __name__ == "__main__":
    main()
