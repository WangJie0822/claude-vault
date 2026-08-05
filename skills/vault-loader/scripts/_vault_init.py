"""Zero-config vault dir creation (idempotent, failure must not raise — callers use fail-open)."""
import os
from pathlib import Path

from scripts._config_loader import DEFAULT_CONFIG


def ensure_vault(vault_path: Path) -> None:
    """无条件创建 vault 目录 + .meta/。**只应由 ensure_vault_if_default 调用**——
    hook 直接调用会违反「vault-loader 对 Vault 只读」不变量，见下。"""
    vault_path.mkdir(parents=True, exist_ok=True)
    (vault_path / ".meta").mkdir(parents=True, exist_ok=True)


def _same_path(a: Path, b: Path) -> bool:
    """路径等价判定（expanduser + resolve + Windows 大小写归一）。
    任何异常一律判「不等价」——不确定时**倾向不创建**，符合只读不变量。"""
    try:
        return (os.path.normcase(str(a.expanduser().resolve()))
                == os.path.normcase(str(b.expanduser().resolve())))
    except (OSError, ValueError):
        return False


def ensure_vault_if_default(vault_path: Path) -> bool:
    """仅当 vault_path 等于 DEFAULT_CONFIG 的默认值时才自动创建，返回是否创建过。

    为什么不能无条件创建（不变量 #3）：CLAUDE.md 硬约束「vault-loader 对 Vault 只读；
    summarize-session 是唯一写入方」。更要命的是无条件 mkdir 会**掩盖失效**——config
    损坏时 `load_config` 回退默认值，vault_path 随之变成默认路径，此时 mkdir 会把那个
    错误路径连同 `.meta/` 建出来：现场看起来像一次正常的新安装，而用户真实 vault
    完全没被读，问题被彻底掩盖。

    保留的唯一写场景是零配置新装（vault_path 还是默认值、用户从未配置过），
    这既是 loader 自身的落地点、也不会踩到任何用户既有目录。
    """
    if not _same_path(vault_path, Path(DEFAULT_CONFIG["vault_path"])):
        return False
    ensure_vault(vault_path)
    return True
