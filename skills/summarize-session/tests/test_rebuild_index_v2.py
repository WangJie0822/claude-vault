from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest

from rebuild_index import (
    _emit_category_index,
    _emit_claude_md,
    _escape_pipe,
    _format_row,
    _health_check,
    scan_vault,
)


def _mk_entries():
    return {
        '缺陷全链路/bug-batch/a.md': {
            'mtime': 0, 'category': '缺陷全链路', 'subcategory': 'bug-batch',
            'summary': 'a', 'tags': ['x'], 'status': 'active',
            'updated': '2026-04-20',
        },
        '缺陷全链路/bug-batch/b.md': {
            'mtime': 0, 'category': '缺陷全链路', 'subcategory': 'bug-batch',
            'summary': 'b', 'tags': [], 'status': 'active',
            'updated': '2026-04-19',
        },
        '缺陷全链路/bug-analyze/c.md': {
            'mtime': 0, 'category': '缺陷全链路', 'subcategory': 'bug-analyze',
            'summary': 'c', 'tags': [], 'status': 'archived',
            'updated': '2026-04-15',
        },
        '技术笔记/tech.md': {
            'mtime': 0, 'category': '技术笔记',
            'summary': 'tech', 'tags': [], 'status': 'active',
            'updated': '2026-04-18',
        },
    }


def test_emit_claude_md_under_80_lines():
    entries = _mk_entries()
    out = _emit_claude_md(entries, max_log_days=7)
    lines = out.count('\n')
    assert lines <= 80
    # 汇总行包含 "缺陷全链路" 和总数
    assert '缺陷全链路' in out
    assert 'bug-batch' in out
    assert 'bug-analyze' not in out or '0 篇' in out  # archived 不计入


def test_emit_claude_md_subcategory_counts():
    entries = _mk_entries()
    out = _emit_claude_md(entries, max_log_days=7)
    # bug-batch 2 篇(2 active)
    assert '2 篇' in out


def test_emit_category_index_groups_by_subcategory():
    entries = _mk_entries()
    result = _emit_category_index(entries)
    # 返回 {category: index_md_text}
    assert '缺陷全链路' in result
    idx = result['缺陷全链路']
    assert '## bug-batch' in idx
    # 归档笔记折叠到底部
    assert '已归档' in idx


def test_emit_category_index_omits_empty_subcategory():
    entries = {
        '技术笔记/a.md': {
            'mtime': 0, 'category': '技术笔记',
            'summary': 'a', 'tags': [], 'status': 'active',
        },
    }
    result = _emit_category_index(entries)
    idx = result['技术笔记']
    # 技术笔记扁平(无 subcategory),不输出二级分组
    assert '##' not in idx.split('| 笔记')[0]  # 第一个表格前不出现二级标题


# --- Important #2: markdown 表格 `|` escape 回归测试 ---


def test_format_row_escapes_pipe_in_summary():
    """summary 含 `|` 时应转义为 `\\|`,防止破坏表格列数。"""
    item = {
        'name': 'foo',
        'summary': 'a | b | c',
        'tags': '—',
        'status': 'active',
    }
    row = _format_row(item)
    assert 'a \\| b \\| c' in row
    # 原始未转义形式不应出现
    assert 'a | b | c' not in row


def test_format_row_escapes_pipe_in_tags():
    """tags 含 `|` 时应转义。"""
    item = {
        'name': 'foo',
        'summary': 's',
        'tags': 'tag1 | tag2',
        'status': 'active',
    }
    row = _format_row(item)
    assert 'tag1 \\| tag2' in row


def test_format_row_rejects_pipe_in_name():
    """name 含 `|` 会同时破坏 wikilink 和表格,应抛 ValueError。"""
    item = {
        'name': 'foo|bar',
        'summary': 's',
        'tags': '—',
        'status': 'active',
    }
    with pytest.raises(ValueError):
        _format_row(item)


def test_escape_pipe_handles_none_and_numbers():
    """_escape_pipe 处理 None/数字等非字符串输入。"""
    assert _escape_pipe(None) == ''
    assert _escape_pipe(42) == '42'
    assert _escape_pipe('a|b') == 'a\\|b'


