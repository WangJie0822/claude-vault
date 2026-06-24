"""Zero-config: create vault dir + .meta/ if missing (idempotent, failure must not raise — callers use fail-open)."""
from pathlib import Path


def ensure_vault(vault_path: Path) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    (vault_path / ".meta").mkdir(parents=True, exist_ok=True)
