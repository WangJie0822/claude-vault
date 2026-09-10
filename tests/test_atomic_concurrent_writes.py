"""`update_json` 在真实多进程并发下不得丢写。

2026-09-10：`lease_lock` 的重试循环此前只捕 `FileExistsError`，而 Windows 上锁文件
处于 delete-pending 时 `os.open(O_CREAT|O_EXCL)` 抛的是 `PermissionError`
（winerror=5），它逃出循环后被上层 fail-open 吞掉 —— 静默丢写。

**判据是「最终落盘的 key 个数」而不是「有没有抛异常」**：丢写的整条链路都在 fail-open
里，调用方看到的是成功，只有数数据才看得出来。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_vault.atomic as A  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("osname,should_retry", [("nt", True), ("posix", False)])
def test_permission_error_retry_is_platform_scoped(tmp_path: Path, monkeypatch,
                                                   osname: str,
                                                   should_retry: bool) -> None:
    """注入 `PermissionError` 直接验判据，不依赖调度运气。

    下面那条真实并发用例是端到端佐证，但它的判别力是**概率性**的：退回只捕
    `FileExistsError` 的变异实测 3 次里只红 2 次。这条把同一契约变成确定性断言。

    平台分界是实现的一部分，两边都要钉：Windows 上 `PermissionError` 可能是
    delete-pending（必须重试），POSIX 上没有该语义、它就是真权限问题（必须原样抛出，
    在那里重试只会白等到 timeout 再换个异常类型失败）。
    """
    monkeypatch.setattr(os, "name", osname)
    target = tmp_path / "x.json"
    real_open = os.open
    calls = {"n": 0}

    def flaky_open(path, flags, mode=0o777, **kw):
        # 只对锁文件的首次创建注入，其余一律透传（pytest 自身也在用 os.open）
        if str(path).endswith(".lock") and calls["n"] < 1:
            calls["n"] += 1
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", flaky_open)

    if should_retry:
        A.update_json(target, lambda cur: {**cur, "k": 1})
        assert json.loads(target.read_text(encoding="utf-8"))["k"] == 1
        assert calls["n"] == 1, "注入没生效，本用例未验证到任何东西"
    else:
        with pytest.raises(PermissionError):
            A.update_json(target, lambda cur: {**cur, "k": 1})

# 子进程脚本：每个进程往同一个文件写 WRITES 个自己的 key
_WORKER = '''
import json, sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from context_vault.atomic import update_json

target, key, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
ok = 0
for i in range(n):
    try:
        update_json(target, lambda cur, k=key, idx=i: {{**cur, f"{{k}}-{{idx}}": idx}})
        ok += 1
    except Exception as exc:
        print("ERR", type(exc).__name__, file=sys.stderr)
print(json.dumps({{"ok": ok}}))
'''

# ⚠️ 规模决定判别力，不是可以随手调小的常量。丢写是概率性的：6×25=150 次写时
# 变异（退回只捕 FileExistsError）实测 3 次里只红 1 次；8×60=480 次时才稳定转红
# （PoC 实测该规模下丢 5 条 ≈1%）。调小它等于让这条守卫变成掷骰子。
PROCS = 8
WRITES = 60


def test_concurrent_update_json_loses_nothing(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER.format(root=str(ROOT)), encoding="utf-8")
    target = tmp_path / "shared.json"

    procs = [
        subprocess.Popen(
            [sys.executable, "-B", str(worker), str(target), f"w{i}", str(WRITES)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        for i in range(PROCS)
    ]
    errors = []
    for p in procs:
        _out, err = p.communicate(timeout=300)
        if err.strip():
            errors.extend(err.strip().splitlines())

    assert target.exists(), "并发写之后目标文件不存在"
    data = json.loads(target.read_text(encoding="utf-8"))
    expected = PROCS * WRITES
    lost = expected - len(data)

    # 判据不是「零丢失」而是「**每一条丢失都有已知异常可解释**」。两者的区别很实在：
    # `lease_lock` 的 2s 租约在 8 路极端争用下偶尔不够，会抛 TimeoutError —— 那是
    # 一个独立的、调用方可见的失败（不是静默丢写），本次不处理。而 PermissionError
    # 逃出重试循环属于**静默**丢写：调用方看到成功、数据却没了，那才是本用例要钉的。
    # 写成「零丢失」会让这条用例被 TimeoutError 拖成 flaky，很快就没人当真了。
    perm = [e for e in errors if "PermissionError" in e]
    assert not perm, (
        f"PermissionError 逃出 lease_lock 的重试循环导致静默丢写（{len(perm)} 次）。"
        "Windows 上锁文件 delete-pending 时 os.open(O_CREAT|O_EXCL) 抛的是它而非 "
        "FileExistsError，重试循环必须一并捕获")
    assert lost <= len(errors), (
        f"丢了 {lost} 条但只有 {len(errors)} 条子进程异常 —— 存在**无法解释**的丢写，"
        f"即调用方以为写成功了、数据却不在。异常样本：{errors[:5]}")
