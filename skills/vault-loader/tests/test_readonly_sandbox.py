"""宿主只读沙箱不得被当成 hook 执行失败——以及这条守卫必须钉得住**接线**。

本文件 2026-09-03 首版只做了一件事：直接调 `_report_nonfatal` 本体，断言
PermissionError 不打 stderr。它钉住了「定义」，钉不住「它被接上了」——把 7 个真实
调用点**全部**还原成 `print(..., file=sys.stderr)`，vault-loader 整套仍然全绿
（2026-09-07 变异实证，两个阳性对照 KILLED，故非工具失效）。
"""
from __future__ import annotations

import ast
import errno
from pathlib import Path

import pytest

from scripts import prompt_submit_load, session_start_load

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PS = SCRIPTS / "prompt_submit_load.py"
SS = SCRIPTS / "session_start_load.py"

# 每个入口脚本里**承重**的写失败上报点。这些是宿主只读时会大量触发、
# 因而必须走降级通道的那些；未列入的（模块 import 失败、崩溃兜底）刻意保持裸 print。
PS_ROUTED = {"state 写入失败", "事件去重失败，继续执行",
             "metrics 写入失败", "metrics 构造失败",
             # 该 except 包裹的 nudge_due / bump_near_miss_counts / mark_nudged
             # 全都写 metrics_dir（还会 mkdir + chmod），是明确的写盘点。
             "near-miss 提示失败"}
# 刻意**不**路由、保持裸 print 的那些，连同理由（避免下次有人「顺手补齐」）：
#   模块加载失败 / 诊断模块不可用 / metrics 模块不可用 —— import 期故障，此时
#     诊断层本身可能就没加载成功，走 stderr 是唯一还可靠的通道；
#   诊断渲染失败 / gate 记录构造失败 / runtime 识别失败 —— 纯计算，不碰文件系统；
#   崩溃兜底 —— 顶层 except，必须无条件可见。
SS_ROUTED = {"state 写入失败", "事件去重失败，继续执行",
             "SessionStart metrics 落盘失败"}


def _literal(node: ast.AST) -> str:
    """取字符串字面量；f-string 取其常量片段拼接。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _call_sites(path: Path) -> tuple[set[str], set[str]]:
    """(经 _report_nonfatal 上报的 message, 直接 print 到 stderr 的 message)。

    走 AST 而非子串/正则：注释里写了 `_report_nonfatal` 不算数，被注释掉的调用
    也不算数——CLAUDE.md 记过「源码级断言会被注释满足」这个坑。
    """
    routed: set[str] = set()
    printed: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "_report_nonfatal":
            routed.add(_literal(node.args[0]))
        elif name == "print" and any(k.arg == "file" for k in node.keywords):
            text = _literal(node.args[0])
            # 裸 print 的字面量形如 "[vault-loader] state 写入失败：{exc}"，
            # 取中间那段与 _report_nonfatal 的 message 对齐。
            for msg in PS_ROUTED | SS_ROUTED:
                if msg in text:
                    printed.add(msg)
    return routed, printed


@pytest.mark.parametrize("path,expected", [(PS, PS_ROUTED), (SS, SS_ROUTED)])
def test_every_load_bearing_write_failure_is_routed(path: Path, expected: set[str]):
    """接线断言：承重的写失败上报点必须**经过** `_report_nonfatal`，不得直接 print。

    把任一调用点改回 `print(..., file=sys.stderr)`，本用例即转红——这正是首版
    守卫做不到的那一半。
    """
    routed, printed = _call_sites(path)
    missing = expected - routed
    assert not missing, f"{path.name}: 这些上报点绕开了 _report_nonfatal：{sorted(missing)}"
    leaked = expected & printed
    assert not leaked, f"{path.name}: 这些上报点仍在直接写 stderr：{sorted(leaked)}"


def test_permission_denial_is_quiet_but_other_failures_remain_visible(capsys):
    """函数本体行为（首版即有，保留）：权限拒绝不进 stderr，真实故障仍可见。"""
    for module in (prompt_submit_load, session_start_load):
        module._report_nonfatal("state 写入失败", PermissionError("denied"))
    assert capsys.readouterr().err == ""

    prompt_submit_load._report_nonfatal("state 写入失败", RuntimeError("broken"))
    err = capsys.readouterr().err
    assert "state 写入失败" in err
    assert "broken" in err


@pytest.mark.parametrize("module", [prompt_submit_load, session_start_load])
def test_host_write_denied_becomes_a_diagnosis_not_permanent_silence(module, capsys):
    """M1：宿主拒绝写入不再**永久静默**，而是登记一条 degraded 诊断。

    改动理由：Windows 上「文件被占用」（WinError 5/32，本机 Obsidian 与安全代理
    属常态）抛的同样是 PermissionError，无条件静默会把它与「用户目录权限真的配坏了」
    一并吞掉，而后者需要用户知道。诊断走 `take_user_visible` 的 code+cwd TTL 冷却，
    同一目录一天最多出现一次。
    """
    from scripts import _diagnostics
    _diagnostics.reset()
    module._report_nonfatal("state 写入失败", PermissionError("denied"))
    assert capsys.readouterr().err == ""
    pending = _diagnostics.pending()
    assert [d.code for d in pending] == [_diagnostics.CODE_HOST_WRITE_DENIED]
    assert pending[0].level == _diagnostics.LEVEL_DEGRADED
    # hint 必须让「属于预期降级」的读者一眼判断无需行动，否则就是 _diagnostics
    # 模块 docstring 第 3 条所警告的那种误报。
    assert "无需处理" in pending[0].hint
    _diagnostics.reset()


def test_read_only_filesystem_is_also_host_write_denied():
    """判据不能只认 PermissionError：只读**文件系统**在 POSIX 上是 OSError(EROFS)。

    CPython 不把 EROFS 映射成 PermissionError，故原先三处内联的
    `isinstance(exc, PermissionError)` 会把只读挂载当成真实故障去刷 stderr。
    """
    from context_vault.degrade import is_host_write_denied
    assert is_host_write_denied(OSError(errno.EROFS, "read-only file system")) is True
    assert is_host_write_denied(OSError(errno.EACCES, "denied")) is True
    assert is_host_write_denied(PermissionError("denied")) is True
    # 阴性对照：与写权限无关的 OSError 不得命中，否则真实故障会被一并吞掉
    assert is_host_write_denied(OSError(errno.ENOENT, "missing")) is False
    assert is_host_write_denied(RuntimeError("broken")) is False


def test_metrics_flush_shares_the_single_judgement():
    """第三处内联判据也必须走公共层——它此前独立于两个入口脚本各写了一份。"""
    src = (SCRIPTS / "_metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "context_vault.degrade"
        for alias in node.names
    }
    assert "is_host_write_denied" in imported, "_metrics.py 未使用公共层判据"
    # 且不得再出现自己那份 isinstance(exc, PermissionError)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "isinstance":
            names = {getattr(a, "id", "") for a in node.args}
            assert "PermissionError" not in names, "_metrics.py 仍内联着第三份判据"
