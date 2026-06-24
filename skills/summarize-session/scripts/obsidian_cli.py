"""Obsidian CLI 封装层：探测、子进程调用、超时保护、降级到文件 I/O。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OBSIDIAN_PROC = "Obsidian"
DEFAULT_TIMEOUT = 5.0
# Windows 上 Obsidian 1.12.x CLI single-instance forward 协议在大 argv 下崩主进程
# （envelope JSON 多关一次 `]` → socket 接收端 JSON.parse 失败）
# 实证：单次 8KB content 必触发；4KB 作保守阈值兜底。命中时跳过 CLI 直接走 fallback 文件 I/O。
# 注意：阈值按 UTF-8 byte 数判断而非字符数——中文 1 字符 ≈ 3 字节，
# 若按字符数判断会漏判中文笔记（实证：5767 byte 中文笔记 = 3998 字符 < 4096 字符阈值，
# 漏判后走 CLI 通路触发崩溃）。
WIN_LARGE_CONTENT_BYTES = 4096
WIN_LARGE_CONTENT_REASON = "win-large-content"

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _yaml_dq(s: str) -> str:
    """YAML 双引号 flow scalar：转义反斜杠和双引号。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_dq_decode(s: str) -> str:
    """反转 _yaml_dq：先还原 \\" 再还原 \\\\。"""
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _needs_dq(v: str) -> bool:
    return (" " in v) or v.startswith(("[", "{")) or '"' in v or "\\" in v


def parse_frontmatter(md: str) -> tuple[dict, str, tuple[int, int] | None]:
    """返回 (fm_dict, body, span)。span 指 frontmatter 在原文中的字符区间。"""
    m = _FM_RE.match(md)
    if not m:
        return {}, md, None
    raw = m.group(1)
    fm: dict = {}
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [item.strip().strip('"').strip("'") for item in inner.split(",")] if inner else []
        elif val.startswith('"') and val.endswith('"'):
            fm[key] = _yaml_dq_decode(val[1:-1])
        elif val.startswith("'") and val.endswith("'"):
            fm[key] = val[1:-1]
        else:
            fm[key] = val
    return fm, md[m.end():], (m.start(), m.end())


