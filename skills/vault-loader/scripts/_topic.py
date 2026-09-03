# -*- coding: utf-8 -*-
"""会话主题词：产生（detached spawn）与存取（搭 state 文件）。

为什么搭 state 文件而不新建目录：CLAUDE.md 记过「切一层 session 会让 sessions
目录单调增长无清理」。state 文件已有单 timestamp 控 TTL 与 MAX_STATE_BYTES 膨胀
保护，topics 再限最近 MAX_TOPIC_SESSIONS 个 session，文件大小即有界。

为什么独立成文件而不并进 _state.py：本模块除读写外还要管理子进程，
把「拉起进程」混进「读写 JSON」会让后者的职责失焦。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ._state import MAX_STATE_BYTES, state_path_for_cwd, update_json

MAX_TOPIC_SESSIONS = 5      # topics 字典最多保留几个 session（文件大小有界的保证）
MAX_TOPIC_WORDS = 8         # 每个会话最多几个主题词（与 spec 的 3-8 对齐）
MAX_TOPIC_WORD_LEN = 100    # 每个词的最大长度（UTF-8 中文 3B/字 ⇒ 最多 300B；
                            # 5×8×300B=12KB，远小于 102KB 上限 ⇒ topics 体量恒有界）


def load_session_topic(cwd: Path, session_id: str, ttl_hours: float) -> list[str]:
    """读该会话的主题词。缺失 / 损坏 / 过期 / 结构不对 → 空列表，绝不抛异常。"""
    try:
        if not session_id:
            return []
        p = state_path_for_cwd(cwd)
        if not p.exists() or p.stat().st_size > MAX_STATE_BYTES:
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        topics = data.get("topics")
        if not isinstance(topics, dict):
            return []
        entry = topics.get(session_id)
        if not isinstance(entry, dict):
            return []
        ts = entry.get("ts", 0)
        if not isinstance(ts, (int, float)) or time.time() - ts > ttl_hours * 3600:
            return []
        words = entry.get("words")
        if not isinstance(words, list):
            return []
        return [w for w in words if isinstance(w, str) and w][:MAX_TOPIC_WORDS]
    except Exception as exc:                      # noqa: BLE001 — fail-open
        print(f"[vault-loader] 读会话主题失败：{exc}", file=sys.stderr)
        return []


def has_recent_topic_attempt(cwd: Path, session_id: str, ttl_hours: float) -> bool:
    """该会话是否已有一次提炼尝试记录（成功或失败）且未过期。

    F2（整分支终审，2026-09-02）：`run_extraction_child` 现在无论提炼成功与否都会
    落一个带 `ts` 的标记（失败时 `words` 为空列表）。`load_session_topic` 对「失败」
    与「从未尝试过 / 已过期」返回的都是同一个 `[]`，无法用它的返回值区分——必须
    单独判「entry 是否存在且未过期」，不看 `words` 内容。

    调用方拿它做 spawn 前置门禁：`not topic_words and not has_recent_topic_attempt(...)`
    才允许再次拉起子进程，使持续提炼失败的代价从「每一轮 UPS 都 spawn」收敛为
    「每个 TTL 窗口最多一次」。缺失 / 损坏 / 结构不对 → False（fail-open，允许尝试，
    与 `load_session_topic` 同一降级方向）。
    """
    try:
        if not session_id:
            return False
        p = state_path_for_cwd(cwd)
        if not p.exists() or p.stat().st_size > MAX_STATE_BYTES:
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        topics = data.get("topics")
        if not isinstance(topics, dict):
            return False
        entry = topics.get(session_id)
        if not isinstance(entry, dict):
            return False
        ts = entry.get("ts", 0)
        if not isinstance(ts, (int, float)):
            return False
        return time.time() - ts <= ttl_hours * 3600
    except Exception:                              # noqa: BLE001 — fail-open
        return False


def _clean_words(words) -> list[str]:
    out: list[str] = []
    try:
        for w in (words or []):
            if isinstance(w, str) and w.strip():
                # 截断到 MAX_TOPIC_WORD_LEN，防止 topics 体量溢出导致整份 state 被重置
                truncated = w.strip()[:MAX_TOPIC_WORD_LEN]
                # 过滤 ANSI 转义序列与控制字符
                cleaned = truncated
                cleaned = re.sub(r'\x1b\[[0-9;]*m', '', cleaned)  # 删除 ANSI 转义序列
                cleaned = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', cleaned)  # 删除其他控制字符
                if cleaned:
                    out.append(cleaned)
    except Exception:                              # noqa: BLE001
        return []
    return out[:MAX_TOPIC_WORDS]


def save_session_topic(cwd: Path, session_id: str, words) -> None:
    """写该会话的主题词。超过 MAX_TOPIC_SESSIONS 时按 ts 淘汰最旧的。"""
    try:
        if not session_id:
            return
        cleaned = _clean_words(words)
        now = time.time()

        def mutate(existing: dict) -> dict:
            payload = dict(existing)
            topics = payload.get("topics")
            if not isinstance(topics, dict):
                topics = {}
            topics = {k: v for k, v in topics.items() if isinstance(v, dict)}
            topics[session_id] = {"words": cleaned, "ts": now}
            if len(topics) > MAX_TOPIC_SESSIONS:
                # 转换为带索引的列表，使 ts 相同时后插入的胜出
                items_with_idx = [(i, k, v) for i, (k, v) in enumerate(topics.items())]
                # 按 ts 升序排列，二级键是插入顺序（升序），取最后 MAX_TOPIC_SESSIONS 个
                ordered = sorted(
                    items_with_idx,
                    key=lambda item: (
                        item[2].get("ts", 0) if isinstance(item[2].get("ts", 0), (int, float)) else 0,
                        item[0]
                    ))
                topics = dict((k, v) for _, k, v in ordered[-MAX_TOPIC_SESSIONS:])

            payload["topics"] = topics
            # F5（整分支终审，2026-09-02）：既有两个写入方（_state.py:save_fallback_ts /
            # save_diag_ts）一律 `setdefault("timestamp", 0)`，理由就写在它们旁边——
            # 不得刷新 paths 的 timestamp，否则会变相续命注入去重 TTL。此前这里传 `now`，
            # 今天无害（`timestamp` 字段后续总会被 save_injected 无条件覆盖），但背离了
            # 正是为防这类 bug 而立的约定，故对齐改成 0。
            payload.setdefault("timestamp", 0)
            return payload

        update_json(state_path_for_cwd(cwd), mutate, max_bytes=MAX_STATE_BYTES)
    except Exception as exc:                       # noqa: BLE001 — fail-open
        print(f"[vault-loader] 写会话主题失败：{exc}", file=sys.stderr)


# -------- spawn 与子进程 --------

TOPIC_MODEL = "haiku"
TOPIC_TIMEOUT_SEC = 120        # 子进程自杀上限；实测 LLM 中位 21s，给 6 倍余量
_NUM_PREFIX = re.compile(r"^\s*\d+\s*[.、)]\s*")
_LABEL_PREFIX = re.compile(r"^\s*(关键词|主题词|主题|keywords?)\s*[:：]\s*", re.I)

_PROMPT_TPL = """从下面这段会话开头提炼 3-8 个用于检索本地知识库的中文关键词。
只输出关键词本身，用逗号分隔，不要编号、不要解释、不要任何前缀。

