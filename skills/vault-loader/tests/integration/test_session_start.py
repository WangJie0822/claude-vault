"""session_start_load 集成测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.session_start_load import build_injection_text_ss, build_summary_ss
from scripts._frontmatter_reader import Entry

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "session_start_load.py"


def _run_hook(cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """模拟 hook 调用：hook 输入 JSON 通过 stdin。"""
    env = os.environ.copy()
    # 子进程强制 UTF-8（镜像生产；Windows 默认 GBK 会令 hook 输出 emoji/中文失败）
    env.setdefault("PYTHONUTF8", "1")
    if env_extra:
        env.update(env_extra)
    hook_input = json.dumps({"cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=hook_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=5,
    )


def test_typical_injection(tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path,
                            write_frontmatter_cache, monkeypatch) -> None:
    """项目命中 + 工作日志 + commits → 注入有内容。"""
    # 构造 vault：项目笔记/<basename>/note.md
    basename = tmp_git_repo.name
    proj_dir = tmp_vault / "项目笔记" / basename
    proj_dir.mkdir(parents=True)
    (proj_dir / "design.md").write_text("# design")

    # 写 cache
    write_frontmatter_cache({
        f"项目笔记/{basename}/design.md": {
            "tags": ["设计"],
            "category": "项目笔记",
            "summary": "项目设计",
            "mtime": 1900000000,  # 近期
        }
    })

    # 写工作日志（嵌套结构 YYYY年/MM月/）
    import time
    today = time.strftime("%Y-%m-%d")
    year, month = today[:4], today[5:7]
    worklog_dir = tmp_vault / "工作日志" / f"{year}年" / f"{month}月"
    worklog_dir.mkdir(parents=True)
    (worklog_dir / f"{today}.md").write_text(
        f"---\ntags: [工作日志]\n---\n\n## 10:00 ~ 11:00 | 1h | hook 触发顺序\n"
        f"**项目**: {basename}\n"
    )

    # config：dry_run 关闭走正式注入路径
    cfg_path = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
    }))

    r = _run_hook(tmp_git_repo)
    assert r.returncode == 0
    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "📚" in d["systemMessage"]
    assert "## 项目相关笔记" in ac and "design.md" in ac and "## 近期提交" in ac


def test_empty_signals_silent(tmp_home: Path, tmp_vault: Path) -> None:
    """无 cache、无 git、无项目目录 → 静默退出。"""
    cfg_path = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
    }))

    r = _run_hook(Path("/nonexistent/path"))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_opt_out_disable_via_env(tmp_home: Path) -> None:
    r = _run_hook(Path("/tmp"), env_extra={"VAULT_LOADER_DISABLE": "1"})
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_opt_out_disable_via_flag_file(tmp_home: Path) -> None:
    (tmp_home / ".claude" / ".vault-loader-disabled").write_text("")
    r = _run_hook(Path("/tmp"))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_opt_out_via_project_claude_md_disable(
    tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path
) -> None:
    (tmp_git_repo / "CLAUDE.md").write_text("<!-- vault-loader: disable -->")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add claude md"], cwd=tmp_git_repo, check=True)

    cfg_path = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
    }))

    r = _run_hook(tmp_git_repo)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_opt_out_path_prefix(tmp_home: Path, tmp_vault: Path) -> None:
    cfg_path = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "opt_out_paths": ["/private/tmp"],
    }))

    r = _run_hook(Path("/private/tmp/sandbox"))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_dry_run_no_injection(tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path,
                               write_frontmatter_cache) -> None:
    basename = tmp_git_repo.name
    proj_dir = tmp_vault / "项目笔记" / basename
    proj_dir.mkdir(parents=True)
    (proj_dir / "design.md").write_text("# design")

    write_frontmatter_cache({
        f"项目笔记/{basename}/design.md": {
            "tags": ["设计"],
            "summary": "项目设计",
            "mtime": 1900000000,
        }
    })

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": True, "vault_path": str(tmp_vault)}))

    r = _run_hook(tmp_git_repo)
    d = _parse(r); assert d is not None
    assert d["systemMessage"].startswith("[DRY-RUN]")
    assert "hookSpecificOutput" not in d, "dry_run 不得真注入"


def test_verbose_on_skip_emits_message(tmp_home: Path, tmp_vault: Path) -> None:
    """verbose_on_skip=true 时，无候选也应输出短提示。"""
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "verbose_on_skip": True,
    }))

    r = _run_hook(Path("/some/no-signals"))
    d = _parse(r); assert d is not None
    assert "vault-loader" in d["systemMessage"]


def test_tag_matched_notes_without_project_dir(
    tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, tmp_path: Path
) -> None:
    """无 项目笔记/<name>/，但有标签匹配笔记 → 仍出现在「项目相关笔记」组（.claude 式场景）。"""
    write_frontmatter_cache({
        "Claude Code/some-note.md": {
            "tags": ["claude-code", "skill"],
            "summary": "claude code 笔记",
            "mtime": 1900000000,
        }
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # keyword_to_tags 明确提供（DEFAULT_CONFIG 中性化后默认为空）
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "keyword_to_tags": {"claude": ["claude-code", "skill"]},
    }))

    # cwd 路径含 "claude" → keyword_to_tags["claude"]=[claude-code,skill]；非 git
    proj = tmp_path / "claude_demo"
    proj.mkdir()
    r = _run_hook(proj)
    d = _parse(r); assert d is not None
    assert "📚" in d["systemMessage"]
    assert "some-note.md" in d["hookSpecificOutput"]["additionalContext"]


def test_include_tag_matched_false_dir_only(
    tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, tmp_path: Path
) -> None:
    """include_tag_matched_notes=false → 标签匹配笔记不出现（严格 dir-only）。"""
    write_frontmatter_cache({
        "Claude Code/some-note.md": {
            "tags": ["claude-code"],
            "summary": "x",
            "mtime": 1900000000,
        }
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "session_start": {"include_tag_matched_notes": False},
    }))
    proj = tmp_path / "claude_demo2"
    proj.mkdir()
    r = _run_hook(proj)
    d = _parse(r)
    assert d is None or "some-note.md" not in d["hookSpecificOutput"]["additionalContext"]


def test_recent_commits_rendered_cache_empty(
    tmp_home: Path, tmp_vault: Path, tmp_git_repo: Path
) -> None:
    """git 提交渲染到「## 近期提交」；且 cache 为空也不早退（仍渲染提交组）。"""
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))
    # 不写 frontmatter-cache → cache 空
    r = _run_hook(tmp_git_repo)
    d = _parse(r); assert d is not None
    ac = d["hookSpecificOutput"]["additionalContext"]
    assert "## 近期提交" in ac and "init" in ac


