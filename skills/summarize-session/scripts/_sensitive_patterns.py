"""敏感文件 deny-list：路径模式 + 内容启发式 + 白名单豁免。

被 archive_doc.py 在归集前调用，命中即拒绝归集（标 denied_sensitive=true）。
"""
import os
import re
import fnmatch

# 路径 glob 模式（命中即敏感）
_SENSITIVE_PATH_GLOBS = [
    "**/.env",
    "**/.env.*",
    "**/credentials*",
    "**/credential.*",
    "**/*secret*",
    "**/*token*",
    "**/CLAUDE.local.md",
    "**/.claude/settings*.json",
    "**/.claude/projects/*/memory/**",
    "**/.claude/jobs/**",
]

# 白名单：文件名包含以下关键词的 markdown 文件豁免（spec/plan 类文档）
_DOC_WHITELIST_KEYWORDS = ('design', 'plan', 'spec', 'doc', 'note')

# 白名单：路径中含以下目录段的 markdown 文件豁免（spec/plan/doc 目录归集）
_DOC_WHITELIST_PATH_SEGMENTS = ('/specs/', '/plans/', '/designs/', '/docs/', '/notes/')

# 硬 deny：文件名精确匹配以下模式时，必须凌驾于白名单之上直接判敏感
# （文件名层比对，已 lower 化）
_HARD_DENY_FILENAMES = ('claude.local.md',)
_HARD_DENY_FILENAME_GLOBS = ('.env', '.env.*', 'credentials', 'credentials.*')

# 内容启发式正则
_PRIVATE_KEY_RE = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
# 词边界：前置 (?<![A-Za-z]) 防 prose 子串污染（如 'mysecret = ...'）
_API_KEY_RE = re.compile(
    r'(?i)(?<![A-Za-z])(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{20,}'
)


def _matches_any_glob(path: str, globs) -> bool:
    """fnmatch 不支持 ** 跨目录，自己用 substring + suffix 兼容。"""
    norm = path.replace('\\', '/').lower()
    for pat in globs:
        pat_norm = pat.replace('\\', '/').lower()
        if pat_norm.startswith('**/'):
            tail = pat_norm[3:]
            if '**/' in tail:
                a, b = tail.split('**/', 1)
                if a in norm and b in norm and norm.find(a) <= norm.find(b):
                    return True
            else:
                if (fnmatch.fnmatch(norm, '*/' + tail)
                        or norm.endswith('/' + tail)
                        or fnmatch.fnmatch(os.path.basename(norm), tail)):
                    return True
        else:
            if fnmatch.fnmatch(norm, pat_norm):
                return True
    return False


def is_sensitive_path(path: str) -> bool:
    """判定路径是否命中敏感 deny-list。命中且非 doc 白名单 → True。

    优先级：硬 deny（CLAUDE.local.md / .env / credentials.*）凌驾于白名单之上，
    防止 /docs/ /notes/ /plans/ 等白名单路径段静默放行高敏文件。
    """
    if not path:
        return False
    norm = path.replace('\\', '/').lower()
    fname = os.path.basename(norm)
    # 硬 deny 短路：文件名命中 → 直接判敏感，不进白名单分支
    if fname in _HARD_DENY_FILENAMES:
        return True
    for pat in _HARD_DENY_FILENAME_GLOBS:
        if fnmatch.fnmatch(fname, pat):
            return True
    # markdown 文件且含 doc 白名单关键词 → 豁免
    if fname.endswith('.md'):
        for kw in _DOC_WHITELIST_KEYWORDS:
            if kw in fname:
                return False
        # 路径含 doc 类目录段 → 豁免（覆盖 /specs/ /plans/ 等存放位置）
        for seg in _DOC_WHITELIST_PATH_SEGMENTS:
            if seg in norm:
                return False
    return _matches_any_glob(path, _SENSITIVE_PATH_GLOBS)


def is_sensitive_content(text: str) -> bool:
    """判定文件内容是否含敏感启发式。"""
    if not text:
        return False
    if _PRIVATE_KEY_RE.search(text):
        return True
    if _API_KEY_RE.search(text):
        return True
    return False
