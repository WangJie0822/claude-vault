#!/usr/bin/env python3
"""扫描 Claude Code 会话文件，找出未总结的会话并提取对话摘要。

用法:
  python3 scan_sessions.py --days 7                    # 列出最近 7 天未总结的会话
  python3 scan_sessions.py --days 3 --parse             # 列出并解析对话内容
  python3 scan_sessions.py --parse --session <uuid>      # 解析指定会话
  python3 scan_sessions.py --mark <uuid1> <uuid2> ...   # 标记为已总结
  python3 scan_sessions.py --mark-current <CWD>         # 标记当前项目最新会话
  python3 scan_sessions.py --timerange <CWD>            # 提取当前会话时段
  python3 scan_sessions.py --touched-repos <CWD>        # 扫描本次会话实际修改的 git 仓库
"""

import json
import os
import platform
import re
import subprocess
import sys
import argparse
import time
from datetime import datetime
from pathlib import Path


def _cwd_to_project_name(cwd: str) -> str:
    """把 cwd 标准化为 Claude Code 的 projects/ 子目录命名格式。

    Claude Code 命名规则（实证）：所有非字母数字字符替换为 `-`。
    - POSIX: /Users/test/Work/x.0 → -Users-test-Work-x-0
    - Windows: C:\\Users\\foo\\.claude → C--Users-foo--claude

    Windows Git Bash 下 cwd 可能是 POSIX 风格 /c/Users/foo/.claude，
    需先归一化为 Windows 原生 C:\\Users\\foo\\.claude 再编码，
    否则编码结果首字符是 `-`、盘符是小写，无法匹配 Claude Code 写入 jsonl 的目录名。
    """
    p = cwd
    if (
        platform.system() == "Windows"
        and len(p) >= 3
        and p[0] == "/"
        and p[1].isalpha()
        and p[2] == "/"
    ):
        drive = p[1].upper()
        rest = p[3:].replace("/", "\\")
        p = f"{drive}:\\{rest}"
    return re.sub(r"[^A-Za-z0-9]", "-", p)

DEFAULT_CLAUDE_DIR = os.path.expanduser("~/.claude")
DEFAULT_MANIFEST = os.path.expanduser(
    "~/.claude/skills/summarize-session/summarized-sessions.json"
)

# 文件锁超时时间（秒）
LOCK_TIMEOUT = 30


def _acquire_lock(lock_path: str, timeout: int = LOCK_TIMEOUT):
    """获取文件锁（跨平台），返回是否成功。"""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            # 检查锁文件是否过期
            try:
                lock_mtime = os.path.getmtime(lock_path)
                if time.time() - lock_mtime > timeout:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.2)


def _release_lock(lock_path: str):
    """释放文件锁。"""
    try:
        os.remove(lock_path)
    except OSError:
        pass


def load_manifest(manifest_path: str) -> set:
    """加载已总结的会话 ID 集合。"""
    if not os.path.exists(manifest_path):
        return set()
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("sessions", []))
    except (json.JSONDecodeError, IOError):
        return set()


def save_manifest(manifest_path: str, session_ids: set) -> bool:
    """保存已总结的会话 ID 集合（并发安全）。

    使用文件锁 + 原子写入，防止多窗口同时执行时数据丢失。
    成功返回 True；获取锁失败返回 False，调用方应据此报错或重试，
    而不是无声降级写入（会覆盖其他窗口的并发写入）。
    """
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    lock_path = manifest_path + ".lock"
    if not _acquire_lock(lock_path):
        print(json.dumps(
            {
                "error": f"无法获取文件锁（超时 {LOCK_TIMEOUT}s），请稍后重试",
                "lock_path": lock_path,
            },
            ensure_ascii=False,
        ), file=sys.stderr)
        return False
    try:
        existing = load_manifest(manifest_path)
        merged = sorted(existing | session_ids)
        tmp_path = manifest_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"sessions": merged, "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, manifest_path)
        return True
    finally:
        _release_lock(lock_path)


