"""笔记文件路径解析单点。

读端**所有以 cache 中 path 字段派生**的笔记文件读取都必须经过 resolve_note_path
（工作日志/项目目录扫描走 rglob 直接发现真实文件、不经不可信 cache path，合法不在此列）。
cache 中的 path 字段不可信（cache 是 Vault 内一个普通 JSON，可被篡改，
且会随 Vault git 仓库分发），而 pathlib 的拼接语义会让绝对路径整段替换、
`..` 不归一，故必须做容器校验。
"""
from __future__ import annotations

from pathlib import Path


def resolve_note_path(vault_path: Path, rel: str) -> Path | None:
    """把 cache 中的相对路径解析为 Vault 内的真实笔记文件。

    返回 None 的情形：非 .md / 越出 Vault / symlink 逃逸 / 不是普通文件 /
    路径解析失败。调用方拿到 None 时应跳过该条目，不得回退到裸拼接。
    """
    if not isinstance(rel, str) or not rel:
        return None
    if not rel.lower().endswith(".md"):
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
