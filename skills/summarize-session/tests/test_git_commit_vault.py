"""tests for scripts/git_commit_vault.py"""
import os
import sys
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import git_commit_vault as gcv  # noqa: E402


def _init_repo(path):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.email', 't@t'], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.name', 't'], check=True)


def test_skipped_when_not_git(tmp_path):
    r = gcv.commit_vault(str(tmp_path), '标题')
    assert r['status'] == 'skipped' and r['reason'] == 'not_git'


def test_skipped_when_no_commit_flag(tmp_path):
    _init_repo(tmp_path)
    r = gcv.commit_vault(str(tmp_path), '标题', no_commit=True)
    assert r['status'] == 'skipped' and r['reason'] == 'no_commit_flag'


def test_sanitize_title_strips_control_and_truncates():
    assert '\n' not in gcv.sanitize_title('a\nb')
    assert gcv.sanitize_title('x"; rm -rf $(y) `z`')  # 不抛异常，返回字符串
    assert len(gcv.sanitize_title('一' * 100)) <= 60


def test_enumerate_only_md_in_include_dirs(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / '工作日志').mkdir()
    (tmp_path / '工作日志' / 'a.md').write_text('x', encoding='utf-8')
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'b.md').write_text('y', encoding='utf-8')
    (tmp_path / 'CLAUDE.md').write_text('z', encoding='utf-8')
    (tmp_path / 'junk.txt').write_text('no', encoding='utf-8')  # 非 .md 不选
    (tmp_path / '.meta').mkdir()
    (tmp_path / '.meta' / 'pending-docs.json').write_text('[]', encoding='utf-8')  # 不在白名单
    changes = gcv.enumerate_changes(str(tmp_path))
    assert '工作日志/a.md' in changes
    assert 'Claude Code/b.md' in changes
    assert 'CLAUDE.md' in changes
    assert 'junk.txt' not in changes
    assert all(not c.startswith('.meta') for c in changes)


def test_commit_creates_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'note.md').write_text('内容', encoding='utf-8')
    r = gcv.commit_vault(str(tmp_path), '测试笔记')
    assert r['status'] == 'committed'
    log = subprocess.run(['git', '-C', str(tmp_path), 'log', '--oneline'],
                         capture_output=True, text=True, encoding='utf-8')
    assert '测试笔记' in log.stdout


def test_nothing_to_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')  # 非知识库 .md
    r = gcv.commit_vault(str(tmp_path), '标题')
    assert r['status'] == 'nothing'


def test_baseline_preview_lists_files(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'n.md').write_text('x', encoding='utf-8')
    r = gcv.baseline_preview(str(tmp_path))
    assert r['status'] == 'preview'
    assert any('n.md' in f for f in r['files'])


def test_commit_failed_does_not_raise(tmp_path, monkeypatch):
    """commit 失败 → status=failed，不抛异常（不阻塞 skill 后续，finding F）。"""
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'n.md').write_text('x', encoding='utf-8')
    orig = gcv._git

    def fake(vault, args):
        if args and args[0] == 'commit':
            class R:
                returncode = 1
                stdout = ''
                stderr = 'simulated failure'
            return R()
        return orig(vault, args)

    monkeypatch.setattr(gcv, '_git', fake)
    res = gcv.commit_vault(str(tmp_path), '标题')
    assert res['status'] == 'failed' and 'git_commit' in res['reason']


def test_baseline_commits_all_including_non_md(tmp_path):
    """--baseline 全量 commit（含非 .md + 中文路径，finding D）。"""
    _init_repo(tmp_path)
    (tmp_path / '工作日志').mkdir()
    (tmp_path / '工作日志' / '中文.md').write_text('x', encoding='utf-8')
    (tmp_path / 'random.txt').write_text('y', encoding='utf-8')
    res = gcv.commit_vault(str(tmp_path), 't', baseline=True)
    assert res['status'] == 'committed' and res['files'] == -1
    show = subprocess.run(['git', '-C', str(tmp_path), '-c', 'core.quotepath=false',
                           'show', '--name-only', '--format='],
                          capture_output=True, text=True, encoding='utf-8')
    assert '中文.md' in show.stdout and 'random.txt' in show.stdout


