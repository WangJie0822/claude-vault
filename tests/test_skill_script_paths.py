# tests/test_skill_script_paths.py
"""防复发守卫：分发的 skill 文档不得引用退役源脚本目录。

背景：插件化后，源 skill（~/.claude/skills/<name>/）退役、只剩 runtime 态，
真脚本只在版本钉死的插件 cache 里。若 SKILL.md / references 仍写
`~/.claude/skills/<name>/scripts/X.py`，LLM 执行时必 No such file（那里只剩
__pycache__）。本守卫扫描所有分发的 skill markdown，命中即 fail，防止此类
死路径回归。

捕获三类死路径形式：
1. **退役源绝对路径** `~/.claude/skills/<skill>/scripts/`（含 `$HOME` 写法）——
   只锚定 `.../scripts/` 子路径，runtime 态引用（如 config.json，
   不含 `/scripts/`）天然不触发；cache-glob 定位器
   （`~/.claude/plugins/cache/.../scripts`，前缀是 `.claude/plugins/` 非
   `.claude/skills/`）也不触发。
2. **相对脚本调用** `python3 scripts/X.py`——假设 cwd 是脚本目录（cwd 不保证，
   在错误 cwd 跑必失败）。分发 skill 文档应统一用 cache-glob 定位器 `$SS`。
3. **`cd` 进退役源 skill 目录** `cd ~/.claude/skills/<skill>`——不含 `/scripts/`，
   形式 1 结构上不可能命中，需独立一条。这是实际发生过的回归：SKILL.md 的验收命令
   曾写 `cd ~/.claude/skills/vault-loader && python3 -m pytest`，而该目录插件化后
   只剩 runtime 态 `config.json`（31 字节），逐字执行收集不到任何用例、静默出 0 个测试。
   `cd` 之后接什么命令都建立在错误前提上，故直接钉 `cd` 本身。
   仅 `ls`/`cat` 该目录不触发——文档说明「那里只剩 config.json」是合法用途。

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
# 形式 3——`cd` 进退役源 skill 目录：`cd ~/.claude/skills/<skill>`。末尾不锚定 `/scripts/`，
# 所以形式 1 覆盖不到；只钉 `cd`，`ls`/`cat` 该目录（说明其已退役）仍合法。
_DEAD_SKILL_CD = re.compile(r"\bcd\s+(?:~|\$HOME)/\.claude/skills/[^/\s]+")


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
            if (_DEAD_SCRIPT_PATH.search(line)
                    or _REL_SCRIPT_CALL.search(line)
                    or _DEAD_SKILL_CD.search(line)):
                violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    assert not violations, (
        "分发 skill 文档引用了退役源 skill 目录 `~/.claude/skills/<x>/`"
        "（插件化后那里只剩 runtime 态 config.json，脚本执行必 No such file、"
        "`cd` 进去跑 pytest 收集不到用例）。"
        "请改用 cache-glob 定位器 `SS=$(ls -d "
        "~/.claude/plugins/cache/*/claude-vault/*/skills/<skill>/scripts "
        "2>/dev/null | sort -V | tail -1)` + `python3 \"$SS/X.py\"`。\n命中：\n"
        + "\n".join(violations)
    )


def test_patterns_actually_match_known_regressions():
    """自证：三条正则对**真实发生过**的回归形式都命中，且不误伤正确写法。

    没有这一条，正则写错（例如改坏成永不匹配）会让上面的扫描永远绿——
    「测试存在」不等于「守卫有效」。
    """
    must_hit = [
        ("python3 ~/.claude/skills/vault-loader/scripts/rebuild_index.py", _DEAD_SCRIPT_PATH),
        ("$HOME/.claude/skills/summarize-session/scripts/x.py", _DEAD_SCRIPT_PATH),
        ("python3 scripts/rebuild_index.py", _REL_SCRIPT_CALL),
        ("cd ~/.claude/skills/vault-loader && python3 -m pytest", _DEAD_SKILL_CD),
    ]
    for line, pat in must_hit:
        assert pat.search(line), f"正则失效，未命中已知回归形式：{line}"

    # 正确写法与合法用途不得误报
    must_not_hit = [
        'SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/vault-loader/scripts '
        '2>/dev/null | sort -V | tail -1)',
        'python3 "$SS/rebuild_index.py"',
        "ls -la ~/.claude/skills/vault-loader/   # 那里只剩 runtime 态 config.json",
    ]
    for line in must_not_hit:
        for pat in (_DEAD_SCRIPT_PATH, _REL_SCRIPT_CALL, _DEAD_SKILL_CD):
            assert not pat.search(line), f"误报：/{pat.pattern}/ 命中了正确写法 {line}"


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
