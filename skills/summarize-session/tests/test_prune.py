"""tests for scripts/prune.py"""
import sys
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prune import is_dead_entry, partition_dead, backup_pending


def test_dead_when_no_vault_missing_and_path_absent():
    e = {'path': '/nonexistent/x.md', 'original_missing': True}
    assert is_dead_entry(e, path_exists=lambda p: False) is True


def test_not_dead_when_path_exists():
    """original_missing 但 path 现存（活文件）→ 不是 dead。"""
    e = {'path': '/some/x.md', 'original_missing': True}
    assert is_dead_entry(e, path_exists=lambda p: True) is False


def test_not_dead_when_has_vault_path():
    e = {'path': '/x.md', 'original_missing': True, 'vault_path': '/V/x.md'}
    assert is_dead_entry(e, path_exists=lambda p: False) is False


def test_not_dead_when_not_original_missing():
    e = {'path': '/x.md'}
    assert is_dead_entry(e, path_exists=lambda p: False) is False


def test_path_invalid_not_dead():
    e = {'path': '~/x.md', 'path_invalid': True}
    assert is_dead_entry(e, path_exists=lambda p: False) is False


def test_oserror_treated_as_exists():
    def boom(p):
        raise OSError('boom')
    e = {'path': '/x.md', 'original_missing': True}
    assert is_dead_entry(e, path_exists=boom) is False


def test_partition_separates_and_preserves_order():
    pending = [
        {'path': '/a', 'vault_path': '/V/a'},
        {'path': '/b', 'original_missing': True},
        {'path': '/c', 'vault_path': '/V/c', 'original_missing': True},
        {'path': '/d', 'original_missing': True},
    ]
    alive, dead = partition_dead(pending, path_exists=lambda p: False)
    assert [e['path'] for e in alive] == ['/a', '/c']
    assert [e['path'] for e in dead] == ['/b', '/d']


def test_partition_keeps_alive_when_path_exists():
    pending = [{'path': '/live', 'original_missing': True}]
    alive, dead = partition_dead(pending, path_exists=lambda p: True)
    assert len(alive) == 1 and dead == []


def test_backup_creates_rotated_bak(tmp_path):
    p = tmp_path / 'pending-docs.json'
    p.write_text('[{"path":"/x"}]', encoding='utf-8')
    bak = backup_pending(str(p))
    assert bak == str(p) + '.bak.1'
    assert pathlib.Path(bak).read_text(encoding='utf-8') == '[{"path":"/x"}]'


def test_backup_rotation_keeps_5(tmp_path):
    p = tmp_path / 'pending-docs.json'
    for i in range(7):
        p.write_text(f'[{{"v":{i}}}]', encoding='utf-8')
        backup_pending(str(p))
    base = str(p)
    # 保留 .bak.1..5，.bak.6/.7 不存在
    assert pathlib.Path(base + '.bak.1').read_text(encoding='utf-8') == '[{"v":6}]'  # 最新
    assert pathlib.Path(base + '.bak.5').read_text(encoding='utf-8') == '[{"v":2}]'  # 最老保留
    assert not pathlib.Path(base + '.bak.6').exists()


def test_backup_migrates_legacy_bak(tmp_path):
    """存量旧单一 .bak 迁移进轮转，不成孤儿。"""
    p = tmp_path / 'pending-docs.json'
    p.write_text('[{"v":"new"}]', encoding='utf-8')
    legacy = pathlib.Path(str(p) + '.bak')
    legacy.write_text('[{"v":"legacy"}]', encoding='utf-8')
    backup_pending(str(p))
    base = str(p)
    assert pathlib.Path(base + '.bak.1').read_text(encoding='utf-8') == '[{"v":"new"}]'
    assert pathlib.Path(base + '.bak.2').read_text(encoding='utf-8') == '[{"v":"legacy"}]'
    assert not legacy.exists()  # 旧无序号 .bak 已迁移


def test_backup_missing_source_returns_none(tmp_path):
    assert backup_pending(str(tmp_path / 'nope.json')) is None


def test_path_none_not_dead():
    """path 为 None（脏数据）→ 保守保留不删，且不崩溃（os.path.exists(None) 会抛 TypeError）。"""
    e = {'path': None, 'original_missing': True}
    assert is_dead_entry(e, path_exists=lambda p: True) is False


def test_path_empty_not_dead():
    """path 为空字符串 → 保守保留不删。"""
    e = {'path': '', 'original_missing': True}
    assert is_dead_entry(e, path_exists=lambda p: True) is False
