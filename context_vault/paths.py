"""Canonical and legacy Context Vault paths."""
from __future__ import annotations

from pathlib import Path


def context_home(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".context-vault"


def canonical_config(home: Path | None = None) -> Path:
    return context_home(home) / "config.json"


def legacy_loader_config(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "skills" / "vault-loader" / "config.json"


def default_vault(home: Path | None = None) -> Path:
    return context_home(home) / "knowledge-vault"


def legacy_default_vault(home: Path | None = None) -> Path:
    """0.9.x 的隐式默认 Vault。

    **不是历史包袱，是正确性所需**：0.9.x 的零配置用户盘上那份 config 是
    `_MINIMAL_STUB`（只有 `_config_version`/`_comment`，**不含 `vault_path`**），
    他们的笔记就落在这个路径下。若 legacy 配置也套用 `default_vault()` 的新默认，
    读端会静默改指一个不存在的新目录，而 `ensure_vault_if_default` 又会把它创建
    出来 —— 于是「零命中」与「知识库本来就是空的」在结果上完全不可区分。
    """
    return (home or Path.home()) / ".claude" / "knowledge-vault"


def migration_committed(home: Path | None = None) -> bool:
    """迁移是否已真正提交（`migrate_context_vault.py --apply` 成功跑完）。"""
    import json
    try:
        data = json.loads((context_home(home) / "migration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("status") == "committed"


def has_legacy_data(home: Path | None = None) -> bool:
    """是否存在 0.9.x 遗留数据。

    只做三次 stat（不 glob `projects/`）：这在每个 hook 进程里都会被调一次，
    而 `projects/` 下可能有几百个目录，未命中时要全遍历。两份 legacy config
    加 metrics 目录已足以判定「这台机器装过 0.9.x」。
    """
    base = home or Path.home()
    return (legacy_loader_config(base).exists()
            or (base / ".claude" / "skills" / "summarize-session" / "config.json").exists()
            or (base / ".claude" / "vault-loader-metrics").is_dir())


def use_canonical_namespace(home: Path | None = None) -> bool:
    """当前是否应使用 1.0 的 canonical 命名空间（state / metrics / session manifest）。

    ⚠️ **判据不是「canonical config 是否存在」。** `/summarize-session --set-default <path>`
    只写 `~/.context-vault/config.json`、**不搬任何数据**；拿 config 存在与否当判据，
    这个「改一下默认库路径」的动作就会把三处命名空间一起切到空目录：指标与不可再生的
    人工标注从报表里消失、`--catch-up` 把全部历史会话重新列一遍、注入去重重置——全程无提示。

    正确判据是显式的迁移完成标记（`migrate_context_vault.py` 提交时写的
    `migration.json::status == "committed"`）；没有 legacy 数据的全新安装则直接走
    canonical（没有要兼容的东西）。
    """
    if migration_committed(home):
        return True
    return not has_legacy_data(home)


def resolve_config_path(home: Path | None = None) -> tuple[Path, bool]:
    """Return (active path, is_fresh_canonical).

    Existing canonical data wins, then the legacy Claude config. A fresh install
    starts in the product-neutral home without moving any legacy data.
    """
    canonical = canonical_config(home)
    legacy = legacy_loader_config(home)
    if canonical.exists():
        return canonical, False
    if legacy.exists():
        return legacy, False
    return canonical, True
