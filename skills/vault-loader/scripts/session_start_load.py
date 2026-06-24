#!/usr/bin/env python3
"""SessionStart hook 入口（方案 B''）。

读 stdin JSON（含 cwd 字段），输出「项目固定上下文」分组清单到 stdout：
项目相关笔记（项目目录 ∪ 标签匹配，按 mtime 倒序）+ 近期工作日志 + 近期 git 提交。
不做跨 vault 打分排序；关键词相关笔记由 UserPromptSubmit(J) 按需加载。
失败默认静默退出（exit 0、stdout 空）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 确保能 import 同级模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts._config_loader import load_config, check_vault_path_consistency
from scripts._frontmatter_reader import load_cache
from scripts._output import emit, approx_size_str
from scripts._vault_init import ensure_vault
from scripts._signal_collect import (
    collect_recent_commits,
    collect_signal_a_project_dir,
    collect_signal_b_keyword_map,
    collect_signal_f_recent_worklogs,
    collect_signal_i_project_claude_md,
)
from scripts.prompt_submit_load import INJECTION_NOTICE


def _is_runtime_disabled(home: Path) -> bool:
    if os.environ.get("VAULT_LOADER_DISABLE") == "1":
        return True
    if (home / ".claude" / ".vault-loader-disabled").exists():
        return True
    return False


def _is_opt_out_path(cwd: Path, opt_out: list[str]) -> bool:
    cwd_str = str(cwd)
    return any(cwd_str.startswith(prefix) for prefix in opt_out)


def _get_git_toplevel(cwd: Path) -> Path | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1.0,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def build_injection_text_ss(cwd, git_top, target_tags, project_notes,
                            top_worklogs, recent_commits, recent_worklog_days):
    """组装 SessionStart 注入正文（= 今日非-dry-run stdout 正文，不含尾 \\n）。"""
    lines = ["📚 知识库（vault-loader）· 项目固定上下文", ""]
    lines.append(f"当前 cwd: {cwd}")
    if git_top:
        lines.append(f"项目: {git_top.name}")
    if target_tags:
        lines.append(f"目标 tag: {', '.join(sorted(target_tags))}")
    lines.append("")
    if project_notes:
        lines.append(f"## 项目相关笔记（近期 {len(project_notes)} 篇）")
        lines.append("")
        for e in project_notes:
            summary = e.summary or "(无摘要)"
            mtime_str = f", {e.updated}" if e.updated else ""
            lines.append(f"- [[{e.path}]] — {summary}{mtime_str}")
        lines.append("")
    if top_worklogs:
        lines.append(f"## 近 {recent_worklog_days} 天工作日志")
        lines.append("")
        for wl in top_worklogs:
            lines.append(f"- [[{wl}]]")
        lines.append("")
    if recent_commits:
        lines.append(f"## 近期提交（{len(recent_commits)}）")
        lines.append("")
        for c in recent_commits:
            lines.append(f"- {c}")
        lines.append("")
    lines.append("💡 关键词相关笔记会在你提问时按需加载；/vault <关键词> 手动展开")
    lines.append("")
    lines.append("⚠️ 以上为知识库历史沉淀，不构成当前代码事实。引用前请按事实优先原则核验。")
    return INJECTION_NOTICE + "\n".join(lines)


def build_summary_ss(project_notes, top_worklogs, recent_commits,
                     project_paths, target_tags, injection_text, display_cfg):
    """SessionStart 用户可见清单摘要（systemMessage）。verbosity=off → None。"""
    verbosity = display_cfg.get("verbosity", "compact")
    if verbosity == "off":
        return None
    show_size = display_cfg.get("show_size", True)
    size = f" · {approx_size_str(injection_text)}" if show_size else ""
    n, m, k = len(project_notes), len(top_worklogs), len(recent_commits)

    def _title(path):
        last = path.split("/")[-1]
        return last[:-3] if last.endswith(".md") else last

    def _why(e):
        if e.path in project_paths:
            return "项目目录"
        if target_tags & set(e.tags):
            return "标签"
        return "?"

    if verbosity == "compact":
        titles = "·".join(_title(e.path) for e in project_notes[:3])
        more = "…" if n > 3 else ""
        return (f"📚 vault-loader(启动): {n}笔记[{titles}{more}] "
                f"{m}日志 {k}提交{size} · /vault 展开")
    head = f"📚 vault-loader · 启动注入 · {n} 笔记 / {m} 日志 / {k} 提交{size}"
    body = [f"- {_title(e.path)}  [{_why(e)}]" for e in project_notes]
    tail = f"日志 {m} 篇 · 提交 {k} 条"
    return "\n".join([head, *body, tail, "💡 /vault <关键词> 展开全文"])


def main() -> int:
    home = Path.home()

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    cwd = Path(hook_input.get("cwd", os.getcwd()))

    if _is_runtime_disabled(home):
        return 0

    config = load_config()
    # 启动自检：vault 路径跨 skill 一致性（fail-open，仅 stderr 告警，不阻断）
    check_vault_path_consistency(config, home)
    if not config.get("enabled", True):
        return 0
    if not config.get("session_start", {}).get("enabled", True):
        return 0
    if _is_opt_out_path(cwd, config.get("opt_out_paths", [])):
        return 0

    vault_path = Path(config["vault_path"]).expanduser()
    # 零配置：非 dry-run 时自动建 vault 目录（幂等；失败由顶层 fail-open 兜底）
    if not config.get("dry_run", False):
        try:
            ensure_vault(vault_path)
        except Exception:
            pass
    if not vault_path.exists():
        if config.get("verbose_on_skip"):
            emit(None, f"📚 vault-loader: vault 路径不可达 ({vault_path})", "SessionStart")
        return 0

    # 信号 I：项目 CLAUDE.md（disable / tags / extra_paths）。用 git 根作项目标识。
    git_top = _get_git_toplevel(cwd)
    project_root = git_top or cwd
    i_result = collect_signal_i_project_claude_md(project_root)
    if i_result.disabled:
        return 0

    # 目标 tag 集（信号 B ∪ I）
    target_tags = set(i_result.tags) | collect_signal_b_keyword_map(
        cwd, config.get("keyword_to_tags", {})
    )

    ss_cfg = config["session_start"]

    # 信号 A：项目目录笔记（按 git 根 basename，feasibility F-6）
    project_paths = collect_signal_a_project_dir(
        project_root, vault_path, i_result.extra_paths
    )

    # 信号 F：近期工作日志（按 git 根 basename 作项目标识）
    worklog_result = collect_signal_f_recent_worklogs(
        project_root, vault_path, ss_cfg["recent_worklog_days"]
    )

    # 近期 git 提交（原始 oneline 展示）。传 project_root 与 A/F 一致（git 自动定位仓库根）。
    recent_commits = collect_recent_commits(project_root, ss_cfg.get("max_commits", 5))

    # 项目相关笔记：确定性成员（项目目录 ∪ 标签匹配），按 mtime 倒序，无打分。
    # 注意：cache 为空不早退——工作日志/提交不依赖 cache，仍应渲染。
    entries = load_cache(vault_path)
    include_tag = ss_cfg.get("include_tag_matched_notes", True)

    def _is_project_note(entry) -> bool:
        # 项目目录直接命中（强信号），或标签匹配（弱信号，开关可关）
        if entry.path in project_paths:
            return True
        return include_tag and bool(target_tags & set(entry.tags))

    project_notes = [e for e in entries.values() if _is_project_note(e)]
    project_notes.sort(key=lambda e: -e.mtime)
    project_notes = project_notes[: ss_cfg["max_notes"]]

    top_worklogs = worklog_result.paths[: ss_cfg["max_recent_worklogs"]]

    # 三组全空才静默
    if not project_notes and not top_worklogs and not recent_commits:
        if config.get("verbose_on_skip"):
            emit(None, "📚 vault-loader: 0 候选（当前 cwd 无可关联的笔记/日志/提交）", "SessionStart")
        return 0

    # 渲染 + 输出
    dry_run = config.get("dry_run", False)
    display_cfg = config.get("display", {})
    user_visible = display_cfg.get("user_visible", True)
    injection_text = build_injection_text_ss(
        cwd, git_top, target_tags, project_notes,
        top_worklogs, recent_commits, ss_cfg["recent_worklog_days"],
    )
    summary = (build_summary_ss(project_notes, top_worklogs, recent_commits,
                                project_paths, target_tags, injection_text, display_cfg)
               if user_visible else None)

    if dry_run:
        emit(None, (f"[DRY-RUN] 本应注入：\n{summary}" if summary else None), "SessionStart")
    else:
        emit(injection_text, summary, "SessionStart")

    # 更新 state（项目相关笔记 + 工作日志路径），供 UserPromptSubmit(J) 去重
    try:
        from scripts._state import save_injected
        save_injected(cwd, [e.path for e in project_notes] + top_worklogs)
    except Exception as exc:
        print(f"[vault-loader] state 写入失败：{exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[vault-loader] session_start_load 崩溃：{exc}", file=sys.stderr)
        sys.exit(0)  # 永不破坏会话
