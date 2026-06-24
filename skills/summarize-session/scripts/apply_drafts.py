"""合入 auto-drafts/ 下所有草稿到目标文件,成功则删除草稿。"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
from diff_drafts import find_drafts, DraftInfo


@dataclass
class ApplyResult:
    applied: int
    failed: int
    failures: list[tuple[Path, str]]


def _append_to_target(target: Path, body: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        text = target.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + body
        if not text.endswith("\n"):
            text += "\n"
    else:
        text = body
        if not text.endswith("\n"):
            text += "\n"
    target.write_text(text, encoding="utf-8")


def apply_all(drafts_root: Path) -> ApplyResult:
    """合入所有草稿,返回结果。"""
    drafts = find_drafts(drafts_root)
    applied = 0
    failed = 0
    failures: list[tuple[Path, str]] = []

    for info in drafts:
        try:
            _append_to_target(info.target_path, info.body)
            info.path.unlink()
            applied += 1
        except Exception as e:
            failed += 1
            failures.append((info.path, str(e)))

    # 清理空的日期目录
    if drafts_root.exists():
        for date_dir in list(drafts_root.iterdir()):
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                try:
                    date_dir.rmdir()
                except OSError:
                    pass

    return ApplyResult(applied=applied, failed=failed, failures=failures)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="合入草稿到目标文件")
    parser.add_argument("--drafts-root",
                        default=str(Path.home() / ".claude/skills/summarize-session/auto-drafts"))
    args = parser.parse_args()
    result = apply_all(Path(args.drafts_root))
    print(f"✅ 合入 {result.applied} 项,❌ 失败 {result.failed} 项")
    for path, err in result.failures:
        print(f"  - 失败: {path.name} — {err}")


if __name__ == "__main__":
    main()