def test_emit_category_index_pipe_safe():
    """端到端:summary 含 `|` 时,生成的表格行列数正确(pipe 不误切列)。"""
    import re

    entries = {
        '技术笔记/a.md': {
            'mtime': 0, 'category': '技术笔记',
            'summary': 'a | b | c',  # 含 pipe 的 summary
            'tags': [], 'status': 'active',
            'updated': '2026-04-20',
        },
    }
    result = _emit_category_index(entries)
    idx = result['技术笔记']

    # 找到数据行(含 [[a]] 的行)
    data_row = None
    for line in idx.split('\n'):
        if line.startswith('| [[a]]'):
            data_row = line
            break
    assert data_row is not None, "未找到数据行"

    # 按未转义的 `|` 分列(用负回顾排除 `\|`):
    # 标准表格行 `| c1 | c2 | c3 | c4 |` 分割后应为 6 段(首尾空+4列)
    cols = re.split(r'(?<!\\)\|', data_row)
    assert len(cols) == 6, \
        "列数错误: 期望 6 段,实际 {}(含转义的 `|` 被误切): {!r}".format(
            len(cols), cols)


def test_generate_index_removed():
    """验证孤儿函数 generate_index 已删除。"""
    import rebuild_index
    assert not hasattr(rebuild_index, 'generate_index'), \
        "generate_index 应已被删除"


# --- scan_vault 排除 INDEX.md ---

def test_scan_vault_excludes_index_md(tmp_path):
    """INDEX.md 是索引文件本身,不应被 scan_vault 收集。"""
    vault = tmp_path / 'vault'
    (vault / '领域A').mkdir(parents=True)
    (vault / '领域A' / 'INDEX.md').write_text('# 领域A 索引', encoding='utf-8')
    (vault / '领域A' / '笔记.md').write_text('正文', encoding='utf-8')
    (vault / 'CLAUDE.md').write_text('根 CLAUDE', encoding='utf-8')

    files = scan_vault(vault, exclude_dirs={'.obsidian', '.meta', '.git'})
    assert '领域A/笔记.md' in files
    assert '领域A/INDEX.md' not in files
    assert 'CLAUDE.md' not in files


# --- has_sub=True 时无 subcategory 的笔记归入"其他"分组 ---

def test_emit_category_index_work_log_grouped_by_year_month():
    """工作日志 INDEX.md 按 年/月 分组,全量列出。"""
    entries = {}
    # 跨 3 个月的 15 条日志
    for m, dmax in (('03', 5), ('04', 5), ('05', 5)):
        for day in range(1, dmax + 1):
            key = f'工作日志/2026年/{m}月/2026-{m}-{day:02d}.md'
            entries[key] = {
                'mtime': 0, 'category': '工作日志',
                'summary': f'2026-{m}-{day:02d} 记录', 'tags': ['工作日志'],
                'status': 'active', 'updated': f'2026-{m}-{day:02d}',
            }
    result = _emit_category_index(entries, max_log_days=7)
    idx = result['工作日志']

    # 三个月分组小标题都在
    assert '## 2026年 / 05月' in idx
    assert '## 2026年 / 04月' in idx
    assert '## 2026年 / 03月' in idx

    # 月内倒序:05 月分组里 2026-05-05 在 2026-05-01 之前
    pos_05 = idx.index('[[2026-05-05]]')
    pos_01 = idx.index('[[2026-05-01]]')
    assert pos_05 < pos_01

    # 月之间倒序:05 月分组整体在 03 月分组之前
    pos_may = idx.index('## 2026年 / 05月')
    pos_mar = idx.index('## 2026年 / 03月')
    assert pos_may < pos_mar

    # 全量列出,无截断占位
    assert '另有' not in idx
    for m, dmax in (('03', 5), ('04', 5), ('05', 5)):
        for day in range(1, dmax + 1):
            assert f'[[2026-{m}-{day:02d}]]' in idx


