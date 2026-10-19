# -*- coding: utf-8 -*-
"""locale 不是 UTF-8 时 obsidian_cli 与 scan_sessions 不崩（2.4.0 修复批次 3a 普查）。

中文 Windows 默认代码页 936，管道下的 stdout 按 GBK 编码：笔记文件名、正文与会话原文里的 emoji 会让 print 抛
`UnicodeEncodeError`、rc=1、stdout 为空。同根的 rebuild_index、keywords_gap 等早已在 `__main__` 里重配为 UTF-8，
这两个脚本没有跟上；作者机器设了用户级 `PYTHONIOENCODING=utf-8`，一直没暴露。

子进程先删掉 `PYTHONUTF8` 与 `PYTHONIOENCODING`（按名字大小写不敏感），再设 `PYTHONIOENCODING=gbk`：三个平台上都能
确定地造出「流不是 UTF-8」，不依赖本机代码页。本根没有隔离 HOME 的 autouse fixture，HOME / USERPROFILE 在子进程
环境里显式指向临时目录。`test_gbk_env_*` 是前提探针：父进程先设上这两个变量，再核对子进程里的流确实是 gbk。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROCKET = "\U0001f680"
TIMEOUT = 60

_ENCODING_VARS = ("PYTHONUTF8", "PYTHONIOENCODING")


def _gbk_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _ENCODING_VARS and not k.upper().startswith("GIT_")}
    env.update({"PYTHONIOENCODING": "gbk", "HOME": str(home), "USERPROFILE": str(home)})
    return env


def _run(argv: list, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, env=_gbk_env(home), cwd=str(home),
                          timeout=TIMEOUT)


def _err(r: subprocess.CompletedProcess) -> str:
    return r.stderr.decode("utf-8", errors="replace")[-600:]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_gbk_env_really_makes_streams_gbk(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前提探针：父进程设了两个编码变量，子进程里的流仍须是 gbk、UTF-8 模式关闭。"""
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    code = "import sys; print(sys.stdout.encoding, sys.stderr.encoding, sys.flags.utf8_mode)"
    r = _run([sys.executable, "-c", code], home)
    assert r.returncode == 0, _err(r)
    assert r.stdout.decode("ascii").split() == ["gbk", "gbk", "0"]


def test_obsidian_cli_read_emoji_note_under_gbk(home: Path, tmp_vault: Path) -> None:
    """降级文件 I/O 读一篇文件名与正文都含 emoji 的笔记：rc=0，JSON 里的正文原样。"""
    rel = f"笔记/{ROCKET}火箭.md"
    note = tmp_vault / rel
    note.parent.mkdir(parents=True)
    note.write_text(f"# 火箭\n\n正文 {ROCKET}\n", encoding="utf-8")
    r = _run([sys.executable, str(SCRIPTS / "obsidian_cli.py"), "--vault", str(tmp_vault),
              "read", "--path", rel], home)
    assert r.returncode == 0, _err(r)
    doc = json.loads(r.stdout.decode("utf-8", errors="replace"))
    assert f"正文 {ROCKET}" in (doc.get("data") or {}).get("content", ""), doc


def test_scan_sessions_parse_emoji_session_under_gbk(home: Path, tmp_path: Path) -> None:
    """`--parse --session` 解析一条含 emoji 的会话：rc=0，输出的对话原样含 emoji。"""
    sid = "-".join(("1b2c3d4e", "0000", "4000", "8000", "000000000001"))
    claude_dir = tmp_path / "claude"
    proj = claude_dir / "projects" / "D--fake-project"
    proj.mkdir(parents=True)
    line = {"type": "user", "sessionId": sid, "cwd": str(home), "timestamp": "2026-10-19T00:00:00Z",
            "message": {"role": "user", "content": f"{ROCKET} 精排补充怎么做"}}
    (proj / f"{sid}.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    r = _run([sys.executable, str(SCRIPTS / "scan_sessions.py"), "--parse", "--session", sid,
              "--claude-dir", str(claude_dir), "--manifest", str(tmp_path / "manifest.json")], home)
    assert r.returncode == 0, _err(r)
    out = r.stdout.decode("utf-8", errors="replace")
    assert f"{ROCKET} 精排补充怎么做" in out, out[-600:]
