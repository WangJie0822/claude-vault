"""一键清理 unresolved 悬空 wikilink：正文（非 frontmatter、非代码块/行内代码区）的
unresolved [[target]] 改反引号 `target`。默认 dry-run，--apply 写入 + .bak。
区间标记不删除（原位）+ fail-closed（mask 异常整文件跳过）+ 永不碰 frontmatter。"""
import re
import sys
import json
import shutil
import argparse
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from obsidian_cli import parse_frontmatter
import _linkgraph

_INLINE_CODE_RE = re.compile(r'(`+)[^\n]{0,500}?\1')  # M3：限长防 ReDoS（真实行内代码远短于 500）
_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]')
_FENCE_LINE_RE = re.compile(r'^[ \t]*((`{3,})|(~{3,}))')  # M1：支持 ~~~ tilde fence


def _masked_spans(body):
    """返回不可改写区间 [(start,end)]：fenced code（栈式）+ inline code。
    未闭合 fence → 抛 ValueError，调用方 fail-closed。"""
    spans = []
    pos, stack, fence_start = 0, [], None
    for line in body.splitlines(keepends=True):
        m = _FENCE_LINE_RE.match(line)
        if m:
            marker = m.group(1)
            key = (marker[0], len(marker))  # (符号, 长度)：~~~ 不能关 ``` fence
            if stack and stack[-1] == key:
                stack.pop()
                if not stack:
                    spans.append((fence_start, pos + len(line)))
            else:
                if not stack:
                    fence_start = pos
                stack.append(key)
        pos += len(line)
    if stack:  # 未闭合 fence → 视为异常，fail-closed
        raise ValueError('unclosed fence')
    for m in _INLINE_CODE_RE.finditer(body):
        spans.append((m.start(), m.end()))
    return spans


def _in_spans(idx, spans):
    return any(s <= idx < e for s, e in spans)


def fix_text(md, unresolved_stems):
    """对正文（frontmatter 之后）非代码区的 unresolved [[t]] 改 `t`。返回 (new_md, n_changed)。
    fail-closed：mask 失败抛异常由调用方捕获跳过该文件。frontmatter 区原样保留。"""
    _, _body, span = parse_frontmatter(md)
    fm_end = span[1] if span else 0
    head, work = md[:fm_end], md[fm_end:]
    spans = _masked_spans(work)
    changed = [0]

    def repl(m):
        if _in_spans(m.start(), spans):
            return m.group(0)
        stem = m.group(1).split('/')[-1].strip()
        if stem in unresolved_stems:
            changed[0] += 1
            return '`' + m.group(1).strip() + '`'
        return m.group(0)

    new_work = _WIKILINK_RE.sub(repl, work)
    return head + new_work, changed[0]


def run(vault_root, apply=False):
    lg = _linkgraph.analyze(vault_root)
    unresolved_stems = {u['target'] for u in lg['unresolved_links']}
    by_file = {}
    for u in lg['unresolved_links']:
        by_file.setdefault(u['src'], True)
    total_files, total_changed, skipped = 0, 0, []
    for rel in by_file:
        ab = pathlib.Path(vault_root) / rel
        try:
            md = ab.read_text(encoding='utf-8')
            new_md, n = fix_text(md, unresolved_stems)
        except Exception as e:
            skipped.append({'rel': rel, 'reason': str(e)})  # fail-closed
            continue
        if n == 0:
            continue
        total_files += 1
        total_changed += n
        if apply:
            shutil.copy2(str(ab), str(ab) + '.bak')
            ab.write_text(new_md, encoding='utf-8')
    return {'status': 'applied' if apply else 'dry-run',
            'files': total_files, 'changed': total_changed, 'skipped': skipped}


def main():
    ap = argparse.ArgumentParser(description='清理 unresolved 悬空 wikilink → 反引号')
    ap.add_argument('--vault', required=True)
    ap.add_argument('--apply', action='store_true', help='实际写入；默认 dry-run')
    a = ap.parse_args()
    print(json.dumps(run(a.vault, apply=a.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
