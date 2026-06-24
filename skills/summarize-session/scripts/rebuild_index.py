#!/usr/bin/env python3
"""
重建 Vault CLAUDE.md 索引。

基于 frontmatter 缓存增量更新索引表格，自动处理 mtime 比对和缓存刷新。
用法: python3 rebuild_index.py --vault /path/to/vault [--cache /path/to/cache.json]
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# 文件锁实现已提取到 _fs.py 公共模块（沿用与 _frontmatter/_fs 一致的同目录 import 风格）
from _fs import _acquire_lock, _release_lock, LOCK_TIMEOUT, atomic_write_text, atomic_write_json  # noqa: F401


def parse_frontmatter(text):
    """从 markdown 文本中解析 YAML frontmatter。

    优先使用 PyYAML 完整解析；未安装时降级到仅支持标量和内联数组的手写解析器。
    """
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        return {}

    fm_lines = []
    for line in lines[1:]:
        if line.strip() == '---':
            break
        fm_lines.append(line)
    else:
        return {}  # 没找到结束标记

    # 优先用 PyYAML(支持块标量、多行数组、嵌套等完整 YAML)
    if _HAS_YAML:
        try:
            data = yaml.safe_load('\n'.join(fm_lines))
            if isinstance(data, dict):
                # 归一化:标量字段统一转为字符串,数组字段保持 list
                result = {}
                for k, v in data.items():
                    if isinstance(v, list):
                        result[k] = [str(i) for i in v]
                    elif v is None:
                        continue
                    else:
                        result[k] = str(v)
                return result
        except yaml.YAMLError:
            pass  # 降级到手写解析器

    # 手写降级解析器:仅处理标量和内联数组
    result = {}
    for line in fm_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        match = re.match(r'^([\w][\w-]*)\s*:\s*(.*)', line)
        if not match:
            continue

        key = match.group(1)
        value = match.group(2).strip()

        if not value:
            continue

        # 内联数组: [item1, item2]
        if value.startswith('[') and value.endswith(']'):
            inner = value[1:-1]
            if inner.strip():
                items = [i.strip().strip('"').strip("'") for i in inner.split(',')]
                result[key] = [i for i in items if i]
            else:
                result[key] = []
        # 带引号的字符串
        elif (value.startswith('"') and value.endswith('"')) or \
             (value.startswith("'") and value.endswith("'")):
            result[key] = value[1:-1]
        else:
            result[key] = value

    return result


def is_system_index(rel):
    """判断相对 vault 的路径 rel 是否是系统生成的索引文件（精确规则，防误伤）。

    1. 根索引：rel == '未分类 索引.md'
    2. category 索引：rel 恰为两段 'top/top 索引.md'（文件名 == 父目录名 + ' 索引.md'）
    3. 过渡兼容（遗留）：rel == 'INDEX.md' 或 rel.endswith('/INDEX.md')
    """
    rel = str(rel).replace('\\', '/')
    if rel == '未分类 索引.md':
        return True
    if rel == 'INDEX.md' or rel.endswith('/INDEX.md'):
        return True
    parts = rel.split('/')
    if len(parts) == 2 and parts[1] == '{} 索引.md'.format(parts[0]):
        return True
    return False


def _infer_path_grouping(rel: str):
    """按文件相对路径推断 (category, subcategory)。

    规则：
      - `<cat>/file.md`         → (cat, '')
      - `<cat>/<sub>/file.md`   → (cat, sub)
      - `<cat>/<sub>/<x>/file.md` → (cat, sub)  # 深层目录仍归到二级
    """
    parts = rel.split('/')
    if len(parts) < 2:
        return ('', '')
    cat = parts[0]
    sub = parts[1] if len(parts) >= 3 else ''
    return (cat, sub)


def _resolve_grouping(entry: dict, rel: str):
    """合并 frontmatter 与路径推断,确定最终 (category, subcategory)。

    优先级:frontmatter > 路径推断。frontmatter 缺失时由路径补全,
    让无 frontmatter 的 plans/specs 类笔记也能正确归组。
    """
    inferred_cat, inferred_sub = _infer_path_grouping(rel)
    category = entry.get('category') or inferred_cat
    subcat = entry.get('subcategory') or inferred_sub
    return category, subcat


def scan_vault(vault, exclude_dirs):
    """扫描 Vault 中的所有 .md 文件，返回 {相对路径: mtime} 映射。"""
    files = {}
    for md_file in vault.rglob('*.md'):
        rel_parts = md_file.relative_to(vault).parts
        if any(part in exclude_dirs for part in rel_parts):
            continue
        # 排除 macOS AppleDouble metadata 文件（._ 前缀），它们是二进制资源叉非真 markdown
        if md_file.name.startswith('._'):
            continue
        # 排除根目录的 CLAUDE.md
        if md_file.name == 'CLAUDE.md' and md_file.parent == vault:
            continue
        rel = str(md_file.relative_to(vault)).replace('\\', '/')
        # 排除系统生成的索引文件（自身不参与索引）
        if is_system_index(rel):
            continue
        try:
            files[rel] = int(md_file.stat().st_mtime)
        except OSError:
            continue

    return files


def load_cache(cache_path):
    """加载 frontmatter 缓存，格式异常时返回空缓存。"""
    if not cache_path.exists():
        return {"_version": 1, "entries": {}}

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('_version') != 1:
            return {"_version": 1, "entries": {}}
        return data
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"_version": 1, "entries": {}}


def _escape_pipe(s):
    """转义 markdown 表格单元格中的 `|`,防止破坏表格结构。"""
    if s is None:
        return ''
    return str(s).replace('|', '\\|')


def _format_row(item):
    name = item['name']
    # 笔记名在 [[...]] wikilink 中,若含 `|` 会被 Obsidian 误解析为别名;
    # 在 markdown 表格层也会破坏列数。直接拒绝,避免生成损坏索引。
    if '|' in str(name):
        raise ValueError(
            "笔记名包含 '|' 字符,会同时破坏 wikilink 与 markdown 表格: {}".format(name))
    return '| [[{}]] | {} | {} | {} |'.format(
        name,
        _escape_pipe(item['summary']),
        _escape_pipe(item['tags']),
        _escape_pipe(item['status']),
    )


def _emit_claude_md(entries, max_log_days=7):
    """瘦身版 CLAUDE.md 索引区:每 category 汇总行 + subcategory 小计。"""
    from collections import defaultdict as _dd
    groups = _dd(lambda: _dd(list))  # category -> subcategory -> items

    for rel, entry in entries.items():
        if entry.get('status') == 'archived':
            continue
        category, subcat = _resolve_grouping(entry, rel)
        name = Path(rel).stem
        sort_key = (entry.get('updated') or entry.get('created')
                    or datetime.fromtimestamp(entry.get('mtime', 0)).strftime('%Y-%m-%d'))
        groups[category][subcat].append({
            'name': name, 'sort_key': sort_key,
        })

    lines = []
    for category in sorted(groups.keys()):
        subs = groups[category]
        total = sum(len(v) for v in subs.values())
        latest = max(
            (item['sort_key'] for items in subs.values() for item in items),
            default='—')

        if category == '工作日志':
            # 工作日志显示最近 N 天
            all_items = sorted(
                (i for items in subs.values() for i in items),
                key=lambda x: x['sort_key'], reverse=True)
            recent = [f'[[{i["name"]}]]' for i in all_items[:max_log_days]]
            lines.append(
                '## {}    最近 {} 天: {} | [[工作日志 索引|历史 →]]'.format(
                    category, len(recent), ', '.join(recent)))
            continue

        # 索引详情链接用 Obsidian wikilink(基名唯一)而非 markdown 路径链接:
        # 带空格的 markdown 路径 [详情 →](Claude Code/Claude Code 索引.md) 在 Obsidian
        # 里裸空格会截断 destination 致点击新建笔记;wikilink 按基名解析必跳转,且进图谱。
        if category == '':
            cat_label, index_name = '未分类', '未分类 索引'
        else:
            cat_label, index_name = category, '{} 索引'.format(category)
        lines.append(
            '## {}    {} 篇 | 最近更新 {} | [[{}|详情 →]]'.format(
                cat_label, total, latest, index_name))

        # 有 subcategory 时输出小计
        has_sub = any(sub for sub in subs.keys() if sub)
        if has_sub:
            for subcat in sorted(subs.keys()):
                if not subcat:
                    continue
                lines.append('  - {}       {} 篇'.format(subcat, len(subs[subcat])))
        lines.append('')

    return '\n'.join(lines)


def _emit_category_index(entries, max_log_days=7):
    """为每个 category 生成 '{category} 索引.md' 文本,按 subcategory 二级分组。"""
    from collections import defaultdict as _dd
    groups = _dd(lambda: _dd(list))

    for rel, entry in entries.items():
        category, subcat = _resolve_grouping(entry, rel)
        name = Path(rel).stem
        summary = entry.get('summary', '(待补全)')
        tags = ', '.join(entry.get('tags', [])) if entry.get('tags') else '—'
        status = entry.get('status', 'active')
        sort_key = (entry.get('updated') or entry.get('created')
                    or datetime.fromtimestamp(entry.get('mtime', 0)).strftime('%Y-%m-%d'))
        groups[category][subcat].append({
            'name': name, 'summary': summary, 'tags': tags,
            'status': status, 'sort_key': sort_key,
        })

    result = {}
    for category, subs in groups.items():
        lines = [f'# {category or "未分类"} 索引', '']

        # 工作日志专用分支:按 name(stem) 提取 (年, 月) 分组,绕开通用 subcategory 逻辑
        if category == '工作日志':
            from collections import defaultdict as _dd2
            all_items = [i for items in subs.values() for i in items]
            ym_groups = _dd2(list)
            for it in all_items:
                stem = it['name']
                if len(stem) >= 7 and stem[4] == '-':
                    ym_groups[(stem[:4], stem[5:7])].append(it)
                else:
                    ym_groups[('未知', '未知')].append(it)
            archived_all = []
            for ym in sorted(ym_groups.keys(), reverse=True):
                items = sorted(ym_groups[ym], key=lambda x: x['sort_key'], reverse=True)
                active = [i for i in items if i['status'] != 'archived']
                archived = [i for i in items if i['status'] == 'archived']
                archived_all.extend(archived)
                lines.append(f'## {ym[0]}年 / {ym[1]}月')
                lines.append('')
                if not active:
                    continue
                lines.append('| 笔记 | 摘要 | tags | status |')
                lines.append('|------|------|------|--------|')
                for i in active:
                    lines.append(_format_row(i))
                lines.append('')
            if archived_all:
                names = ', '.join(
                    f'[[{_escape_pipe(i["name"])}]]' for i in archived_all)
                lines.append(f'> 已归档 {len(archived_all)} 篇: {names}')
            result[category] = '\n'.join(lines).rstrip() + '\n'
            continue

        has_sub = any(s for s in subs.keys() if s)

        # 确定排序顺序
        if has_sub:
            subcats = sorted(subs.keys(), key=lambda x: ('', x) if x else ('z',))
        else:
            subcats = ['']

        archived_all = []
        for sub in subcats:
            items = subs.get(sub, [])
            items.sort(key=lambda x: x['sort_key'], reverse=True)
            active = [i for i in items if i['status'] != 'archived']
            archived = [i for i in items if i['status'] == 'archived']
            archived_all.extend(archived)

            if has_sub:
                # 有 subcategory 的分组给实际名称,无 subcategory 的笔记归入"其他"
                heading = sub if sub else '其他'
                lines.append(f'## {heading}')
                lines.append('')

            if not active:
                continue

            lines.append('| 笔记 | 摘要 | tags | status |')
            lines.append('|------|------|------|--------|')

            # category 索引(详情页)全量列出所有 active 笔记;
            # 截断只用于 CLAUDE.md 瘦身版(_emit_claude_md)
            for i in active:
                lines.append(_format_row(i))
            lines.append('')

        if archived_all:
            # 防御性:笔记名理论上不应含 `|`,但这里是动态拼接,仍过一遍转义避免意外破坏
            names = ', '.join(
                f'[[{_escape_pipe(i["name"])}]]' for i in archived_all)
            lines.append(f'> 已归档 {len(archived_all)} 篇: {names}')

        result[category] = '\n'.join(lines).rstrip() + '\n'

    return result


def _read_entry(vault: Path, rel: str, mtime: int):
    """读取单个笔记 frontmatter,返回 entry dict(含 summary 兜底)。"""
    filepath = vault / rel
    with open(filepath, 'r', encoding='utf-8') as f:
        head = f.read(2000)
    fm = parse_frontmatter(head)
    entry = {'mtime': mtime}
    for field in ('tags', 'category', 'subcategory', 'status', 'summary', 'updated', 'created', 'project'):
        if field in fm:
            entry[field] = fm[field]
    entry['_has_frontmatter'] = bool(fm)
    if 'summary' not in entry:
        has_fm = head.lstrip().startswith('---')
        past_fm = False
        dash_count = 0
        for line in head.split('\n'):
            if line.strip() == '---':
                dash_count += 1
                if dash_count >= 2:
                    past_fm = True
                continue
            if (past_fm or not has_fm) and line.startswith('# '):
                entry['summary'] = line[2:].strip()
                break
    return entry


def _health_check(entries: dict, vault: Path, indexes_written: list):
    """诊断 frontmatter 不规范模式与孤立 INDEX 文件。

    返回 dict,各键对应一类问题的笔记/INDEX 列表。仅诊断,不修改文件。
    """
    issues = {
        'category_with_slash': [],   # category 含斜杠 → 应拆分
        'project_field': [],         # 用了过时 project 字段
        'folder_subcat_missing': [], # 子目录下 subcategory 缺失
        'no_frontmatter': [],        # 完全无 frontmatter
        'stale_indexes': [],         # 磁盘上有但本次未写入的 INDEX
    }

    for rel, entry in entries.items():
        cat = entry.get('category', '')
        sub = entry.get('subcategory', '')
        proj = entry.get('project', '')
        has_fm = entry.get('_has_frontmatter', True)
        _, inferred_sub = _infer_path_grouping(rel)

        if not has_fm:
            issues['no_frontmatter'].append(rel)
            continue
        if cat and '/' in cat:
            issues['category_with_slash'].append({'path': rel, 'category': cat})
        if proj:
            issues['project_field'].append({'path': rel, 'project': proj})
        # 工作日志按 YYYY年/MM月/ 嵌套，由 _emit_category_index 专用分支按 stem 拆年月，
        # 不依赖 frontmatter.subcategory；这里跳过避免误报
        if inferred_sub and not sub and cat and '/' not in cat and cat != '工作日志':
            issues['folder_subcat_missing'].append({'path': rel, 'inferred': inferred_sub})

    written_set = set(indexes_written)
    _skip_dirs = {'.obsidian', '.meta', '.git', 'node_modules', '.trash'}
    for idx_path in vault.rglob('*.md'):
        # 排除 macOS AppleDouble metadata 副本
        if idx_path.name.startswith('._'):
            continue
        rel = str(idx_path.relative_to(vault)).replace('\\', '/')
        if any(part in _skip_dirs for part in rel.split('/')):
            continue
        # 只关心系统生成的索引文件;新名根索引在 written_set 中会被下面跳过
        if not is_system_index(rel):
            continue
        if rel in written_set:
            continue
        issues['stale_indexes'].append(rel)

    # spec/plan 无 backlink + unresolved 悬空链接（窄化，不做全量 orphan）
    try:
        import _linkgraph
        _lg = _linkgraph.analyze(str(vault))
        issues['specplan_no_backlink'] = _lg['specplan_no_backlink']
        issues['unresolved_links'] = _lg['unresolved_links']
    except Exception as e:
        issues['specplan_no_backlink'] = []
        issues['unresolved_links'] = []
        issues['_linkgraph_error'] = str(e)

    return issues


def _fix_frontmatter_in_file(vault: Path, rel: str, fix_actions: list):
    """对单个笔记按 fix_actions 修复 frontmatter,原子写入。

    fix_actions: 列表,每项 {'type': 'split_category'|'drop_project'|'add_subcategory', ...}
    返回 (success: bool, message: str)
    """
    path = vault / rel
    try:
        txt = path.read_text(encoding='utf-8')
    except Exception as e:
        return False, f'读取失败: {e}'

    m = re.match(r'^(---\n)(.*?)(\n---)', txt, re.DOTALL)
    if not m:
        return False, '无 frontmatter,跳过(plans/specs 类临时文档不自动补 frontmatter)'

    head, body, tail = m.group(1), m.group(2), m.group(3)
    rest = txt[m.end():]
    new_body = body

    for action in fix_actions:
        atype = action['type']
        if atype == 'split_category':
            new_cat, new_sub = action['new_cat'], action['new_sub']
            new_body = re.sub(r'^category:\s*.+?$', f'category: {new_cat}',
                              new_body, count=1, flags=re.MULTILINE)
            if re.search(r'^subcategory:', new_body, re.MULTILINE):
                new_body = re.sub(r'^subcategory:\s*.+?$', f'subcategory: {new_sub}',
                                  new_body, count=1, flags=re.MULTILINE)
            else:
                new_body = re.sub(r'^(category:\s*.+?)$', rf'\1\nsubcategory: {new_sub}',
                                  new_body, count=1, flags=re.MULTILINE)
        elif atype == 'drop_project':
            new_body2 = re.sub(r'^project:\s*.+?\n', '', new_body,
                               count=1, flags=re.MULTILINE)
            new_body2 = re.sub(r'\nproject:\s*.+?$', '', new_body2,
                               count=1, flags=re.MULTILINE)
            new_body = new_body2
        elif atype == 'add_subcategory':
            sub_val = action['value']
            if not re.search(r'^subcategory:', new_body, re.MULTILINE):
                if re.search(r'^category:', new_body, re.MULTILINE):
                    new_body = re.sub(r'^(category:\s*.+?)$',
                                      rf'\1\nsubcategory: {sub_val}',
                                      new_body, count=1, flags=re.MULTILINE)
                else:
                    new_body = f'subcategory: {sub_val}\n' + new_body

    new_txt = head + new_body + tail + rest
    if new_txt == txt:
        return False, '无需修改'

    try:
        atomic_write_text(str(path), new_txt)
        return True, 'ok'
    except Exception as e:
        return False, f'写入失败: {e}'


def fix_frontmatter(vault: Path, issues: dict):
    """根据 health_check 结果自动修复 frontmatter,按文件聚合操作。

    返回 (fixed: int, failed: list[(rel, reason)])
    """
    by_file = defaultdict(list)
    for item in issues.get('category_with_slash', []):
        cat = item['category']
        new_cat, new_sub = cat.split('/', 1)
        by_file[item['path']].append({
            'type': 'split_category', 'new_cat': new_cat, 'new_sub': new_sub,
        })
    for item in issues.get('project_field', []):
        by_file[item['path']].append({'type': 'drop_project'})
    for item in issues.get('folder_subcat_missing', []):
        by_file[item['path']].append({
            'type': 'add_subcategory', 'value': item['inferred'],
        })

    fixed = 0
    failed = []
    for rel, actions in by_file.items():
        # 若同时有 split_category 与 add_subcategory,split 已写入 subcategory,跳过 add
        types = {a['type'] for a in actions}
        if 'split_category' in types:
            actions = [a for a in actions if a['type'] != 'add_subcategory']
        # 若有 drop_project 但无 add_subcategory,且有目录推断的 subcategory,
        # health_check 已通过 folder_subcat_missing 单独列出,这里不重复
        ok, msg = _fix_frontmatter_in_file(vault, rel, actions)
        if ok:
            fixed += 1
        else:
            failed.append((rel, msg))
    return fixed, failed


def archive_stale_indexes(vault: Path, stale: list):
    """把孤立 INDEX 移到 .meta/archived-indexes/<date>/ 而非直接删除。

    返回 (archived: int, errors: list[(rel, reason)])
    """
    if not stale:
        return 0, []
    archive_root = vault / '.meta' / 'archived-indexes' / datetime.now().strftime('%Y-%m-%d')
    archive_root.mkdir(parents=True, exist_ok=True)
    archived = 0
    errors = []
    for rel in stale:
        src = vault / rel
        if not src.exists():
            continue
        # 保留目录结构,平展到归档目录(用 - 分隔)避免歧义
        flat_name = rel.replace('/', '__')
        dst = archive_root / flat_name
        try:
            src.replace(dst)
            archived += 1
        except Exception as e:
            errors.append((rel, str(e)))
    return archived, errors


def update_claude_md(vault, index_content):
    """更新 CLAUDE.md 的索引区（并发安全），返回是否成功。

    使用文件锁防止多窗口同时更新索引区时相互覆盖。
    """
    claude_md = vault / 'CLAUDE.md'
    if not claude_md.exists():
        return False

    lock_path = str(claude_md) + '.lock'
    if not _acquire_lock(lock_path):
        print('警告: 无法获取 CLAUDE.md 文件锁，跳过索引更新', file=sys.stderr)
        return False

    try:
        # 在锁内重新读取，确保获取最新内容
        with open(claude_md, 'r', encoding='utf-8') as f:
            content = f.read()

        start_markers = [
            '<!-- 索引区：以下内容由 /summarize-session 自动生成，请勿手动编辑 -->',
            '<!-- 索引区 -->',
        ]
        end_marker = '<!-- /索引区 -->'

        start_pos = -1
        start_len = 0
        for marker in start_markers:
            pos = content.find(marker)
            if pos != -1:
                start_pos = pos
                start_len = len(marker)
                break

        if start_pos == -1:
            return False

        before = content[:start_pos + start_len]

        end_pos = content.find(end_marker, start_pos + start_len)
        if end_pos != -1:
            after = content[end_pos:]
        else:
            after = end_marker + '\n'

        new_content = before + '\n' + index_content + '\n' + after

        # 原子写入（带 Windows 占用重试，防 Obsidian/OCular 锁 CLAUDE.md 致 WinError 5）
        atomic_write_text(str(claude_md), new_content)

        return True
    finally:
        _release_lock(lock_path)


def main():
    parser = argparse.ArgumentParser(description='重建 Vault CLAUDE.md 索引')
    parser.add_argument('--vault', required=True, help='Vault 根目录路径')
    parser.add_argument('--cache', help='缓存文件路径(默认: $VAULT/.meta/frontmatter-cache.json)')
    parser.add_argument('--max-log-days', type=int, default=7,
                        help='工作日志最多显示天数 (默认: 7)')
    parser.add_argument('--max-lines', type=int, default=200,
                        help='索引区最大行数 (默认: 200)')
    parser.add_argument('--emit', choices=['claude_md', 'indexes', 'all'],
                        default='claude_md',
                        help='生成目标:claude_md (默认,瘦身版 CLAUDE.md) / indexes (各目录 {category} 索引.md) / all')
    parser.add_argument('--dry-run', action='store_true',
                        help='仅输出索引内容,不写入文件')
    parser.add_argument('--fix-frontmatter', action='store_true',
                        help='按 health_check 结果自动修复笔记 frontmatter '
                             '(category 拆斜杠/迁移 project 字段/补 subcategory),'
                             '修改原文件,Vault 是 git 仓库可回滚')
    parser.add_argument('--archive-stale-indexes', action='store_true',
                        help='把孤立 INDEX 移到 .meta/archived-indexes/<date>/,'
                             '不直接删除')
    parser.add_argument('--health-check-only', action='store_true',
                        help='仅运行 frontmatter/孤立 INDEX 诊断,不写入索引')
    args = parser.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print('错误: Vault 路径不存在或不是目录: {}'.format(vault), file=sys.stderr)
        sys.exit(1)

    cache_path = Path(args.cache) if args.cache else vault / '.meta' / 'frontmatter-cache.json'

    exclude_dirs = {'.obsidian', '.meta', '.git', 'node_modules', '.trash'}

    # 1. 加载缓存
    cache = load_cache(cache_path)
    entries = cache.get('entries', {})

    # 2. 扫描文件
    all_files = scan_vault(vault, exclude_dirs)

    # 3. 比对变更
    to_read = []
    for rel, mtime in all_files.items():
        cached = entries.get(rel)
        if not cached or cached.get('mtime') != mtime:
            to_read.append(rel)

    # 删除已不存在的文件
    deleted = [rel for rel in list(entries.keys()) if rel not in all_files]
    for rel in deleted:
        del entries[rel]

    # 4. 读取变更文件的 frontmatter
    for rel in to_read:
        try:
            entries[rel] = _read_entry(vault, rel, all_files[rel])
        except Exception as e:
            print('警告: 读取 {} 失败: {}'.format(rel, e), file=sys.stderr)

    # 4.5. 可选:--fix-frontmatter 自动修复 + 刷新受影响 entries
    fix_report = None
    if args.fix_frontmatter and not args.dry_run:
        pre_issues = _health_check(entries, vault, indexes_written=[])
        affected = set()
        for item in pre_issues['category_with_slash']:
            affected.add(item['path'])
        for item in pre_issues['project_field']:
            affected.add(item['path'])
        for item in pre_issues['folder_subcat_missing']:
            affected.add(item['path'])
        fixed, failed = fix_frontmatter(vault, pre_issues)
        for rel in affected:
            fp = vault / rel
            if fp.exists():
                try:
                    new_mtime = int(fp.stat().st_mtime)
                    entries[rel] = _read_entry(vault, rel, new_mtime)
                except Exception as e:
                    print('警告: 重读 {} 失败: {}'.format(rel, e), file=sys.stderr)
        fix_report = {'fixed': fixed, 'failed': failed, 'affected_files': sorted(affected)}

    # --health-check-only:跳过索引写入,直接输出诊断
    if args.health_check_only:
        # 从 entries 推断本应存在的 INDEX 集合,避免把合法 INDEX 误标为孤立
        predicted_indexes = set()
        for rel, entry in entries.items():
            cat, _ = _resolve_grouping(entry, rel)
            if cat:
                predicted_indexes.add('{0}/{0} 索引.md'.format(cat))
            else:
                predicted_indexes.add('未分类 索引.md')
        issues = _health_check(entries, vault, sorted(predicted_indexes))
        report = {
            'total_notes': len(entries),
            'health_check': issues,
            'fix_report': fix_report,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 5. 生成索引
    if args.emit in ('claude_md', 'all'):
        index_content = _emit_claude_md(entries, args.max_log_days)
        # 行数校验(spec §8.3)
        if index_content.count('\n') > args.max_lines:
            print(
                '警告: CLAUDE.md 索引区 {} 行,超过 {} 行上限'.format(
                    index_content.count('\n'), args.max_lines),
                file=sys.stderr,
            )
    else:
        index_content = None

    indexes = {}
    if args.emit in ('indexes', 'all'):
        indexes = _emit_category_index(entries, args.max_log_days)

    if args.dry_run:
        if index_content is not None:
            print('=== CLAUDE.md ===')
            print(index_content)
        for cat, text in indexes.items():
            _name = '未分类 索引.md' if cat == '' else '{0}/{0} 索引.md'.format(cat)
            print('=== {} ==='.format(_name))
            print(text)
        sys.exit(0)

    # 6. 写入
    updated = False
    if index_content is not None:
        updated = update_claude_md(vault, index_content)

    index_files_written = []
    for cat, text in indexes.items():
        # 防御:cat 来自目录名/frontmatter,写入前拒绝含路径分隔符/穿越段的值,
        # 防 frontmatter 注入 category=../.. 把索引写到 vault 外(CWE-22)
        if cat and ('/' in cat or '\\' in cat or '..' in cat.split('/')):
            print('警告: category 含非法路径字符,跳过索引写入: {}'.format(cat),
                  file=sys.stderr)
            continue
        if cat == '':
            idx_path = vault / '未分类 索引.md'
        else:
            idx_path = vault / cat / '{} 索引.md'.format(cat)
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(idx_path), text)
        index_files_written.append(
            str(idx_path.relative_to(vault)).replace('\\', '/'))

    # 7. 写回缓存（并发安全：锁 + 原子写入）
    cache['entries'] = entries
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_lock = str(cache_path) + '.lock'
    cache_written = False
    if _acquire_lock(cache_lock):
        try:
            atomic_write_json(str(cache_path), cache)
            cache_written = True
        finally:
            _release_lock(cache_lock)
    else:
        # 锁失败时跳过缓存写入（不覆盖，以免丢失其他窗口的并发写入）。
        # 索引本身已经成功写入 CLAUDE.md；缓存只是性能优化，下次全量重建即可。
        print(
            '警告: 缓存锁获取失败（超时 {}s），本次跳过缓存写入，下次将全量重建'.format(LOCK_TIMEOUT),
            file=sys.stderr,
        )

    # 8. 最终 health_check + 可选孤立 INDEX 归档
    final_issues = _health_check(entries, vault, index_files_written)

    archive_report = None
    if args.archive_stale_indexes and final_issues['stale_indexes']:
        archived, errors = archive_stale_indexes(vault, final_issues['stale_indexes'])
        archive_report = {'archived': archived, 'errors': errors}
        # 归档后重新跑诊断,stale_indexes 应为空
        final_issues = _health_check(entries, vault, index_files_written)

    # 9. 输出报告
    report = {
        'total_notes': len(entries),
        'scanned': len(to_read),
        'deleted': len(deleted),
        'index_updated': updated,
        'cache_written': cache_written,
        'indexes_written': index_files_written,
        'health_check': {
            'no_frontmatter': len(final_issues['no_frontmatter']),
            'category_with_slash': len(final_issues['category_with_slash']),
            'project_field': len(final_issues['project_field']),
            'folder_subcat_missing': len(final_issues['folder_subcat_missing']),
            'stale_indexes': final_issues['stale_indexes'],
            'specplan_no_backlink': len(final_issues.get('specplan_no_backlink', [])),
            'unresolved_links': len(final_issues.get('unresolved_links', [])),
            'specplan_no_backlink_samples': final_issues.get('specplan_no_backlink', [])[:10],
            'unresolved_links_samples': final_issues.get('unresolved_links', [])[:10],
        },
    }
    if fix_report:
        report['fix_frontmatter'] = fix_report
    if archive_report:
        report['archive_stale_indexes'] = archive_report
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
