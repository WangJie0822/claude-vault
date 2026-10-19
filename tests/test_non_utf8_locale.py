# -*- coding: utf-8 -*-
"""locale 不是 UTF-8 时仓库根两个运维脚本不崩（2.4.0 编码一族，批次 3a 普查遗留的两处）。

`scripts/context_vault_doctor.py` 以 `ensure_ascii=False` 打印 JSON 报告，含 HOME 下的 config 路径；
`scripts/migrate_settings.py` 打印 settings.json 的路径。HOME 路径里有 GBK 之外的汉字、stdout 按 GBK 编码时
（中文 Windows 默认代码页 936 下的管道），print 抛 `UnicodeEncodeError`、rc=1。

写法照抄各 skill 根的 `test_non_utf8_locale.py`：子进程先删掉 `PYTHONUTF8` 与 `PYTHONIOENCODING`（按名字大小写
不敏感），再设 `PYTHONIOENCODING=gbk`，三个平台上都能确定地造出「流不是 UTF-8」。`test_gbk_env_*` 是前提探针：
父进程先设上这两个变量，再核对子进程里的流确实是 gbk——删变量那一步失效时，其余用例会在 UTF-8 下跑、恒绿，
只有它会红。本根没有 HOME 隔离 fixture，子进程环境显式设 HOME / USERPROFILE。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "context_vault_doctor.py"
MIGRATE_SETTINGS = ROOT / "scripts" / "migrate_settings.py"
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


def _home(tmp_path: Path) -> Path:
    home = tmp_path / f"home{NON_GBK}"
    home.mkdir()
    return home


def test_gbk_env_really_makes_streams_gbk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前提探针：父进程设了两个编码变量，子进程里的流仍须是 gbk、UTF-8 模式关闭。"""
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    code = "import sys; print(sys.stdout.encoding, sys.stderr.encoding, sys.flags.utf8_mode)"
    r = _run([sys.executable, "-c", code], tmp_path)
    assert r.returncode == 0, _err(r)
    assert r.stdout.decode("ascii").split() == ["gbk", "gbk", "0"]


def test_context_vault_doctor_under_gbk_with_non_gbk_home(tmp_path: Path) -> None:
    """canonical config 在场：报告里打印它的路径（含 GBK 之外的汉字）不得崩，报告可按 UTF-8 解析。"""
    home = _home(tmp_path)
    cfg = home / ".context-vault" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    r = _run([sys.executable, str(DOCTOR)], home)
    assert r.returncode == 0, _err(r)
    report = json.loads(r.stdout.decode("utf-8"))
    assert NON_GBK in report["canonical_config"], report


def test_migrate_settings_dry_run_under_gbk_with_non_gbk_home(tmp_path: Path) -> None:
    """settings.json 不存在时 dry-run 打印它的默认路径（在 HOME 下）不得崩。"""
    home = _home(tmp_path)
    r = _run([sys.executable, str(MIGRATE_SETTINGS)], home)
    assert r.returncode == 0, _err(r)
    out = r.stdout.decode("utf-8")
    assert "nothing to migrate" in out and NON_GBK in out, out[-400:]
