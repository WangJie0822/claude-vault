"""笔记文件路径解析单点。

读端**所有以 cache 中 path 字段派生**的笔记文件读取都必须经过 resolve_note_path
（工作日志/项目目录扫描走 rglob 直接发现真实文件、不经不可信 cache path，合法不在此列）。
cache 中的 path 字段不可信（cache 是 Vault 内一个普通 JSON，可被篡改，
且会随 Vault git 仓库分发），而 pathlib 的拼接语义会让绝对路径整段替换、
`..` 不归一，故必须做容器校验。
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

_SEP_RE = re.compile(r"[\\/]")


def safe_rel_note_path(rel) -> bool:
    """纯字符串判据：cache 键能否当作 Vault 内的相对笔记路径（spec 2.4.0 §8.3，F23）。

    **在任何文件系统调用之前**执行：`Path.resolve()` / `is_file()` 碰到 UNC 路径会去连远端主机
    （Windows 上 `os.path.isfile` 对不可路由主机的 UNC 路径实测阻塞约 21 秒才返回，且每个主机
    各算一次；那是阻塞不是异常，`try/except` 救不了），
    而 cache 键是不可信输入。拒：非 str、空串、含 C0 控制字符、以斜杠或反斜杠开头（含 UNC 与
    verbatim 前缀）、含 `:`（盘符与 NTFS 备用数据流）、带盘符或 POSIX 绝对、任一段为 `..`、
    不以 `.md` 结尾（不区分大小写，与 resolve_note_path 原有判据一致）。
    """
    if not isinstance(rel, str) or not rel:
        return False
    if any(ord(c) < 0x20 for c in rel):
        return False
    if rel.startswith(("/", "\\")):
        return False
    if ":" in rel:
        return False
    if PureWindowsPath(rel).drive or PurePosixPath(rel).is_absolute():
        return False
    if any(seg == ".." for seg in _SEP_RE.split(rel)):
        return False
    return rel.lower().endswith(".md")


def resolve_note_path(vault_path: Path, rel: str) -> Path | None:
    """把 cache 中的相对路径解析为 Vault 内的真实笔记文件。

    返回 None 的情形：`safe_rel_note_path` 不通过（控制字符、绝对路径、UNC、`:`、`..`、非 .md，
    不碰文件系统）/ 越出 Vault / symlink 逃逸 / 不是普通文件 / 路径解析失败。调用方拿到 None
    时应跳过该条目，不得回退到裸拼接。
    """
    if not safe_rel_note_path(rel):
        return None
    try:
        # resolve(strict=False) 同时归一 `..` 与符号链接；strict=False 使不存在
        # 的路径也能归一（便于统一在下面用 is_relative_to 判定后再查存在性）。
        real_vault = Path(vault_path).resolve(strict=False)
        candidate = (real_vault / rel).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None
    try:
        if not candidate.is_relative_to(real_vault):
            return None
    except ValueError:
        return None
    if candidate == real_vault:
        return None
    try:
        if not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate
