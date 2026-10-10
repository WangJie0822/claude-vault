"""Windows 上按程序名起子进程时，不从当前目录解析可执行文件（CWE-427）。

- ``harden_exe_search()``：在本进程环境里设 ``NoDefaultCurrentDirectoryInExePath``。设了之后
  CreateProcess、cmd.exe 与 Python 3.12+ 的 ``shutil.which`` 都不再先搜当前目录，子进程继承。
  Windows 只看变量是否存在、不看取值，所以用 ``setdefault``，不覆盖已有的值。
- ``which_no_cwd(name)``：Python 3.9–3.11 的 ``shutil.which`` 在 Windows 上无条件先搜当前
  目录、不认上面那个变量。结果是相对路径或落在当前目录时，改为只在 PATH 的绝对路径条目里重找。

POSIX 上 ``harden_exe_search()`` 从 PATH 里去掉空条目与相对条目（它们指向当前目录），
``which_no_cwd`` 原样返回 ``shutil.which`` 的结果。平台按 ``sys.platform`` 判，与调用方的平台分支一致。

边界：只保护按名字解析出的第一跳；子进程自己再按名字起程序时，靠的是它继承的环境（变量与过滤后的 PATH）。
``context_vault/exe_search.py`` 是同一份实现（本目录的脚本不保证能导入 context_vault），
两份的函数定义由仓库根 ``tests/test_exe_search.py`` 守卫保持一致。
"""
from __future__ import annotations

import os
import shutil
import sys

_VAR = "NoDefaultCurrentDirectoryInExePath"


def harden_exe_search() -> None:
    """Windows：设变量，让 CreateProcess / cmd.exe 起的子进程不再隐式搜当前目录。
    POSIX：从 PATH 里去掉空条目与相对条目（它们指向当前目录）。"""
    if sys.platform == "win32":
        os.environ.setdefault(_VAR, "1")
        return
    raw = os.environ.get("PATH")
    if raw is not None:
        safe = _posix_safe_path(raw)
        if safe != raw:
            os.environ["PATH"] = safe


def _posix_safe_path(value: str) -> str:
    """只保留以 / 开头的 PATH 条目；一条不剩时回落到 /usr/bin:/bin。"""
    kept = [d for d in value.split(":") if d.startswith("/")]
    return ":".join(kept) or "/usr/bin:/bin"


def which_no_cwd(name: str) -> str | None:
    """Windows 上结果是相对路径或落在当前目录时，只在 PATH 的有根条目里重找；其余原样返回 shutil.which 的结果。"""
    found = shutil.which(name)
    if found is None or sys.platform != "win32":
        return found
    if _rooted(found) and not _in_cwd(found):
        return found
    return _search_path_only(name)


def _rooted(path: str) -> bool:
    """`\\x` 这类有根无盘符的路径相对的是盘符根而不是当前目录；3.13 起 ntpath.isabs 对它返回 False。"""
    return os.path.isabs(path) or path.startswith(("/", "\\"))


def _in_cwd(path: str) -> bool:
    try:
        here = os.path.normcase(os.path.abspath(os.getcwd()))
        return os.path.normcase(os.path.dirname(os.path.abspath(path))) == here
    except OSError:
        return True


def _search_path_only(name: str) -> str | None:
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    names = ([name] if os.path.splitext(name)[1] else []) + [name + e for e in exts]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not _rooted(d):
            continue
        for n in names:
            cand = os.path.join(d, n)
            if os.path.isfile(cand):
                return cand
    return None
