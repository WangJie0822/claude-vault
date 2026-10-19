"""SessionStart 写入注入去重集的只能是**真正注入过内容**的条目。

工作日志在 SessionStart 正文里只渲染裸链接 `- [[路径]]`、没有摘要；把它们写进注入集，
UPS 会把它们当 `candidate_injected` 排除，相关工作日志此后在提问时拿不到摘要。
1.x 里这个问题潜伏：SessionStart 被错判到 codex 命名空间，UPS 读不到它写的集合；
2.0.0 修好落点后它就会生效，所以与落点修复一起改。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SS_SCRIPT = SCRIPTS / "session_start_load.py"
UPS_SCRIPT = SCRIPTS / "prompt_submit_load.py"


def _run(payload: dict, script: Path = SS_SCRIPT) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    env.pop("PLUGIN_ROOT", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)


def test_worklogs_are_not_recorded_as_injected(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    cfg_dir = tmp_home / ".context-vault"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({"vault_path": str(tmp_vault)}),
                                         encoding="utf-8")
    project = tmp_git_repo.name
    note_rel = f"项目笔记/{project}/design.md"
    (tmp_vault / "项目笔记" / project).mkdir(parents=True, exist_ok=True)
    (tmp_vault / note_rel).write_text("# design", encoding="utf-8")
    write_frontmatter_cache({note_rel: {"tags": ["设计"], "category": "项目笔记",
                                        "summary": "项目设计", "mtime": 1900000000}})
    wl_dir = tmp_vault / "工作日志"
    wl_dir.mkdir(parents=True, exist_ok=True)
    (wl_dir / "2026-10-08.md").write_text(
        f"---\n项目: {project}\n---\n\n## 10:00 ~ 11:00 | 开发 | 某条目\n", encoding="utf-8")

    r = _run({"cwd": str(tmp_git_repo), "hook_event_name": "SessionStart",
              "session_id": "sess-injected-set", "source": "startup"})
    assert r.returncode == 0, r.stderr
    context = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "工作日志/2026-10-08.md" in context, "前提：工作日志应作为链接出现在 SessionStart 正文"

    states = []
    for f in (tmp_home / ".context-vault" / "state" / "claude").glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "paths" in data:
            states.append(data)
    assert len(states) == 1, f"应恰有一份注入去重 state：{states}"
    paths = states[0]["paths"]
    assert note_rel in paths, "项目笔记注入过摘要，必须记入"
    assert not [p for p in paths if p.startswith("工作日志/")], \
        f"工作日志只给过链接，不得记为已注入：{paths}"


# ---------------------------------------------------------------------------
# SessionStart 与 UPS 共享同一份注入去重集（2.0.0 的核心修复，full-review P3A-H1）
#
# 1.x 把部分 SessionStart 判成 Codex，它写的注入集落在另一个命名空间，UPS 读不到。
# 同进程用例两次 configure_context 必然同为 "claude"，对入口接线零判别力；这里必须
# 真起两个子进程、按生产顺序跑。观测量取 UPS 的 metrics 记录：同一篇笔记若已被
# SessionStart 注入过，UPS 把它判为 `dedup == "candidate_injected"`。
# ---------------------------------------------------------------------------

_UPS_PROMPT = "项目设计 相关的历史决策 怎么做的"


def _setup_shared(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache) -> str:
    cfg_dir = tmp_home / ".context-vault"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # session_topic 关掉：否则 UPS 会拉起真实的 `claude -p` 提炼子进程。
    # auto_fulltext 打开：观测量是 admitted 条目上的 dedup，曾注入过的篇靠 upgrade_candidate 重新进
    # admitted；2.4.0 起这条通路随自动全文默认关，不开它这篇只落 excluded、admitted 里看不到它
    (cfg_dir / "config.json").write_text(json.dumps({
        "vault_path": str(tmp_vault),
        "metrics": {"enabled": True},
        "relevance": {"session_topic": False, "auto_fulltext": True},
    }), encoding="utf-8")
    project = tmp_git_repo.name
    note_rel = f"项目笔记/{project}/design.md"
    (tmp_vault / "项目笔记" / project).mkdir(parents=True, exist_ok=True)
    (tmp_vault / note_rel).write_text("# 项目设计\n\n历史决策记录。\n", encoding="utf-8")
    write_frontmatter_cache({note_rel: {"tags": ["设计"], "category": "项目笔记",
                                        "summary": "项目设计与历史决策",
                                        "mtime": 1900000000}})
    return note_rel


def _ups_dedup(home: Path, note_rel: str) -> list[str]:
    """UPS 记录里该笔记 admitted 条目的 dedup 值（按出现顺序）。"""
    out: list[str] = []
    for f in (home / ".context-vault" / "metrics" / "claude").rglob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("channel") == "session_start":
                continue
            out += [e.get("dedup", "") for e in rec.get("admitted", [])
                    if e.get("path") == note_rel]
    return out


def _ups(tmp_git_repo) -> subprocess.CompletedProcess:
    return _run({"cwd": str(tmp_git_repo), "hook_event_name": "UserPromptSubmit",
                 "session_id": "S-shared", "prompt_id": "P-1", "prompt": _UPS_PROMPT},
                UPS_SCRIPT)


def test_ups_sees_session_start_injected_set(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    note_rel = _setup_shared(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache)
    ss = _run({"cwd": str(tmp_git_repo), "hook_event_name": "SessionStart",
               "session_id": "S-shared", "source": "startup"})
    assert ss.returncode == 0, ss.stderr
    # 统一行不再渲染相对路径 `[[rel]]`：认文件名标题（绝对路径里的分隔符随平台变，不能拿 rel 去子串匹配）
    assert "「design」" in json.loads(ss.stdout)["hookSpecificOutput"]["additionalContext"], \
        "前提：SessionStart 注入了这篇项目笔记"

    ups = _ups(tmp_git_repo)
    assert ups.returncode == 0, ups.stderr
    assert _ups_dedup(tmp_home, note_rel) == ["candidate_injected"], \
        "UPS 没读到 SessionStart 写的注入集：两个入口的 state 命名空间分裂了"


def test_ups_alone_treats_note_as_new(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """阴性对照：同一夹具不跑 SessionStart，UPS 不应把这篇判为已注入。"""
    note_rel = _setup_shared(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache)
    ups = _ups(tmp_git_repo)
    assert ups.returncode == 0, ups.stderr
    assert _ups_dedup(tmp_home, note_rel) == [""]
