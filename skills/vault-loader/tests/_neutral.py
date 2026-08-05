"""测试用的中性 cwd —— 一个保证**不在**出厂 `opt_out_paths` 内的目录。

## 为什么需要它（H-2，防复发）

出厂 `DEFAULT_CONFIG["opt_out_paths"]` 含 `/tmp` 与 `/private/tmp`。集成用例过去
用 `Path("/tmp")` 当「随便一个工作目录」，恰好撞进这份清单——也就是说这些用例
本该被 opt-out 闸门拦掉、拿不到任何注入。

它们之所以一直是绿的，是因为 `_is_opt_out_path` 当时用裸 `str().startswith()`：
Windows 上 `str(Path("/tmp"))` 得到 `\tmp`，与前缀 `/tmp` 比对不上，闸门从未生效。
**用例在依赖一个 bug 才能通过**。POSIX 上没有这个分隔符差异，同一批用例本该早就红
（评审阶段复刻 POSIX 语义实跑确实是 22 failed）。

BP-1 修好归一化后，Windows 也复现了正确行为，于是必须换掉这个 cwd。

## 选值

刻意选一个**不存在**的路径：hook 只把 cwd 当字符串信号用（项目目录匹配、git 根查找），
不要求它存在；原来的 `/tmp` 在 Windows 上同样不存在（会归一成 `D:\tmp`）。
不用 `tmp_path` 是因为它随 fixture 变化，而多数用例只需要一个稳定的中性值。

要断言 opt-out **确实生效**的用例，请显式用 `/tmp` 之类在清单内的路径，
不要用本常量——见 `test_opt_out_path_prefix` 与 `tests/test_opt_out_paths.py`。
"""
from __future__ import annotations

from pathlib import Path

NEUTRAL_CWD = Path("/workspace/neutral-project")
