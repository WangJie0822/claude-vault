# tests/test_skill_script_paths.py
"""防复发守卫：分发的 skill 文档不得引用退役源脚本目录。

背景：插件化后，源 skill（~/.claude/skills/<name>/）退役、只剩 runtime 态，
真脚本只在版本钉死的插件 cache 里。若 SKILL.md / references 仍写
`~/.claude/skills/<name>/scripts/X.py`，LLM 执行时必 No such file（那里只剩
__pycache__）。本守卫扫描所有分发的 skill markdown，命中即 fail，防止此类
死路径回归。

捕获两类死路径形式：
1. **退役源绝对路径** `~/.claude/skills/<skill>/scripts/`（含 `$HOME` 写法）——
   只锚定 `.../scripts/` 子路径，runtime 态引用（如 config.json，
   不含 `/scripts/`）天然不触发；cache-glob 定位器
   （`~/.claude/plugins/cache/.../scripts`，前缀是 `.claude/plugins/` 非
   `.claude/skills/`）也不触发。
2. **相对脚本调用** `python3 scripts/X.py`——假设 cwd 是脚本目录（cwd 不保证，
   在错误 cwd 跑必失败）。分发 skill 文档应统一用 cache-glob 定位器 `$SS`。

约定：
- 扫描范围限定 `skills/**/*.md`：不扫 docs/（MIGRATION.md 故意保留旧路径作迁移
  对照）、不扫仓库根 tests/ 自身。

已知局限（非本守卫职责，故意不覆盖；写明以免误以为全覆盖）：
- 跨行续写（`~/.claude/skills/...` 与 `scripts/` 分两行）——只做逐行匹配。
- 变量拼接（`BASE=~/.claude/skills/...; $BASE/scripts/`）——执行行无字面前缀。
这两类需运行时验证；本守卫只钉住「单行字面引用退役脚本路径」这一最高频回归源。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 形式 1——退役源绝对路径：`~/.claude/skills/<skill>/scripts/`，亦覆盖 $HOME 写法。
# <skill> 用 [^/\s]+：不跨 `/`、不跨空白，故 `.../skills/X/scripts/` 连续才命中。
_DEAD_SCRIPT_PATH = re.compile(r"(?:~|\$HOME)/\.claude/skills/[^/\s]+/scripts/")
# 形式 2——相对脚本调用：`python3 scripts/X.py` / `python scripts/X.py`（假设 cwd
# 是脚本目录，cwd 不保证）。不与 cache-glob 定位器 `python3 "$SS/X.py"` 冲突。
_REL_SCRIPT_CALL = re.compile(r"\bpython3?\s+scripts/")


def _scan_md_files() -> list[Path]:
    skills_dir = ROOT / "skills"
    return sorted(skills_dir.rglob("*.md"))


def test_no_retired_source_script_paths_in_skill_docs():
    """skills/**/*.md 任一行含 `~/.claude/skills/<x>/scripts/` 即视为死路径回归。"""
    violations: list[str] = []
    for md in _scan_md_files():
        rel = md.relative_to(ROOT).as_posix()
        text = md.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _DEAD_SCRIPT_PATH.search(line) or _REL_SCRIPT_CALL.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    assert not violations, (
        "分发 skill 文档引用了退役源脚本目录 `~/.claude/skills/<x>/scripts/`"
        "（插件化后该目录只剩 __pycache__，LLM 执行必 No such file）。"
        "请改用 cache-glob 定位器 `SS=$(ls -d "
        "~/.claude/plugins/cache/*/claude-vault/*/skills/<skill>/scripts "
        "2>/dev/null | sort -V | tail -1)` + `python3 \"$SS/X.py\"`。\n命中：\n"
        + "\n".join(violations)
    )


def test_scan_actually_covers_known_files():
    """守护：确保扫描真的覆盖到 SKILL.md 与 references（防 glob 写错导致空扫假绿）。"""
    scanned = {p.relative_to(ROOT).as_posix() for p in _scan_md_files()}
    must_cover = {
        "skills/summarize-session/SKILL.md",
        "skills/vault-loader/SKILL.md",
        "skills/vault/SKILL.md",
        "skills/summarize-session/references/catch-up.md",
    }
    missing = must_cover - scanned
    assert not missing, f"守卫扫描范围漏掉预期文件：{sorted(missing)}"