def test_uses_git_root_basename_from_subdir(
    tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, tmp_git_repo: Path
) -> None:
    """从子目录启动时，项目相关笔记按 git 根 basename 匹配（feasibility F-6）。"""
    reponame = tmp_git_repo.name  # conftest 里为 "repo"
    proj_dir = tmp_vault / "项目笔记" / reponame
    proj_dir.mkdir(parents=True)
    (proj_dir / "rootnote.md").write_text("# n", encoding="utf-8")
    write_frontmatter_cache({
        f"项目笔记/{reponame}/rootnote.md": {
            "tags": [], "summary": "根笔记", "mtime": 1900000000,
        }
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}))

    subdir = tmp_git_repo / "src"
    subdir.mkdir()
    r = _run_hook(subdir)
    d = _parse(r); assert d is not None
    assert "rootnote.md" in d["hookSpecificOutput"]["additionalContext"], "应按 git 根 basename 找到项目笔记，而非子目录名"


def test_special_chars_valid_json_and_verbatim(
    tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, tmp_path: Path
) -> None:
    """笔记含特殊字符/终端转义 → stdout 合法 JSON、additionalContext 逐字含原文、systemMessage 无裸 ESC。"""
    nasty = '含 ``` 代码 "引号" \\反斜杠 emoji😀 \x1b]0;X\x07 零宽​'
    write_frontmatter_cache({
        "Claude Code/nasty.md": {
            "tags": ["claude-code", "skill"],
            "summary": nasty,
            "mtime": 1900000000,
        }
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # keyword_to_tags 明确提供（DEFAULT_CONFIG 中性化后默认为空）
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "keyword_to_tags": {"claude": ["claude-code", "skill"]},
    }))
    proj = tmp_path / "claude_nasty"
    proj.mkdir()

    r = _run_hook(proj)
    assert r.returncode == 0
    d = _parse(r)
    assert d is not None
    assert nasty in d["hookSpecificOutput"]["additionalContext"]  # 喂模型逐字
    assert "\x1b" not in d["systemMessage"]                       # 用户侧已清洗
    assert "\x07" not in d["systemMessage"]


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _parse(r):
    out = r.stdout.strip()
    return json.loads(out) if out else None


