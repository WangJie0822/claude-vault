"""判定「宿主拒绝写入」这类**非本插件故障**的降级条件。

单点存在的理由：该判据此前在三处各写了一份 —— `prompt_submit_load._report_nonfatal`、
`session_start_load._report_nonfatal`（两份逐字相同的副本）与 `_metrics.flush()` 里的
内联 `isinstance(exc, PermissionError)`。三份意味着改一处漏两处，而其中任何一处的
漏改都只表现为「少了一条本该出现的诊断」或「多了一行本不该出现的 stderr」，
两种都不会让任何测试转红。

刻意只提供**判据**、不提供报告出口：报告要走 vault-loader 的诊断缓冲（带 code+cwd
的 TTL 冷却），而那是上层设施，公共层不能反向依赖它。
"""
from __future__ import annotations

import errno

# EACCES/EPERM：沙箱或目录 ACL 拒绝；EROFS：只读文件系统。
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def is_host_write_denied(exc: BaseException) -> bool:
    """宿主环境拒绝写入时返回 True（沙箱只读、目录 ACL、只读挂载）。

    **不能只判 `isinstance(exc, PermissionError)`**：只读*文件系统*在 POSIX 上抛的是
    `OSError(EROFS)`，CPython 不把它映射成 `PermissionError`，于是挂载为只读的 vault
    会走到「真实故障」分支去刷 stderr。

    ⚠️ 命中本判据**不等于「无害」**，调用方不得据此永久静默：Windows 上「文件被
    占用」（WinError 5 / 32）同样抛 `PermissionError` —— 本机 Obsidian 与安全代理会
    常态触发，`skills/summarize-session/scripts/_fs.py` 正为此做指数退避重试。
    永久静默会把它与「用户目录权限真的配坏了」一并吞掉，而后者是需要用户知道的。
    正确处置是「降级并说一次」（交给带冷却的诊断通道），而不是完全不吭声。
    """
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and exc.errno in _DENIED_ERRNOS
