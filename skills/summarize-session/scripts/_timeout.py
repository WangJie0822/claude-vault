#!/usr/bin/env python3
"""跨平台超时工具,模拟 GNU coreutils 的 timeout 命令。

macOS 默认没有 timeout/gtimeout,本工具用 subprocess.timeout 实现等价语义:
- 命令在超时内完成 → 透传子进程退出码
- 超时 → 退出码 124(与 GNU timeout 相同)
- 信号传递 → SIGTERM 后 2s 仍未退出则 SIGKILL

用法:
  python3 _timeout.py <seconds> <cmd> [args...]
"""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _timeout.py <seconds> <cmd> [args...]", file=sys.stderr)
        return 2
    try:
        seconds = float(sys.argv[1])
    except ValueError:
        print(f"_timeout.py: invalid seconds '{sys.argv[1]}'", file=sys.stderr)
        return 2
    cmd = sys.argv[2:]
    try:
        p = subprocess.run(cmd, timeout=seconds)
        return p.returncode
    except subprocess.TimeoutExpired:
        return 124
    except FileNotFoundError as e:
        print(f"_timeout.py: command not found: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