# ---------------------------------------------------------------------------
# build_summary_ss 格式守护
# ---------------------------------------------------------------------------

def test_build_summary_ss_list_format():
    notes = [Entry(path="项目笔记/repo/n.md", summary="s", mtime=1, tags=("设计",))]
    injection_text = "x" * 1500
    out = build_summary_ss(
        notes, ["wl.md"], ["c1"],
        {"项目笔记/repo/n.md"}, {"设计"},
        injection_text,
        {"verbosity": "list", "show_size": True},
    )
    assert out.startswith("📚 vault-loader · 启动注入 · 1 笔记 / 1 日志 / 1 提交 · ~1.5k 字")
    assert "- n  [项目目录]" in out


def test_build_summary_ss_compact_format():
    notes = [Entry(path="a/b.md", summary="s", mtime=1)]
    injection_text = "y" * 300
    out = build_summary_ss(
        notes, [], [],
        set(), set(),
        injection_text,
        {"verbosity": "compact", "show_size": True},
    )
    assert out == "📚 vault-loader(启动): 1笔记[b] 0日志 0提交 · ~300 字 · /vault 展开"


# ---------------------------------------------------------------------------
# golden 等价守护
# ---------------------------------------------------------------------------

def test_build_injection_text_ss_golden() -> None:
    """注入正文逐字等价旧格式（模型侧零回归守护）。
    Task 5.1：头部含 INJECTION_NOTICE 隔离声明（intentional update）。"""
    from scripts.prompt_submit_load import INJECTION_NOTICE

    notes = [Entry(path="项目笔记/repo/design.md", summary="项目设计",
                   mtime=1900000000, updated="2026-06-20")]
    text = build_injection_text_ss(
        cwd=Path("/work/repo"),
        git_top=Path("/work/repo"),
        target_tags={"设计"},
        project_notes=notes,
        top_worklogs=["工作日志/2026年/06月/2026-06-20.md"],
        recent_commits=["abc123 修复 X"],
        recent_worklog_days=7,
    )
    expected = INJECTION_NOTICE + "\n".join([
        "📚 知识库（vault-loader）· 项目固定上下文",
        "",
        f"当前 cwd: {Path('/work/repo')}",
        "项目: repo",
        "目标 tag: 设计",
        "",
        "## 项目相关笔记（近期 1 篇）",
        "",
        "- [[项目笔记/repo/design.md]] — 项目设计, 2026-06-20",
        "",
        "## 近 7 天工作日志",
        "",
        "- [[工作日志/2026年/06月/2026-06-20.md]]",
        "",
        "## 近期提交（1）",
        "",
        "- abc123 修复 X",
        "",
        "💡 关键词相关笔记会在你提问时按需加载；/vault <关键词> 手动展开",
        "",
        "⚠️ 以上为知识库历史沉淀，不构成当前代码事实。引用前请按事实优先原则核验。",
    ])
    assert text == expected