def test_rename_stages_old_path_deletion(tmp_path):
    """知识库 .md 重命名时旧路径删除也被 stage（M2，避免孤立 + commit 记凭空新增）。"""
    _init_repo(tmp_path)
    d = tmp_path / 'Claude Code'
    d.mkdir()
    (d / 'old.md').write_text('内容相同足够触发 rename 检测的文本', encoding='utf-8')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'commit', '-qm', 'init'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'mv', 'Claude Code/old.md', 'Claude Code/new.md'], check=True)
    changes = gcv.enumerate_changes(str(tmp_path))
    assert 'Claude Code/new.md' in changes
    assert 'Claude Code/old.md' in changes  # 旧路径删除也纳入


from git_commit_vault import _is_knowledge_md


def test_is_knowledge_md_new_index_names():
    assert _is_knowledge_md('未分类 索引.md') is True
    assert _is_knowledge_md('Windows 系统/Windows 系统 索引.md') is True
    assert _is_knowledge_md('改进计划/改进计划 索引.md') is True


def test_arbitrary_suffix_note_is_not_mistaken_for_a_system_index():
    """原用例断言 `随便目录/随便 索引.md` 被拒，那依赖「top 不在白名单」这半边。
    2026-09-03 判据反转为排除法后，它作为**普通笔记**被纳入才是正确行为。
    本用例改为守住原本真正要守的那一半：文件名前缀 != 父目录名 ⇒ 它不是系统索引，
    只是恰好叫这个名字（防止伪装成索引的文件走 is_system_index 那条快速放行）。"""
    from rebuild_index import is_system_index
    assert is_system_index('随便目录/随便 索引.md') is False
    assert _is_knowledge_md('随便目录/随便 索引.md') is True


def test_notes_outside_the_former_whitelist_are_included():
    """2026-09-03 回归守卫。

    原白名单只有 工作日志/Claude Code/项目笔记/缺陷全链路/技术笔记/偏好与习惯/参考资料/领域，
    漏掉了某用户 Vault 里真实存在的 4 个顶层目录。这些目录下只有系统索引能靠
    is_system_index 放行，普通笔记的新增与修改被**静默**漏提交：实测 6 篇长期滞留，
    其中 4 篇从未入过 git 且被该用户的 CLAUDE.local.md 直接引用。

    最后一条（未来才新建的目录）是本用例的重点：白名单法每加一个顶层目录就要改代码，
    忘了改就再次静默漏掉——排除法必须让新目录自动纳入。
    """
    for p in ('Windows 系统/AppXSvc 句柄泄漏根因 2026-08-06.md',
              '改进计划/2026-05-19-x.md',
              'plans/2026-09-03-y.md',
              'specs/2026-09-03-z.md',
              '将来才新建的顶层目录/某笔记.md'):
        assert _is_knowledge_md(p) is True, p


def test_tool_and_metadata_dirs_are_excluded():
    """排除法的另一半：工具/元数据目录不得被收进来。
    判据是「顶层段以 . 开头」，一次覆盖 .obsidian/.meta/.trash/.git，
    不逐个枚举——枚举正是白名单缺陷的成因。"""
    for p in ('.obsidian/plugins/foo/README.md',
              '.meta/notes.md',
              '.trash/deleted.md',
              '.git/x.md',
              '.hidden.md'):
        assert _is_knowledge_md(p) is False, p


def test_system_index_form_inside_tool_dirs_is_still_excluded():
    """判据**顺序**契约：点目录排除必须先于 `is_system_index`。

    `is_system_index` 的规则是「文件名前缀 == 父目录名」，而工具/元数据目录下同样
    能出现这种形态（`.obsidian/.obsidian 索引.md`）。2026-09-03 引入点目录排除时它被
    放在 `is_system_index` **之后**，于是这批路径被抢先放行；而同批新增的
    `test_tool_and_metadata_dirs_are_excluded` 用的样本全是**非**索引形态，正好测不到
    这个缺口。

    第一条断言是**阳性对照**：若这些路径根本不是 is_system_index 形态，本用例就
    退化成在测一条与顺序无关的普通排除，必须让它当场失败而不是假绿。
    """
    from rebuild_index import is_system_index
    for p in ('.obsidian/.obsidian 索引.md',
              '.meta/.meta 索引.md',
              '.trash/.trash 索引.md',
              '.git/.git 索引.md'):
        assert is_system_index(p) is True, (
            f"阳性对照失败：{p} 不是 is_system_index 形态，本用例已失去判别力")
        assert _is_knowledge_md(p) is False, p


def test_is_knowledge_md_path_traversal_still_blocked():
    assert _is_knowledge_md('../外部 索引.md') is False
    assert _is_knowledge_md('/abs/x 索引.md') is False
