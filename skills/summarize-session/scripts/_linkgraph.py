"""链接图分析：剥 frontmatter + mask 代码块/行内代码后提取正文真实 wikilink，
计算已归集 spec/plan 的 backlink 缺失 + unresolved 悬空链接。"""
import os
import re
import sys
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from obsidian_cli import parse_frontmatter


def _strip_fenced_code(text):
    """按行栈式扫描剥离 fenced code block（支持 3/4+ 反引号嵌套）。

    fence 栈记录每层打开的反引号数量；关闭 fence 必须与栈顶相同长度才 pop。
    这样 ```` 外层不会被内层 ``` 误关。支持 ~~~ tilde fence（M1）。
    """
    fence_re = re.compile(r'^[ \t]*((`{3,})|(~{3,}))')
    lines = text.splitlines(keepends=True)
    out = []
    stack = []
    for line in lines:
        m = fence_re.match(line)
        if m:
            key = (m.group(1)[0], len(m.group(1)))
            if stack and stack[-1] == key:
                stack.pop()
                continue
            else:
                stack.append(key)
                continue
        if not stack:
            out.append(line)
    return ''.join(out)

_VAULT_SKIP_DIRS = {'.git', '.obsidian', '.meta', '.trash'}
# wikilink，归一化锚点 #/别名 |（沿用 verify_migration:97）
_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]')
# inline code：相同数量反引号成对（处理双/三反引号，finding R4），不跨行
_INLINE_CODE_RE = re.compile(r'(`+)[^\n]{0,500}?\1')  # M3：限长防 ReDoS


def extract_wikilinks(md):
    """剥 frontmatter → mask fenced → mask inline code → 提取正文 wikilink（归一化子目录/锚点/别名）。"""
    _, body, _ = parse_frontmatter(md)
    body = _strip_fenced_code(body)
    body = _INLINE_CODE_RE.sub('', body)
    out = []
    for m in _WIKILINK_RE.finditer(body):
        t = m.group(1).strip()
        if t:
            out.append(t.split('/')[-1])  # 子目录前缀归一化取 basename（finding R8）
    return out


def _scan_notes(vault_root):
    """返回 [(rel, abspath)]，跳过 .git/.obsidian/.meta/.trash。"""
    root = pathlib.Path(vault_root)
    out = []
    for dirpath, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in _VAULT_SKIP_DIRS]
        for f in files:
            if f.endswith('.md'):
                ab = pathlib.Path(dirpath) / f
                out.append((str(ab.relative_to(root)).replace(os.sep, '/'), str(ab)))
    return out


def analyze(vault_root):
    """返回 {unresolved_links: [{src, target}], specplan_no_backlink: [{stem, rel}]}。
    - unresolved：正文 wikilink target（按 stem）在 Vault 内无同名 .md
    - specplan_no_backlink：specs/plans 目录下的笔记，无任何其他笔记的正文 [[wikilink]] 指向它"""
    notes = _scan_notes(vault_root)
    stems = {pathlib.Path(rel).stem for rel, _ in notes}
    inbound = {s: 0 for s in stems}
    unresolved = []
    for rel, ab in notes:
        try:
            md = pathlib.Path(ab).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for target in extract_wikilinks(md):
            if target in inbound:
                inbound[target] += 1
            else:
                unresolved.append({'src': rel, 'target': target})
    specplan_no_backlink = []
    for rel, _ in notes:
        parts = pathlib.Path(rel).parts
        if 'specs' in parts or 'plans' in parts:
            stem = pathlib.Path(rel).stem
            if inbound.get(stem, 0) == 0:
                specplan_no_backlink.append({'stem': stem, 'rel': rel})
    return {
        'unresolved_links': unresolved,
        'specplan_no_backlink': specplan_no_backlink,
    }