def test_emit_claude_md_work_log_still_truncates():
    """CLAUDE.md 瘦身版保留截断,显示最近 max_log_days 条;支持嵌套 slug。"""
    entries = {}
    for day in range(1, 16):
        key = f'工作日志/2026年/03月/2026-03-{day:02d}.md'
        entries[key] = {
            'mtime': 0, 'category': '工作日志',
            'summary': '', 'tags': [], 'status': 'active',
            'updated': f'2026-03-{day:02d}',
        }
    out = _emit_claude_md(entries, max_log_days=3)
    # 只显示最近 3 条(03-15/14/13)
    assert '[[2026-03-15]]' in out
    assert '[[2026-03-14]]' in out
    assert '[[2026-03-13]]' in out
    assert '[[2026-03-12]]' not in out
    assert '[[2026-03-01]]' not in out


def test_emit_category_index_mixed_sub_gives_default_heading():
    """同一 category 既有 subcategory 也有无 subcategory 笔记时,
    无 subcategory 分组应给默认标题'其他',避免表格无 `##` 前导。"""
    entries = {
        'Claude Code/specs/design.md': {
            'mtime': 0, 'category': 'Claude Code', 'subcategory': 'specs',
            'summary': 'design', 'tags': [], 'status': 'active',
            'updated': '2026-04-20',
        },
        'Claude Code/散笔记.md': {
            'mtime': 0, 'category': 'Claude Code',
            'summary': 'note', 'tags': [], 'status': 'active',
            'updated': '2026-04-21',
        },
    }
    result = _emit_category_index(entries)
    idx = result['Claude Code']
    assert '## specs' in idx
    assert '## 其他' in idx
    # 每个 `| 笔记 |` 表格前必有 `##` 标题,不允许裸表格
    segments = idx.split('| 笔记 |')
    for seg in segments[:-1]:
        tail_lines = [l for l in seg.strip().splitlines() if l.strip()]
        assert tail_lines and tail_lines[-1].startswith('##'), \
            f"表格前缺少 ## 标题: {tail_lines[-3:]}"


# --- 工作日志在 health_check 中应豁免 folder_subcat_missing ---

def test_health_check_exempts_worklog_from_folder_subcat_missing(tmp_path):
    """工作日志 entries 走 _emit_category_index 专用分支,不依赖 frontmatter.subcategory,
    所以不应触发 folder_subcat_missing 误报。"""
    entries = {
        '工作日志/2026年/05月/2026-05-19.md': {
            'mtime': 0, 'category': '工作日志',
            'tags': ['工作日志'], 'status': 'active',
            '_has_frontmatter': True,
        },
        '工作日志/2026年/04月/2026-04-20.md': {
            'mtime': 0, 'category': '工作日志',
            'tags': ['工作日志'], 'status': 'active',
            '_has_frontmatter': True,
        },
        # 对照组:其他 category 下嵌套且缺 subcategory,应被报告
        '技术笔记/sub1/x.md': {
            'mtime': 0, 'category': '技术笔记',
            'tags': [], 'status': 'active',
            '_has_frontmatter': True,
        },
    }
    issues = _health_check(entries, tmp_path, indexes_written=[])
    flagged_paths = {item['path'] for item in issues['folder_subcat_missing']}
    assert '工作日志/2026年/05月/2026-05-19.md' not in flagged_paths
    assert '工作日志/2026年/04月/2026-04-20.md' not in flagged_paths
    assert '技术笔记/sub1/x.md' in flagged_paths  # 对照组仍受检


from rebuild_index import is_system_index


def test_is_system_index_root():
    assert is_system_index('未分类 索引.md') is True


def test_is_system_index_category():
    assert is_system_index('工作日志/工作日志 索引.md') is True
    assert is_system_index('Claude Code/Claude Code 索引.md') is True
    assert is_system_index('Windows 系统/Windows 系统 索引.md') is True


def test_is_system_index_legacy():
    assert is_system_index('INDEX.md') is True
    assert is_system_index('工作日志/INDEX.md') is True


def test_is_system_index_rejects_user_notes():
    # 文件名 != 父目录名 + ' 索引.md' → 不是索引
    assert is_system_index('项目笔记/随便 索引.md') is False
    # 普通笔记
    assert is_system_index('工作日志/2026-06-22.md') is False
    # 深层目录下恰好同名也不算（规则要求恰两段）
    assert is_system_index('项目笔记/ProjectA/ProjectA 索引.md') is False


def test_is_system_index_windows_sep():
    assert is_system_index('工作日志\\工作日志 索引.md') is True


from rebuild_index import _emit_claude_md, _emit_category_index