def find_session_files(claude_dir: str, days: int, project_filter: str = None) -> list:
    """在 ~/.claude/projects/ 下查找 .jsonl 会话文件。"""
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return []

    cutoff = datetime.now().timestamp() - days * 86400
    sessions = []

    for project in os.listdir(projects_dir):
        project_path = os.path.join(projects_dir, project)
        if not os.path.isdir(project_path):
            continue
        if project_filter and project_filter not in project:
            continue

        for fname in os.listdir(project_path):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(project_path, fname)

            # 跳过子代理文件
            if "subagents" in fpath:
                continue

            mtime = os.path.getmtime(fpath)
            if mtime < cutoff:
                continue

            session_id = fname[:-6]  # 去掉 .jsonl
            sessions.append({
                "session_id": session_id,
                "project": project,
                "file_path": fpath,
                "mtime": mtime,
                "size": os.path.getsize(fpath),
            })

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def extract_text(content) -> str:
    """从消息 content 中提取纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                # 跳过系统提示标签内容
                if text.strip().startswith("<system-reminder>"):
                    continue
                # 跳过 skill 加载内容
                if text.strip().startswith("<command-message>"):
                    continue
                texts.append(text)
        return "\n".join(texts).strip()
    return ""


def extract_first_user_intent(messages: list) -> str:
    """从对话消息中提取用户的首个有意义请求，作为会话主题。"""
    for msg in messages:
        if msg["role"] == "user":
            text = msg["text"]
            # 跳过 init 命令等
            if text.startswith("<command-message>init") or len(text) < 5:
                continue
            # 截取前 150 字符作为主题
            return text[:150].replace("\n", " ")
    return "(无明确主题)"


def _extract_timerange(file_path: str) -> dict | None:
    """从 JSONL 会话文件提取首尾 timestamp，转换为本地时区的 HH:MM。

    返回:
      {"start": "14:30", "end": "16:00", "date": "2026-04-15", "duration_hours": 1.5}
      无法提取时返回 None。
    """
    first_ts_str = None
    last_ts_str = None
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = obj.get("timestamp")
                if ts:
                    if first_ts_str is None:
                        first_ts_str = ts
                    last_ts_str = ts
    except IOError:
        return None

    if not first_ts_str or not last_ts_str:
        return None

    try:
        # 兼容末尾带 Z 的 ISO 8601(Python 3.11+ 已原生支持,这里保险起见手动替换)
        start_dt = datetime.fromisoformat(first_ts_str.replace("Z", "+00:00")).astimezone()
        end_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None

    duration_hours = round((end_dt - start_dt).total_seconds() / 3600, 2)
    return {
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "date": start_dt.strftime("%Y-%m-%d"),
        "duration_hours": duration_hours,
    }


def _locate_current_session_file(claude_dir: str, cwd: str) -> tuple:
    """定位当前项目最新的会话 JSONL 文件路径。

    返回 (session_id, file_path);找不到时返回 (None, None)。
    """
    sid = get_current_session_id(claude_dir, cwd)
    if not sid:
        return (None, None)
    project_name = _cwd_to_project_name(cwd)
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return (None, None)
    # 先精确匹配,再前缀 fallback(与 get_current_session_id 保持一致)
    entries = os.listdir(projects_dir)
    for d in entries:
        if d == project_name:
            candidate = os.path.join(projects_dir, d, f"{sid}.jsonl")
            if os.path.exists(candidate):
                return (sid, candidate)
    for d in entries:
        if d.startswith(project_name):
            candidate = os.path.join(projects_dir, d, f"{sid}.jsonl")
            if os.path.exists(candidate):
                return (sid, candidate)
    return (sid, None)


def _extract_touched_files(jsonl_path: str) -> list:
    """从会话 JSONL 中提取所有 Edit/Write/NotebookEdit 工具调用的 file_path。

    按调用顺序去重返回 list,便于下游统计文件数和定位仓库。
    """
    seen = set()
    ordered = []
    edit_tools = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "tool_use":
                        continue
                    if item.get("name") not in edit_tools:
                        continue
                    input_data = item.get("input") or {}
                    fp = input_data.get("file_path")
                    if fp and fp not in seen:
                        seen.add(fp)
                        ordered.append(fp)
    except IOError:
        return []
    return ordered


def _locate_git_repo(path: str) -> dict:
    """定位 path 所属 git 仓库的 toplevel 和当前分支。

    path 可以是文件或目录;若文件不存在,取其 dirname。
    返回 {"toplevel": str, "branch": str} 或 None(不在 git 仓库内 / 命令失败)。
    """
    # 解析出一个有效的目录
    if os.path.isdir(path):
        dir_path = path
    else:
        dir_path = os.path.dirname(path)
        if not os.path.isdir(dir_path):
            # 路径整个不存在,尝试向上追溯
            while dir_path and not os.path.isdir(dir_path):
                parent = os.path.dirname(dir_path)
                if parent == dir_path:
                    return None
                dir_path = parent
            if not dir_path:
                return None

    try:
        tl = subprocess.run(
            ["git", "-C", dir_path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if tl.returncode != 0:
            return None
        toplevel = tl.stdout.strip()
        if not toplevel:
            return None
        br = subprocess.run(
            ["git", "-C", dir_path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        branch = br.stdout.strip() if br.returncode == 0 else ""
        return {"toplevel": toplevel, "branch": branch}
    except (subprocess.SubprocessError, OSError):
        return None


def _aggregate_touched_repos(touched_files: list) -> dict:
    """把 file_path 列表聚合为 {toplevel: {branch, file_count}}。

    使用 dirname 去重避免对同目录下多个文件重复跑 git。
    """
    dir_to_repo = {}  # dirname -> {toplevel, branch} (None 表示非 git)
    repo_map = {}  # toplevel -> {branch, file_count}
    for fp in touched_files:
        d = os.path.dirname(fp) or "."
        if d not in dir_to_repo:
            dir_to_repo[d] = _locate_git_repo(d)
        info = dir_to_repo[d]
        if not info:
            continue
        key = info["toplevel"]
        if key not in repo_map:
            repo_map[key] = {"branch": info["branch"], "file_count": 0}
        repo_map[key]["file_count"] += 1
    return repo_map


def parse_session(file_path: str, max_chars: int = 4000) -> dict:
    """解析会话 JSONL 文件，提取对话内容。

    返回:
      {
        "messages": [{"role": "user/assistant", "text": "..."}],
        "total_messages": int,
        "cwd": str,
        "first_intent": str,
        "date": str,
        "timerange": {...}  # 或 None
      }
    """
    messages = []
    cwd = ""
    session_date = ""

    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 提取会话元数据
                if not cwd and "cwd" in obj:
                    cwd = obj["cwd"]
                if not session_date and "timestamp" in obj:
                    try:
                        session_date = datetime.fromisoformat(obj["timestamp"]).strftime("%Y-%m-%d %H:%M")
                    except (ValueError, TypeError):
                        pass

                if "message" not in obj:
                    continue

                msg = obj["message"]
                role = msg.get("role")
                content = msg.get("content", "")

                if role == "user":
                    text = extract_text(content)
                    if text and len(text) > 3:
                        messages.append({"role": "user", "text": text[:800]})
                elif role == "assistant":
                    text = extract_text(content)
                    if text and len(text) > 3:
                        messages.append({"role": "assistant", "text": text[:800]})
    except IOError as e:
        return {"error": str(e), "messages": [], "total_messages": 0, "cwd": cwd}

    # 截断到 max_chars 以内
    total_chars = 0
    truncated = []
    for msg in messages:
        if total_chars + len(msg["text"]) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 50:
                truncated.append({"role": msg["role"], "text": msg["text"][:remaining] + "..."})
            break
        truncated.append(msg)
        total_chars += len(msg["text"])

    first_intent = extract_first_user_intent(messages)

    return {
        "messages": truncated,
        "total_messages": len(messages),
        "cwd": cwd,
        "first_intent": first_intent,
        "date": session_date,
        "timerange": _extract_timerange(file_path),
    }


def get_current_session_id(claude_dir: str, cwd: str) -> str | None:
    """获取当前项目目录下最新的会话 ID。"""
    # 跨平台 cwd 编码（POSIX `/Users/...`、Windows `C:\\...`、Git Bash `/c/...` 全兼容）
    project_name = _cwd_to_project_name(cwd)
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return None

    # 优先精确匹配,避免 worktree 等派生目录干扰
    entries = os.listdir(projects_dir)
    best_match = None
    for d in entries:
        if d == project_name:
            best_match = d
            break
    # 回退到前缀匹配(兼容历史目录命名差异)
    if not best_match:
        for d in entries:
            if d.startswith(project_name):
                best_match = d
                break

    if not best_match:
        return None

    project_path = os.path.join(projects_dir, best_match)
    # 找最新的 .jsonl 文件
    latest = None
    latest_mtime = 0
    for fname in os.listdir(project_path):
        if fname.endswith(".jsonl") and "subagents" not in fname:
            fpath = os.path.join(project_path, fname)
            mtime = os.path.getmtime(fpath)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = fname[:-6]
    return latest


def format_project_name(raw: str) -> str:
    """将项目目录编码名转换为可读路径。
    例: -Users-test-Work-WorkSpace-assistantskills -> /Users/test/Work/.../assistantskills
    """
    if raw.startswith("-"):
        parts = raw.split("-")
        # 重建路径
        path = "/".join(parts)
        if len(path) > 50:
            # 缩短中间部分
            segments = path.split("/")
            if len(segments) > 4:
                return "/".join(segments[:2]) + "/.../" + segments[-1]
        return path
    return raw


def main():
    parser = argparse.ArgumentParser(description="扫描未总结的 Claude Code 会话")
    parser.add_argument("--days", type=int, default=7, help="回溯天数（默认 7）")
    parser.add_argument("--project", type=str, help="按项目目录名过滤")
    parser.add_argument("--manifest", type=str, default=DEFAULT_MANIFEST,
                        help="已总结会话清单路径")
    parser.add_argument("--claude-dir", type=str, default=DEFAULT_CLAUDE_DIR,
                        help="Claude 配置目录")
    parser.add_argument("--parse", action="store_true",
                        help="解析对话内容（默认只列出）")
    parser.add_argument("--session", type=str, nargs="+",
                        help="指定要解析的会话 ID")
    parser.add_argument("--max-chars", type=int, default=4000,
                        help="每个会话提取的最大字符数")
    parser.add_argument("--min-size", type=int, default=5,
                        help="最小文件大小（KB），过滤掉过短的会话")
    parser.add_argument("--mark", type=str, nargs="+",
                        help="标记指定会话为已总结")
    parser.add_argument("--mark-current", type=str, metavar="CWD",
                        help="标记当前工作目录下最新会话为已总结")
    parser.add_argument("--timerange", type=str, metavar="CWD",
                        help="提取当前项目最新会话的时段信息（start/end/date/duration_hours）")
    parser.add_argument("--touched-repos", type=str, metavar="CWD",
                        help="扫描本次会话实际修改的 git 仓库集合（用于工作日志项目字段）")
    parser.add_argument("--json", action="store_true", default=True,
                        help="JSON 输出（默认）")

    args = parser.parse_args()

    # 标记模式
    if args.mark:
        ok = save_manifest(args.manifest, set(args.mark))
        if not ok:
            sys.exit(2)
        print(json.dumps({
            "action": "marked",
            "session_ids": args.mark,
            "manifest": args.manifest,
        }, ensure_ascii=False, indent=2))
        return

    if args.mark_current:
        sid = get_current_session_id(args.claude_dir, args.mark_current)
        if sid:
            ok = save_manifest(args.manifest, {sid})
            if not ok:
                sys.exit(2)
            print(json.dumps({
                "action": "marked_current",
                "session_id": sid,
                "cwd": args.mark_current,
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"error": f"未找到 {args.mark_current} 的会话文件"}, ensure_ascii=False))
        return

    if args.timerange:
        sid, fpath = _locate_current_session_file(args.claude_dir, args.timerange)
        if not sid or not fpath:
            print(json.dumps(
                {"error": f"未找到 {args.timerange} 的会话文件"},
                ensure_ascii=False,
            ))
            sys.exit(2)
        tr = _extract_timerange(fpath)
        if not tr:
            print(json.dumps(
                {"error": "无法从会话 JSONL 提取 timestamp", "session_id": sid},
                ensure_ascii=False,
            ))
            sys.exit(2)
        tr["session_id"] = sid
        print(json.dumps(tr, ensure_ascii=False, indent=2))
        return

    if args.touched_repos:
        sid, fpath = _locate_current_session_file(args.claude_dir, args.touched_repos)
        if not sid or not fpath:
            print(json.dumps(
                {"error": f"未找到 {args.touched_repos} 的会话文件"},
                ensure_ascii=False,
            ))
            sys.exit(2)

        touched_files = _extract_touched_files(fpath)
        repo_map = _aggregate_touched_repos(touched_files)
        cwd_repo = _locate_git_repo(args.touched_repos)

        # 组装 touched_repos 列表,按 file_count 降序排
        touched_repos_list = [
            {"toplevel": k, "branch": v["branch"], "file_count": v["file_count"]}
            for k, v in repo_map.items()
        ]
        touched_repos_list.sort(key=lambda x: -x["file_count"])

        # primary_repo: file_count 最多;并列时优先 cwd_repo;若没有任何 touched repo,回退到 cwd_repo
        primary = None
        if touched_repos_list:
            top_count = touched_repos_list[0]["file_count"]
            tied = [r for r in touched_repos_list if r["file_count"] == top_count]
            if cwd_repo and any(r["toplevel"] == cwd_repo["toplevel"] for r in tied):
                primary = cwd_repo["toplevel"]
            else:
                primary = tied[0]["toplevel"]
        elif cwd_repo:
            primary = cwd_repo["toplevel"]

        result = {
            "session_id": sid,
            "touched_file_count": len(touched_files),
            "cwd_repo": cwd_repo,
            "touched_repos": touched_repos_list,
            "primary_repo": primary,
            "cross_repo": bool(
                cwd_repo and primary and primary != cwd_repo["toplevel"]
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 扫描模式
    manifest = load_manifest(args.manifest)

    # 如果指定了 session ID，直接解析
    if args.session:
        results = []
        all_sessions = find_session_files(args.claude_dir, days=365)  # 搜索范围放大
        session_map = {s["session_id"]: s for s in all_sessions}
        for sid in args.session:
            if sid in session_map:
                s = session_map[sid]
                parsed = parse_session(s["file_path"], args.max_chars)
                results.append({
                    "session_id": sid,
                    "project": format_project_name(s["project"]),
                    "date": parsed.get("date") or datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M"),
                    "cwd": parsed.get("cwd", ""),
                    "first_intent": parsed.get("first_intent", ""),
                    "total_messages": parsed["total_messages"],
                    "conversation": parsed["messages"],
                    "timerange": parsed.get("timerange"),
                })
            else:
                results.append({"session_id": sid, "error": "未找到"})
        print(json.dumps({"sessions": results}, ensure_ascii=False, indent=2))
        return

    # 常规扫描
    sessions = find_session_files(args.claude_dir, args.days, args.project)
    unsummarized = [s for s in sessions if s["session_id"] not in manifest]
    # 过滤过短会话
    min_bytes = args.min_size * 1024
    unsummarized = [s for s in unsummarized if s["size"] > min_bytes]

    result = {
        "scan_range_days": args.days,
        "total_sessions_in_range": len(sessions),
        "already_summarized": len([s for s in sessions if s["session_id"] in manifest]),
        "unsummarized_count": len(unsummarized),
        "sessions": [],
    }

    for s in unsummarized:
        entry = {
            "session_id": s["session_id"],
            "project": format_project_name(s["project"]),
            "date": datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "size_kb": round(s["size"] / 1024, 1),
        }

        if args.parse:
            parsed = parse_session(s["file_path"], args.max_chars)
            entry.update({
                "cwd": parsed.get("cwd", ""),
                "first_intent": parsed.get("first_intent", ""),
                "total_messages": parsed["total_messages"],
                "conversation": parsed["messages"],
                "timerange": parsed.get("timerange"),
            })
        else:
            # 快速模式：只读取前几行获取主题
            parsed = parse_session(s["file_path"], max_chars=500)
            entry["first_intent"] = parsed.get("first_intent", "")
            entry["total_messages"] = parsed["total_messages"]
            entry["cwd"] = parsed.get("cwd", "")

        result["sessions"].append(entry)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