def dump_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            inner = ", ".join(v)
            lines.append(f"{k}: [{inner}]")
        elif isinstance(v, str) and _needs_dq(v):
            lines.append(f"{k}: {_yaml_dq(v)}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


@dataclass
class Degraded:
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1


class ObsidianCLI:
    def __init__(self, vault_path: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.timeout = timeout
        self.degraded = Degraded()
        self._probe_cache: dict | None = None
        self._vault_name: str | None = None  # 多 Vault 时需要指定

    @property
    def degraded_counts(self) -> dict[str, int]:
        return dict(self.degraded.counts)

    def probe(self) -> dict:
        """Obsidian GUI 是否在运行 + obsidian CLI 是否可调用。结果缓存到 skill 运行期。

        - mac/linux：pgrep + zsh -lc 'command -v obsidian'
        - Windows：tasklist + shutil.which('obsidian')
        """
        if self._probe_cache is not None:
            return self._probe_cache

        is_windows = sys.platform == "win32"

        # 1. 检测 Obsidian GUI 进程
        if is_windows:
            if shutil.which("tasklist") is None:
                self._probe_cache = {"ok": False, "reason": "tasklist-missing"}
                return self._probe_cache
            try:
                # 注意：用 subprocess list 形式调用，避免 Git Bash MSYS 路径转换破坏 /FI 参数
                tasklist = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {OBSIDIAN_PROC}.exe", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                self._probe_cache = {"ok": False, "reason": "tasklist-timeout"}
                return self._probe_cache
            # 未找到时 tasklist 返回 0 + stdout 含 "INFO: No tasks..."
            out = (tasklist.stdout or "").strip()
            if tasklist.returncode != 0 or not out or out.startswith("INFO:"):
                self._probe_cache = {"ok": False, "reason": "obsidian-not-running"}
                return self._probe_cache
        else:
            if shutil.which("pgrep") is None:
                self._probe_cache = {"ok": False, "reason": "pgrep-missing"}
                return self._probe_cache
            try:
                pgrep = subprocess.run(
                    ["pgrep", "-x", OBSIDIAN_PROC],
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                self._probe_cache = {"ok": False, "reason": "pgrep-timeout"}
                return self._probe_cache
            if pgrep.returncode != 0 or not pgrep.stdout.strip():
                self._probe_cache = {"ok": False, "reason": "obsidian-not-running"}
                return self._probe_cache

        # 2. 检测 obsidian CLI 可用性
        if is_windows:
            # Windows 上 obsidian 安装到用户 PATH（如 .COM/.EXE），shutil.which 可直接找到
            if shutil.which("obsidian") is None:
                self._probe_cache = {"ok": False, "reason": "cli-not-registered"}
                return self._probe_cache
        else:
            # 通过 login sh 查 obsidian 命令（sh 在所有 POSIX 系统保证存在，确保 PATH 完整）
            try:
                cv = subprocess.run(
                    ["sh", "-lc", "command -v obsidian"],
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                self._probe_cache = {"ok": False, "reason": "cli-probe-timeout"}
                return self._probe_cache
            if cv.returncode != 0 or not cv.stdout.strip():
                self._probe_cache = {"ok": False, "reason": "cli-not-registered"}
                return self._probe_cache

        self._probe_cache = {"ok": True}
        return self._probe_cache

    def _cli(self, args: list[str]) -> dict:
        """调用 obsidian CLI。返回统一结果结构。

        - mac/linux：通过 sh -lc 调用（继承 login shell PATH，sh 在所有 POSIX 系统保证存在）
        - Windows：subprocess list 直接调 obsidian.COM/EXE，shlex.split 把 POSIX-quoted
          args 反解析为 token list（caller 用 shlex.quote 包装的 path/content 由此解开）
        """
        # 拼接命令字符串：在多 Vault 场景下加 Vault 名前缀
        prefix = f'"{self._vault_name}" ' if self._vault_name else ""

        if sys.platform == "win32":
            obsidian_exe = shutil.which("obsidian")
            if not obsidian_exe:
                return {"ok": False, "reason": "cli-not-found"}
            # caller 传入的 args 含 POSIX shlex.quote 包装；用 shlex.split(posix=True) 反解析
            # 保证跨平台一致性。obsidian.COM 用 subprocess list 直接调，不经过 shell。
            cmd_str = "obsidian " + prefix + " ".join(args)
            try:
                tokens = shlex.split(cmd_str, posix=True)
            except ValueError as e:
                return {"ok": False, "reason": "cli-arg-parse-error", "stderr": str(e)}
            if not tokens:
                return {"ok": False, "reason": "cli-empty-args"}
            tokens[0] = obsidian_exe  # 替换 "obsidian" 为完整路径
            try:
                # Windows 默认 codec 是系统 locale（中文环境 GBK），但 obsidian CLI 输出 UTF-8 JSON
                # 必须显式 encoding="utf-8" + errors="replace" 容错非 utf-8 字节
                cp = subprocess.run(
                    tokens,
                    capture_output=True, text=True, timeout=self.timeout,
                    encoding="utf-8", errors="replace",
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "cli-timeout"}
        else:
            cmd = "obsidian " + prefix + " ".join(args)
            try:
                cp = subprocess.run(
                    ["sh", "-lc", cmd],
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "cli-timeout"}

        if cp.returncode != 0:
            return {
                "ok": False,
                "reason": "cli-nonzero",
                "exit_code": cp.returncode,
                "stderr": (cp.stderr or "").strip()[:200],
            }

        return {"ok": True, "stdout": cp.stdout, "stderr": cp.stderr}

    def _resolve_vault(self) -> dict:
        """确认当前 CLI 默认 Vault 与 self.vault_path 是否一致；不一致时在 vaults 列表中定位名字。

        Obsidian CLI 的 vault/vaults 子命令不支持 format=json：
          `vault info=path`  → 单行纯路径字符串
          `vaults verbose`   → 多行 "<name>\t<path>" TSV
        """
        current = self._cli(["vault", "info=path"])
        if not current["ok"]:
            return current
        active_path = current["stdout"].strip()
        if not active_path:
            return {"ok": False, "reason": "vault-parse-fail"}

        if Path(active_path).resolve() == self.vault_path:
            self._vault_name = None
            return {"ok": True}

        vaults_res = self._cli(["vaults", "verbose"])
        if not vaults_res["ok"]:
            return vaults_res
        for line in vaults_res["stdout"].splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, path = parts[0].strip(), parts[1].strip()
            if path and Path(path).resolve() == self.vault_path:
                self._vault_name = name
                return {"ok": True}

        return {"ok": False, "reason": "vault-not-open-in-obsidian"}

    def _ensure_ready(self) -> dict:
        """探测 + Vault 解析。任一失败返回同一格式，调用方据此走 fallback。"""
        p = self.probe()
        if not p["ok"]:
            return p
        return self._resolve_vault()

    def _shell_quote(self, s: str) -> str:
        # POSIX 单引号安全转义，阻断 $VAR / $(...) / `...` 注入
        return shlex.quote(s)

    def _should_skip_cli_for_large_content(self, content: str) -> bool:
        # Windows 上大 content（≥ 4KB UTF-8 byte）会触发 Obsidian 主进程 forward envelope 崩溃
        # 直接绕过 CLI 走 fallback 文件 I/O，记入 degraded_counts 便于观测
        # 用 byte 数（不是字符数）确保中文笔记也能被正确拦截
        return sys.platform == "win32" and len(content.encode("utf-8")) >= WIN_LARGE_CONTENT_BYTES

    def _fallback(self, reason: str, data: dict | None = None, ok: bool = True) -> dict:
        self.degraded.bump(reason)
        result = {"ok": ok, "used": "fallback", "reason": reason}
        if data is not None:
            result["data"] = data
        return result

    def read_note(self, path: str) -> dict:
        ready = self._ensure_ready()
        if ready["ok"]:
            r = self._cli(["read", f"path={self._shell_quote(path)}"])
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {"content": r["stdout"]}}
            return self._fallback_read(path, reason=r.get("reason", "cli-error"))
        return self._fallback_read(path, reason=ready["reason"])

    def _fallback_read(self, path: str, reason: str) -> dict:
        target = self.vault_path / path
        if not target.exists():
            self.degraded.bump(reason)
            return {"ok": False, "used": "fallback", "reason": "file-not-found"}
        content = target.read_text(encoding="utf-8")
        return self._fallback(reason, data={"content": content})

    def create_note(self, path: str, content: str) -> dict:
        if self._should_skip_cli_for_large_content(content):
            return self._fallback_create(path, content, reason=WIN_LARGE_CONTENT_REASON)
        ready = self._ensure_ready()
        if ready["ok"]:
            args = [
                "create",
                f"path={self._shell_quote(path)}",
                f"content={self._shell_quote(content)}",
            ]
            r = self._cli(args)
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {"path": path}}
            return self._fallback_create(path, content, reason=r.get("reason", "cli-error"))
        return self._fallback_create(path, content, reason=ready["reason"])

    def _fallback_create(self, path: str, content: str, reason: str) -> dict:
        target = self.vault_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self._fallback(reason, data={"path": path})

    def append_note(self, path: str, content: str) -> dict:
        if self._should_skip_cli_for_large_content(content):
            return self._fallback_append(path, content, reason=WIN_LARGE_CONTENT_REASON)
        ready = self._ensure_ready()
        if ready["ok"]:
            args = [
                "append",
                f"path={self._shell_quote(path)}",
                f"content={self._shell_quote(content)}",
            ]
            r = self._cli(args)
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {"path": path}}
            return self._fallback_append(path, content, reason=r.get("reason", "cli-error"))
        return self._fallback_append(path, content, reason=ready["reason"])

    def _fallback_append(self, path: str, content: str, reason: str) -> dict:
        target = self.vault_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
        return self._fallback(reason, data={"path": path})

    def properties(self, path: str) -> dict:
        ready = self._ensure_ready()
        if ready["ok"]:
            r = self._cli(["properties", f"path={self._shell_quote(path)}", "format=json"])
            if r["ok"]:
                try:
                    data = json.loads(r["stdout"])
                except json.JSONDecodeError:
                    return self._fallback_properties(path, reason="parse-fail")
                return {"ok": True, "used": "cli", "data": data}
            return self._fallback_properties(path, reason=r.get("reason", "cli-error"))
        return self._fallback_properties(path, reason=ready["reason"])

    def _fallback_properties(self, path: str, reason: str) -> dict:
        target = self.vault_path / path
        if not target.exists():
            self.degraded.bump(reason)
            return {"ok": False, "used": "fallback", "reason": "file-not-found"}
        fm, _, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        return self._fallback(reason, data=fm)

    def property_read(self, path: str, name: str) -> dict:
        ready = self._ensure_ready()
        if ready["ok"]:
            r = self._cli([
                "property:read",
                f"path={self._shell_quote(path)}",
                f"name={self._shell_quote(name)}",
            ])
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {"value": r["stdout"].strip()}}
            return self._fallback_property_read(path, name, reason=r.get("reason", "cli-error"))
        return self._fallback_property_read(path, name, reason=ready["reason"])

    def _fallback_property_read(self, path: str, name: str, reason: str) -> dict:
        pr = self._fallback_properties(path, reason=reason)
        if not pr["ok"]:
            return pr
        return {"ok": True, "used": "fallback", "reason": reason, "data": {"value": pr["data"].get(name)}}

    def property_set(self, path: str, name: str, value: str) -> dict:
        if self._should_skip_cli_for_large_content(value):
            return self._fallback_property_set(path, name, value, reason=WIN_LARGE_CONTENT_REASON)
        ready = self._ensure_ready()
        if ready["ok"]:
            r = self._cli([
                "property:set",
                f"path={self._shell_quote(path)}",
                f"name={self._shell_quote(name)}",
                f"value={self._shell_quote(value)}",
            ])
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {"path": path, "name": name}}
            return self._fallback_property_set(path, name, value, reason=r.get("reason", "cli-error"))
        return self._fallback_property_set(path, name, value, reason=ready["reason"])

    def _fallback_property_set(self, path: str, name: str, value: str, reason: str) -> dict:
        target = self.vault_path / path
        if not target.exists():
            self.degraded.bump(reason)
            return {"ok": False, "used": "fallback", "reason": "file-not-found"}
        md = target.read_text(encoding="utf-8")
        fm, body, span = parse_frontmatter(md)
        if span is None:
            # 无 frontmatter 块：在文件头插入新块
            new_md = dump_frontmatter({name: value}) + md
        else:
            fm_raw = md[span[0]:span[1]]
            if name in fm:
                # 行级替换：保留原字段的引号风格；特殊字符值一律按 YAML 双引号转义
                pattern = re.compile(rf'^(\s*{re.escape(name)}\s*:\s*)(.*)$', re.MULTILINE)
                def _repl(m: re.Match) -> str:
                    prefix = m.group(1)
                    old_val = m.group(2).strip()
                    quoted = (old_val.startswith('"') and old_val.endswith('"')) or \
                             (old_val.startswith("'") and old_val.endswith("'"))
                    if quoted or _needs_dq(value):
                        return f"{prefix}{_yaml_dq(value)}"
                    return f"{prefix}{value}"
                new_fm_raw = pattern.sub(_repl, fm_raw, count=1)
            else:
                # 新字段：在末尾 `---` 前追加一行
                end_marker = re.compile(r'\n---\s*\n?\Z')
                m = end_marker.search(fm_raw)
                new_line = f"\n{name}: {_yaml_dq(value)}" if _needs_dq(value) else f"\n{name}: {value}"
                if m:
                    new_fm_raw = fm_raw[:m.start()] + new_line + fm_raw[m.start():]
                else:
                    fm[name] = value
                    new_fm_raw = dump_frontmatter(fm)
            new_md = md[:span[0]] + new_fm_raw + md[span[1]:]
        target.write_text(new_md, encoding="utf-8")
        return self._fallback(reason, data={"path": path, "name": name})


    def search(self, query: str, folder: str | None = None, limit: int | None = None) -> dict:
        ready = self._ensure_ready()
        if ready["ok"]:
            args = ["search", f"query={self._shell_quote(query)}", "format=json"]
            if folder:
                args.append(f"path={self._shell_quote(folder)}")
            if limit:
                args.append(f"limit={limit}")
            r = self._cli(args)
            if r["ok"]:
                stdout = (r["stdout"] or "").strip()
                # 边界：obsidian CLI search 在无匹配时返回纯文本 "No matches found."
                # 而非空 JSON。直接走 json.loads 会 JSONDecodeError 导致 parse-fail 误降级。
                if stdout.startswith("No matches found"):
                    return {"ok": True, "used": "cli", "data": {"paths": []}}
                try:
                    data = json.loads(stdout)
                except json.JSONDecodeError:
                    return self._fallback_search(query, folder, reason="parse-fail")
                return {"ok": True, "used": "cli", "data": {"paths": data}}
            return self._fallback_search(query, folder, reason=r.get("reason", "cli-error"))
        return self._fallback_search(query, folder, reason=ready["reason"])

    def _fallback_search(self, query: str, folder: str | None, reason: str) -> dict:
        root = self.vault_path / folder if folder else self.vault_path
        hits: list[str] = []
        for p in root.rglob("*.md"):
            try:
                if query in p.read_text(encoding="utf-8"):
                    hits.append(str(p.relative_to(self.vault_path)))
            except OSError:
                continue
        return self._fallback(reason, data={"paths": sorted(hits)})

    def files(self, ext: str | None = None, folder: str | None = None) -> dict:
        """列 Vault 内文件。

        Obsidian CLI 的 `files` 子命令不支持 format=json，默认按行输出相对路径。
        """
        ready = self._ensure_ready()
        if ready["ok"]:
            args = ["files"]
            if ext:
                args.append(f"ext={ext}")
            if folder:
                args.append(f"folder={self._shell_quote(folder)}")
            r = self._cli(args)
            if r["ok"]:
                paths = [ln.strip() for ln in r["stdout"].splitlines() if ln.strip()]
                return {"ok": True, "used": "cli", "data": {"paths": paths}}
            return self._fallback_files(ext, folder, reason=r.get("reason", "cli-error"))
        return self._fallback_files(ext, folder, reason=ready["reason"])

    def _fallback_files(self, ext: str | None, folder: str | None, reason: str) -> dict:
        root = self.vault_path / folder if folder else self.vault_path
        suffix = f".{ext}" if ext else None
        results: list[str] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if suffix and p.suffix != suffix:
                continue
            results.append(str(p.relative_to(self.vault_path)))
        return self._fallback(reason, data={"paths": sorted(results)})

    def reload_vault(self) -> dict:
        ready = self._ensure_ready()
        if ready["ok"]:
            r = self._cli(["reload"])
            if r["ok"]:
                return {"ok": True, "used": "cli", "data": {}}
            return self._fallback(r.get("reason", "cli-error"), data={"noop": True})
        return self._fallback(ready["reason"], data={"noop": True})


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Obsidian CLI 封装层")
    p.add_argument("--vault", required=True)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sub = p.add_subparsers(dest="op", required=True)

    sub.add_parser("probe")

    sp = sub.add_parser("read")
    sp.add_argument("--path", required=True)

    sp = sub.add_parser("create")
    sp.add_argument("--path", required=True)
    sp.add_argument("--content", required=True)

    sp = sub.add_parser("append")
    sp.add_argument("--path", required=True)
    sp.add_argument("--content", required=True)

    sp = sub.add_parser("properties")
    sp.add_argument("--path", required=True)

    sp = sub.add_parser("property-read")
    sp.add_argument("--path", required=True)
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("property-set")
    sp.add_argument("--path", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--value", required=True)

    sp = sub.add_parser("search")
    sp.add_argument("--query", required=True)
    sp.add_argument("--folder")
    sp.add_argument("--limit", type=int)

    sp = sub.add_parser("files")
    sp.add_argument("--ext")
    sp.add_argument("--folder")

    sub.add_parser("reload")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    cli = ObsidianCLI(args.vault, timeout=args.timeout)

    dispatch = {
        "probe": lambda: cli.probe(),
        "read": lambda: cli.read_note(args.path),
        "create": lambda: cli.create_note(args.path, args.content),
        "append": lambda: cli.append_note(args.path, args.content),
        "properties": lambda: cli.properties(args.path),
        "property-read": lambda: cli.property_read(args.path, args.name),
        "property-set": lambda: cli.property_set(args.path, args.name, args.value),
        "search": lambda: cli.search(args.query, folder=args.folder, limit=args.limit),
        "files": lambda: cli.files(ext=args.ext, folder=args.folder),
        "reload": lambda: cli.reload_vault(),
    }

    result = dispatch[args.op]()
    result["degraded_counts"] = cli.degraded_counts
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