def _entry(category='', subcategory='', summary='s', updated='2026-06-22'):
    return {'category': category, 'subcategory': subcategory,
            'summary': summary, 'updated': updated, 'status': 'active',
            'tags': [], 'mtime': 0}


def test_emit_claude_md_category_link_new_name():
    entries = {'工作日志/2026-06-22.md': _entry('工作日志'),
               '技术笔记/x.md': _entry('技术笔记')}
    out = _emit_claude_md(entries)
    assert '[[工作日志 索引|历史 →]]' in out
    assert '[[技术笔记 索引|详情 →]]' in out
    assert '/INDEX.md' not in out


def test_emit_claude_md_empty_category_unclassified():
    entries = {'游离.md': _entry('')}
    out = _emit_claude_md(entries)
    assert '## 未分类' in out
    assert '[[未分类 索引|详情 →]]' in out
    assert '(/INDEX.md)' not in out  # 不再有前导斜杠坏链


def test_emit_category_index_empty_title():
    entries = {'游离.md': _entry('')}
    result = _emit_category_index(entries)
    assert result[''].startswith('# 未分类 索引')


def test_emit_category_index_category_title():
    entries = {'技术笔记/x.md': _entry('技术笔记')}
    result = _emit_category_index(entries)
    assert result['技术笔记'].startswith('# 技术笔记 索引')


def test_scan_vault_excludes_new_index(tmp_path):
    (tmp_path / '工作日志').mkdir()
    (tmp_path / '工作日志' / '工作日志 索引.md').write_text('# 工作日志 索引', encoding='utf-8')
    (tmp_path / '工作日志' / '2026-06-22.md').write_text('# x', encoding='utf-8')
    (tmp_path / '未分类 索引.md').write_text('#  索引', encoding='utf-8')
    files = scan_vault(tmp_path, {'.git'})
    assert '工作日志/工作日志 索引.md' not in files
    assert '未分类 索引.md' not in files
    assert '工作日志/2026-06-22.md' in files


def test_health_check_flags_legacy_root_index_stale(tmp_path):
    # 旧根 INDEX.md 应被判孤立（移除 :412 root-skip 后）
    (tmp_path / 'INDEX.md').write_text('#  索引', encoding='utf-8')
    issues = _health_check({}, tmp_path, indexes_written=['未分类 索引.md'])
    assert 'INDEX.md' in issues['stale_indexes']


def test_health_check_new_root_index_not_stale(tmp_path):
    (tmp_path / '未分类 索引.md').write_text('# 未分类 索引', encoding='utf-8')
    issues = _health_check({}, tmp_path, indexes_written=['未分类 索引.md'])
    assert '未分类 索引.md' not in issues['stale_indexes']


def test_rebuild_index_rejects_traversal_category(tmp_path):
    """frontmatter category 含 .. 时拒绝写入,不逃逸 vault 外(SEC-3 guard)。"""
    import os
    import subprocess
    (tmp_path / 'note.md').write_text(
        '---\ncategory: ../../evil\n---\n# x\n', encoding='utf-8')
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / 'rebuild_index.py'),
         '--vault', str(tmp_path), '--emit', 'all'],
        capture_output=True, text=True, encoding='utf-8', env=env)
    assert '非法路径字符' in r.stderr
    # vault 外(tmp_path/../../evil)不应被创建
    assert not (tmp_path.parent.parent / 'evil').exists()


def test_read_entry_picks_up_keywords(tmp_path):
    from rebuild_index import _read_entry
    vault = tmp_path / "v"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text(
        "---\ntags: [t1]\nkeywords: [同义词, alias]\nsummary: s\n---\n# 标题\n",
        encoding="utf-8",
    )
    entry = _read_entry(vault, "a.md", 123)
    assert entry["keywords"] == ["同义词", "alias"]


def test_read_entry_no_keywords_field_absent(tmp_path):
    from rebuild_index import _read_entry
    vault = tmp_path / "v"
    vault.mkdir()
    note = vault / "b.md"
    note.write_text("---\ntags: [t1]\nsummary: s\n---\n# 标题\n", encoding="utf-8")
    entry = _read_entry(vault, "b.md", 123)
    assert "keywords" not in entry
