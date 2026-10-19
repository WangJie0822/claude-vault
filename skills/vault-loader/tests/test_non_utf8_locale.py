# -*- coding: utf-8 -*-
"""locale 不是 UTF-8 时的编码守卫（2.4.0 full-review BP-H1、DO-H2）。

中文 Windows 默认 ANSI 代码页 936，文本模式的标准流按 GBK 编解码；而宿主交给 hook 的 JSON 是 UTF-8，读这些输出的
调用方（Claude Code、它的 Bash 工具）也按 UTF-8 读。作者机器设了用户级 `PYTHONIOENCODING=utf-8`，既有集成用例又给
子进程设 `PYTHONUTF8=1`，两者都替生产环境补上了「流是 UTF-8」这个前提，缺陷因此一直全绿。

本文件的子进程先删掉 `PYTHONUTF8` 与 `PYTHONIOENCODING`（按名字大小写不敏感），再设 `PYTHONIOENCODING=gbk`：三个平台
上都能确定地造出「流不是 UTF-8」，不依赖本机代码页。它与真实的中文 Windows 有一处不同：显式指定编码时 stdin 的错误
处理是 strict（直接抛 `UnicodeDecodeError`），按 locale 时是 surrogateescape（静默解成乱码）。两种情形下中文提问都召回
不到笔记，按字节读 stdin 之后两种都不再发生。

`test_gbk_env_*` 是这组用例的前提探针：父进程先设上这两个变量，再核对子进程里的流确实是 gbk。删变量那一步失效时
（例如只删大写名），其余用例会在 UTF-8 下跑、恒绿，只有它会红。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._neutral import NEUTRAL_CWD

VL_ROOT = Path(__file__).resolve().parents[1]
UPS = VL_ROOT / "scripts" / "prompt_submit_load.py"
SS = VL_ROOT / "scripts" / "session_start_load.py"
ANALYZE = VL_ROOT / "scripts" / "analyze_metrics.py"
TIMEOUT = 60

_ENCODING_VARS = ("PYTHONUTF8", "PYTHONIOENCODING")


def _gbk_env(home: Path) -> dict:
    """子进程环境：删掉两个编码变量后设 gbk；HOME 指向临时目录；PATH 指向空目录（精排默认开，不能真拉起
    付费的 `claude`，`spawn_fine` 找不到它就记 no_claude）。"""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _ENCODING_VARS and not k.upper().startswith("GIT_")}
    empty = home / "emptybin"
    empty.mkdir(exist_ok=True)
    env.update({"PYTHONIOENCODING": "gbk", "HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": str(empty)})
    env.pop("VAULT_LOADER_DISABLE", None)
    return env


def _run(argv: list, home: Path, stdin: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, env=_gbk_env(home),
                          cwd=str(home), timeout=TIMEOUT)


def _stderr(r: subprocess.CompletedProcess) -> str:
    return r.stderr.decode("gbk", errors="replace")


def _write_config(home: Path, vault: Path) -> None:
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"vault_path": str(vault)}, ensure_ascii=False),
                                   encoding="utf-8")


def _note(vault: Path, rel: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {Path(rel).stem}\n\n正文\n", encoding="utf-8")


def _hook_output(r: subprocess.CompletedProcess) -> dict:
    out = r.stdout.decode("utf-8", errors="replace").strip()
    assert out, f"hook 没有输出（rc={r.returncode}）。stderr：{_stderr(r)}"
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    assert isinstance(doc, dict), f"stdout 不是合法 JSON：{out[:300]}"
    return doc


def _context(doc: dict) -> str:
    return (doc.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_gbk_env_really_makes_streams_gbk(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前提探针：父进程设了两个编码变量（作者机器与既有集成用例的形态），子进程里的流仍须是 gbk、UTF-8 模式关闭。"""
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    code = "import sys; print(sys.stdin.encoding, sys.stdout.encoding, sys.flags.utf8_mode)"
    r = _run([sys.executable, "-c", code], tmp_home)
    assert r.returncode == 0, _stderr(r)
    assert r.stdout.decode("ascii").split() == ["gbk", "gbk", "0"]


def test_ups_cjk_prompt_recalls_cjk_note_under_gbk(tmp_home: Path, tmp_vault: Path,
                                                   write_frontmatter_cache) -> None:
    """BP-H1：中文提问命中中文关键词的笔记。按 locale 读 stdin 时提问被解坏，这一篇召回不到。"""
    _note(tmp_vault, "笔记/精排补充.md")
    _note(tmp_vault, "笔记/红烧肉.md")
    write_frontmatter_cache({
        "笔记/精排补充.md": {"category": "笔记", "mtime": 1900000000, "tags": ["设计"],
                          "keywords": ["精排补充"], "summary": "精排补充的设计取舍与实现细节，摘要足够长足够长"},
        "笔记/红烧肉.md": {"category": "笔记", "mtime": 1900000000, "tags": ["烹饪"],
                        "keywords": [], "summary": "红烧肉的做法与火候控制要点，摘要足够长足够长"},
    })
    _write_config(tmp_home, tmp_vault)
    payload = {"cwd": str(NEUTRAL_CWD), "prompt": "精排补充 怎么设计", "session_id": "sess-gbk-ups",
               "prompt_id": "pid-gbk-ups", "hook_event_name": "UserPromptSubmit", "transcript_path": ""}
    r = _run([sys.executable, str(UPS)], tmp_home,
             json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert r.returncode == 0, _stderr(r)
    ctx = _context(_hook_output(r))
    assert "「精排补充」" in ctx, f"中文提问没有召回中文关键词的笔记。stderr：{_stderr(r)}"
    assert "红烧肉" not in ctx


def test_session_start_cjk_cwd_under_gbk(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> None:
    """BP-H1：cwd 含中文时项目目录信号照常生效，注入里的 cwd 原样是中文。按 locale 读 stdin 时 cwd 被解坏，
    `项目笔记/<目录名>/` 对不上，这一轮没有注入。"""
    proj = tmp_home / "精排项目"
    proj.mkdir()
    _note(tmp_vault, "项目笔记/精排项目/设计.md")
    write_frontmatter_cache({
        "项目笔记/精排项目/设计.md": {"category": "项目笔记", "mtime": 1900000000, "tags": ["设计"],
                                "summary": "精排项目的设计文档，摘要足够长足够长足够长"},
    })
    _write_config(tmp_home, tmp_vault)
    payload = {"cwd": str(proj), "hook_event_name": "SessionStart", "session_id": "sess-gbk-ss",
               "source": "startup", "transcript_path": ""}
    r = _run([sys.executable, str(SS)], tmp_home,
             json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert r.returncode == 0, _stderr(r)
    ctx = _context(_hook_output(r))
    assert f"当前 cwd: {proj}" in ctx, f"注入里的 cwd 不是原样中文：{ctx[:400]}"
    assert "精排项目的设计文档" in ctx, f"项目目录信号没有生效：{ctx[:400]}"


def test_analyze_metrics_report_under_gbk(tmp_home: Path) -> None:
    """DO-H2：`--report` 首行是 📊，按 GBK 编码 stdout 时 rc=1、stdout 为空。"""
    r = _run([sys.executable, str(ANALYZE), "--report"], tmp_home)
    assert r.returncode == 0, r.stderr.decode("utf-8", errors="replace")[-600:]
    assert "📊 vault-loader 指标报表" in r.stdout.decode("utf-8", errors="replace")