## 用户的提问
{prompt}

## 知识库里按关键词粗筛出的候选笔记（仅供理解话题范围）
{cands}
"""


def build_topic_prompt(prompt: str, candidates) -> str:
    lines = []
    for path, summary in (candidates or []):
        lines.append(f"- {path}：{summary}")
    return _PROMPT_TPL.format(prompt=(prompt or "").strip(),
                              cands="\n".join(lines) or "（无）")


def parse_topic_words(raw) -> list[str]:
    """模型输出 → 词表。剥编号/标签前缀，按逗号顿号切分，去重保序，封顶。

    为防止恶意或失控输出导致解析卡顿，限制输入到前 10KB（足以容纳 ~1000 个合理词）。
    控制字符与 ANSI 转义序列会被剥掉。
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    # 截断输入防止 O(n²) 复杂度：10KB 足以容纳合理的模型输出
    raw = raw[:10240]
    # 过滤控制字符与 ANSI 转义序列：删除 ANSI，但控制字符替换为空格以保留词分隔
    raw = re.sub(r'\x1b\[[0-9;]*m', '', raw)  # 删除 ANSI 转义序列
    raw = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', ' ', raw)  # 控制字符→空格
    out: list[str] = []
    seen = set()
    for line in raw.splitlines():
        line = _NUM_PREFIX.sub("", _LABEL_PREFIX.sub("", line)).strip()
        if not line:
            continue
        # 按逗号、顿号、分号以及空格分割（控制字符已替换为空格）
        for piece in re.split(r"[\s,，、;；]+", line):
            w = piece.strip()
            if w and w not in seen:
                out.append(w)
                seen.add(w)
                if len(out) >= MAX_TOPIC_WORDS:
                    return out
    return out


