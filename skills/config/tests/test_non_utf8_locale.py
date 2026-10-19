# -*- coding: utf-8 -*-
"""locale 不是 UTF-8 时 /config 的两个 CLI 不崩（2.4.0 full-review DO-H2）。

中文 Windows 默认代码页 936，管道下的 stdout 按 GBK 编码：清单里的 🔒（隐私旋钮标记）、`--json` 里的 −（U+2212），
以及 HOME 路径里 GBK 之外的汉字，都会让 print 抛 `UnicodeEncodeError`、rc=1、stdout 为空。作者机器设了用户级
`PYTHONIOENCODING=utf-8`，同目录 `test_inventory.py` 的两条 CLI 用例因此一直全绿。

子进程先删掉 `PYTHONUTF8` 与 `PYTHONIOENCODING`（按名字大小写不敏感），再设 `PYTHONIOENCODING=gbk`：三个平台上都能
确定地造出「流不是 UTF-8」，不依赖本机代码页。`test_gbk_env_*` 是前提探针：父进程先设上这两个变量，再核对子进程里的
流确实是 gbk——删变量那一步失效时，其余用例会在 UTF-8 下跑、恒绿，只有它会红。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = SKILL_ROOT / "scripts" / "config_inventory.py"
SET = SKILL_ROOT / "scripts" / "config_set.py"
NON_GBK = "\U00020bb7"  # 𠮷：CJK 扩展 B，不在 GBK 里
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


def test_gbk_env_really_makes_streams_gbk(_isolate_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前提探针：父进程设了两个编码变量，子进程里的流仍须是 gbk、UTF-8 模式关闭。"""
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    code = "import sys; print(sys.stdout.encoding, sys.stderr.encoding, sys.flags.utf8_mode)"
    r = _run([sys.executable, "-c", code], _isolate_home)
    assert r.returncode == 0, _err(r)
    assert r.stdout.decode("ascii").split() == ["gbk", "gbk", "0"]


def test_inventory_human_output_under_gbk(_isolate_home: Path) -> None:
    r = _run([sys.executable, str(INVENTORY)], _isolate_home)
    assert r.returncode == 0, _err(r)
    out = r.stdout.decode("utf-8")
    assert "🔒 隐私旋钮" in out, out[-400:]


def test_inventory_json_output_under_gbk(_isolate_home: Path) -> None:
    r = _run([sys.executable, str(INVENTORY), "--json"], _isolate_home)
    assert r.returncode == 0, _err(r)
    data = json.loads(r.stdout.decode("utf-8"))
    assert isinstance(data, list) and len(data) > 60


def test_set_under_gbk_with_non_gbk_home(tmp_path: Path) -> None:
    """HOME 路径含 GBK 之外的汉字：写入成功后打印「已写入 <路径>」不得崩，配置确实写进去了。"""
    home = tmp_path / f"home{NON_GBK}"
    home.mkdir()
    r = _run([sys.executable, str(SET), "user_prompt_submit.max_notes", "5", "--yes"], home)
    assert r.returncode == 0, _err(r)
    out = r.stdout.decode("utf-8")
    assert "已写入" in out and NON_GBK in out, out[-400:]
    cfg = json.loads((home / ".context-vault" / "config.json").read_text(encoding="utf-8"))
    assert cfg["user_prompt_submit"]["max_notes"] == 5
