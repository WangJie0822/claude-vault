#!/usr/bin/env python3
"""Build a clean, standard Codex marketplace from the committed tree."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = (
    ".codex-plugin/plugin.json",
    "context_vault/runtime.py",
    "hooks/hooks.json",
    "skills/vault/SKILL.md",
    "skills/vault-loader/SKILL.md",
    "skills/summarize-session/SKILL.md",
    "VERSION",
    "AGENTS.md",
)
SKIP_PREFIXES = ("packaging/", "docs/superpowers/")
SENSITIVE = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{16,}"),
    re.compile(rb"(?i)[A-Z]:\\Users\\[^\\\s]+\\"),
)
# 脱敏扫描豁免：脱敏器自身的测试夹具必然含「看起来像密钥」的字面量。
# 按**完整相对路径**精确豁免，不用目录前缀——目录级豁免会让该目录下将来新增的
# 任何文件自动免检，那正是把闸门变成盲区的方式。
SCAN_EXEMPT = (
    "skills/summarize-session/tests/test_sensitive_patterns.py",
)


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          check=True).stdout


def inventory(*, allow_dirty: bool = False) -> list[Path]:
    relative = [p for p in _git("ls-files", "-z").decode(
        "utf-8", "surrogateescape").split("\0") if p]
    tracked = set(relative)
    missing = [path for path in REQUIRED if path not in tracked]
    if missing and not allow_dirty:
        raise RuntimeError(f"required release files are not tracked: {', '.join(missing)}")
    if not allow_dirty:
        dirty = _git("status", "--porcelain", "--untracked-files=all").decode(
            "utf-8", "replace").strip()
        if dirty:
            raise RuntimeError("release build requires a clean working tree")
    if allow_dirty:
        relative.extend(path for path in REQUIRED if (ROOT / path).is_file())
        for directory in (ROOT / ".codex-plugin", ROOT / "context_vault"):
            if directory.is_dir():
                relative.extend(p.relative_to(ROOT).as_posix() for p in directory.rglob("*")
                                if p.is_file() and "__pycache__" not in p.parts)
    return sorted({ROOT / p for p in relative
                   if not p.startswith(SKIP_PREFIXES)})


def _release_gate_patterns() -> tuple:
    """软依赖发布闸门的完整私人内容清单（作者标识、内网域名、真实 session UUID…）。

    那份清单在 `packaging/`（不分发），所以**只能是软依赖**：作者机器上拿得到，
    分发出去的副本拿不到就只跑本文件自带的通用集。

    为什么要复用而不是各写各的：两条发布通路的论域完全相同（都是 `git ls-files`），
    差别只在 pattern 强度。不复用的话，同一份内容走 Claude 发布会被拦、走 Codex
    发布却放行——而后者恰恰是 1.0 的主打通路。
    """
    try:
        sys.path.insert(0, str(ROOT / "packaging"))
        from build_plugin import SECRET_PATTERNS  # type: ignore[import-not-found]
        return tuple(SECRET_PATTERNS)
    except Exception:
        return ()


def _scan(sources: list[Path]) -> None:
    """扫描**将要进入产物的源文件**。

    对源扫描而不是对产物扫描：命中时产物尚未落盘，不会留下一个含敏感内容的
    半成品目录（而重跑还会因 `FileExistsError` 被拒、逼用户手工清理）。

    豁免清单自带两道防腐：清单项不在论域内 ⇒ 报错（防止指向已删除文件的死条目）；
    清单项实际未命中任何 pattern ⇒ 报错（说明它已不需要豁免，留着就是一条无人
    知晓的免检通道）。两者都让「豁免」无法悄悄变成「盲区」。
    """
    by_rel = {source.relative_to(ROOT).as_posix(): source for source in sources}
    stale = [rel for rel in SCAN_EXEMPT if rel not in by_rel]
    if stale:
        raise RuntimeError(
            f"scan exemption points at files outside the artifact: {', '.join(stale)}")
    extra = _release_gate_patterns()
    if not extra:
        print("[build_codex_artifact] 未找到发布闸门 pattern（packaging/ 不随插件分发），"
              "本次仅跑通用敏感内容集", file=sys.stderr)
    findings: list[str] = []
    unused: list[str] = []
    for rel, source in sorted(by_rel.items()):
        data = source.read_bytes()
        binary = b"\x00" in data[:4096]
        hit = (not binary) and (
            any(pattern.search(data) for pattern in SENSITIVE)
            or any(pattern.search(data.decode("utf-8", "replace")) for pattern in extra))
        if rel in SCAN_EXEMPT:
            if not hit:
                unused.append(rel)
        elif hit:
            findings.append(rel)
    if unused:
        raise RuntimeError(
            f"scan exemption no longer needed, remove it: {', '.join(unused)}")
    if findings:
        raise RuntimeError(f"sensitive content found in artifact: {', '.join(findings)}")


def build(output: Path, *, allow_dirty: bool = False) -> Path:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    sources = inventory(allow_dirty=allow_dirty)
    _scan(sources)
    plugin_root = output / "plugins" / "context-vault"
    for source in sources:
        target = plugin_root / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    marketplace = {
        "name": "context-vault-local",
        "interface": {"displayName": "Context Vault Local"},
        "plugins": [{
            "name": "context-vault",
            "source": {"source": "local", "path": "./plugins/context-vault"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }],
    }
    market = output / ".agents" / "plugins" / "marketplace.json"
    market.parent.mkdir(parents=True, exist_ok=True)
    market.write_text(json.dumps(marketplace, ensure_ascii=False, indent=2), encoding="utf-8")
    return plugin_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    print(build(args.output, allow_dirty=args.allow_dirty))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