def _call_model(prompt_text: str) -> str | None:
    """同步调模型。**只在子进程里跑**，父进程永远不碰它。

    刻意不复用 context_vault.model.call_claude：那个是同步语义没错，但它走
    choose_backend，而后者在 hook 环境抛 ValueError（PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT
    不在 hook 子进程 env）。这里直接固定 argv。
    """
    exe = shutil.which("claude")
    if exe is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"          # 防递归：子进程不得再触发本 hook
    try:
        r = subprocess.run(
            [exe, "-p", "--model", TOPIC_MODEL, "--tools", "",
             "--no-session-persistence"],
            input=prompt_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TOPIC_TIMEOUT_SEC,
            env=env, shell=False)
    except Exception:                           # noqa: BLE001
        return None
    return r.stdout if r.returncode == 0 else None


def spawn_topic_extraction(cwd: Path, session_id: str, prompt: str,
                           candidates, config: dict) -> bool:
    """detached 拉起提炼子进程，**立即返回**，不等待。

    UPS 有 300ms 预算而 LLM 中位 21s，故只能异步。父进程不读子进程 stdout：
    结果由子进程自己写进 state。

    F4（整分支终审，2026-09-02）：prompt 原文与候选笔记路径+摘要经 **stdin**
    传给子进程，不放 argv——argv 在本机进程表全程可见（如 `ps`/任务管理器）且无
    长度上限，与本项目 metrics 层"只存加盐 hash、刻意不落 transcript_path"的隐私
    口径不一致。argv 只留 cwd/session_id 两个非敏感定位参数。
    """
    try:
        if shutil.which("claude") is None:
            return False
        stdin_payload = json.dumps({
            "prompt": prompt or "",
            "candidates": [[p, s] for p, s in (candidates or [])],
        }, ensure_ascii=False)
        argv = [sys.executable, "-m", "scripts._topic", str(cwd), session_id]
        kwargs: dict = {"stdin": subprocess.PIPE,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                        "cwd": str(Path(__file__).resolve().parents[1])}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 — argv 全部由本模块构造
        try:
            # 子进程已 detach、独立运行；父进程写 stdin 失败（管道已被回收等）不影响
            # 子进程本身，不应因此把整个 spawn 判定为失败。
            proc.stdin.write(stdin_payload.encode("utf-8"))
            proc.stdin.close()
        except Exception:                        # noqa: BLE001
            pass
        return True
    except Exception as exc:                     # noqa: BLE001 — fail-open
        print(f"[vault-loader] 主题提炼拉起失败：{exc}", file=sys.stderr)
        return False


def run_extraction_child(argv: list[str], stdin_text: str | None = None) -> int:
    """子进程入口：从 stdin 读 prompt/候选 → 调模型 → 解析 → 写 state。
    永远返回 0（不给父进程添乱）。

    `stdin_text` 是控制反转参数：生产不传（None）时真读 `sys.stdin`；测试可直接
    传入字符串，不必猴补 `sys.stdin`。

    F2（整分支终审，2026-09-02）：无论提炼成功、失败、还是中途抛出未预期异常，
    都会调用 `save_session_topic` 落一个带当前 `ts` 的标记（失败/异常时 `words`
    为空列表）。此前失败时什么都不写，使 `prompt_submit_load.py` 里 `not topic_words`
    的 spawn 门禁恒为真，持续失效场景下**每一轮** UPS 都重新拉起一个子进程、无上限
    （真实上界是完整 LLM 时延，中位 21s / 超时 120s，不是先前注释估计的
    "同会话 30 秒内"）。现在配合 `has_recent_topic_attempt` 一起用，代价收敛为
    「每个 TTL 窗口最多一次」——即使 `_call_model`/`parse_topic_words` 内部
    抛出了 `_call_model` 自身 fail-open 契约本不该放出的异常，也不例外
    （故取词与落盘分两个独立 try 块，取词失败不得连累落盘）。
    """
    try:
        cwd, session_id = argv[0], argv[1]
    except Exception:                            # noqa: BLE001 — argv 不足，无处可写
        return 0
    words: list[str] = []
    try:
        if stdin_text is None:
            try:
                stdin_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            except Exception:                    # noqa: BLE001
                stdin_text = ""
        try:
            payload = json.loads(stdin_text) if (stdin_text or "").strip() else {}
        except Exception:                        # noqa: BLE001
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str):
            prompt = ""
        raw_cands = payload.get("candidates", [])
        cands: list[tuple[str, str]] = []
        if isinstance(raw_cands, list):
            for item in raw_cands:
                if (isinstance(item, (list, tuple)) and len(item) == 2
                        and isinstance(item[0], str) and isinstance(item[1], str)):
                    cands.append((item[0], item[1]))
        raw = _call_model(build_topic_prompt(prompt, cands))
        words = parse_topic_words(raw)
    except Exception:                            # noqa: BLE001 — words 保持 []
        words = []
    try:
        save_session_topic(Path(cwd), session_id, words)
    except Exception:                            # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(run_extraction_child(sys.argv[1:]))
