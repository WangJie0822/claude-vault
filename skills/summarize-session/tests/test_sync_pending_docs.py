"""tests for scripts/sync_pending_docs.py (S1-S16 + 端到端 + wikilink)"""
import os
import sys
import json
import pathlib
import hashlib
import subprocess
import tempfile
import time

import pytest

# conftest.py 已经把 scripts/ 加入 sys.path（与 test_archive_doc.py 风格一致）
from sync_pending_docs import sync, run_cli


def _make_repo(path):
    """初始化一个 git repo 作为模拟主仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.name', 't'], cwd=str(path), check=True)


def _write_pending(tmp_path, entries):
    """写 pending-docs.json 返回路径字符串。"""
    p = tmp_path / 'pending-docs.json'
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
    return str(p)


# ========== S1-S8 基础同步分支 ==========


def test_S1_skipped_unchanged_by_mtime_size(tmp_path):
    """S1: 第二次同步 mtime/size 不变 → skipped_unchanged。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    # 第一次 incremental 归集
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    # 第二次再跑，mtime/size 未变，应 skipped
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'skipped_unchanged' for r in result['items'])


def test_S3_hash_diff_overwrites_vault_copy(tmp_path):
    """S3: 仅 src 改动 → synced，Vault 副本覆盖为新正文。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    # 改原文件（sleep 让 mtime 变化）
    time.sleep(1.1)
    src.write_text('# v2 changed', encoding='utf-8')
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    sync_items = [r for r in result['items'] if r['status'] == 'synced']
    assert len(sync_items) >= 1
    # Vault 副本内容已更新
    vp = sync_items[0]['vault_path']
    text = pathlib.Path(vp).read_text(encoding='utf-8')
    assert 'v2 changed' in text


def test_S4_conflict_vault_edited(tmp_path):
    """S4: 仅 Vault 副本被手工编辑 → conflict_vault_edited，副本不被覆盖。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    r1 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    vp = next(r['vault_path'] for r in r1['items'] if r['status'] == 'new_archived')
    # 用户在 Vault 副本上手工 echo 一行
    text = pathlib.Path(vp).read_text(encoding='utf-8')
    pathlib.Path(vp).write_text(text + '\n手工添加批注\n', encoding='utf-8')
    # 原文件不变，再跑一次 → 应报 conflict_vault_edited
    time.sleep(1.1)
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'conflict_vault_edited' for r in result['items'])
    # Vault 副本未被覆盖
    text2 = pathlib.Path(vp).read_text(encoding='utf-8')
    assert '手工添加批注' in text2


def test_S5_conflict_both_edited(tmp_path):
    """S5: src 和 vault 副本都改 → conflict_both_edited。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    r1 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    vp = next(r['vault_path'] for r in r1['items'] if r['status'] == 'new_archived')
    text = pathlib.Path(vp).read_text(encoding='utf-8')
    pathlib.Path(vp).write_text(text + '\nbatch by user\n', encoding='utf-8')
    time.sleep(1.1)
    src.write_text('# v2 by author', encoding='utf-8')
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'conflict_both_edited' for r in result['items'])


def test_S6_original_missing(tmp_path):
    """S6: src 已删除 → original_missing；pending-docs 标 original_missing=true。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    src.unlink()
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    miss = [r for r in result['items'] if r['status'] == 'original_missing']
    assert len(miss) >= 1
    # pending-docs.json 的该条目带 original_missing
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert any(d.get('original_missing') is True for d in data)


def test_S12_dry_run_no_write(tmp_path):
    """S12: apply=False → 不写 Vault；items 含 new_archived_planned。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=False)
    # Vault 内不应有任何文件
    md_files = list(pathlib.Path(vault).rglob('*.md'))
    assert md_files == []
    # 但 result 中标出 new_archived 计划
    assert any(r['status'] == 'new_archived_planned' for r in result['items'])


def test_S15_path_invalid_and_denied_sensitive(tmp_path):
    """S15: ~ 开头 → path_invalid；.env → denied_sensitive。"""
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [
        {'path': '~/relative.md', 'type': 'spec', 'context': 'x', 'created': '2026-05-28'},
        {'path': str(tmp_path / 'project' / '.env'), 'type': 'other', 'context': 'x', 'created': '2026-05-28'},
    ])
    # 创建 .env 文件
    (tmp_path / 'project').mkdir()
    (tmp_path / 'project' / '.env').write_text('SECRET=x', encoding='utf-8')
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    statuses = [r['status'] for r in result['items']]
    assert 'path_invalid' in statuses
    assert 'denied_sensitive' in statuses


def test_S16_output_json_structure(tmp_path):
    """S16: 返回 dict 的分类键齐全。"""
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [])
    result = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    expected_keys = {'new_archived', 'synced', 'adopted', 'skipped_unchanged',
                     'conflict_vault_edited', 'conflict_both_edited',
                     'original_missing', 'path_invalid', 'denied_sensitive',
                     'errors', 'items', 'pruned'}
    assert expected_keys.issubset(set(result.keys()))


# ========== 端到端集成测试 ==========


def test_e2e_full_flow(tmp_path):
    """端到端 7 步集成：见 spec 测试覆盖节。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# v1\n\nbody', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 'test', 'created': '2026-05-28',
    }])
    # 1. dry-run → new_archived_planned，无写入
    r1 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=False)
    assert any(r['status'] == 'new_archived_planned' for r in r1['items'])
    assert list(pathlib.Path(vault).rglob('*.md')) == []

    # 2. apply → new_archived
    r2 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    new_items = [r for r in r2['items'] if r['status'] == 'new_archived']
    assert len(new_items) == 1
    vp = new_items[0]['vault_path']
    assert pathlib.Path(vp).exists()

    # 3. 改原文件 → synced
    time.sleep(1.1)
    src.write_text('# v2 changed', encoding='utf-8')
    r3 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'synced' for r in r3['items'])

    # 4. 手工改 Vault 副本 → conflict_vault_edited
    text = pathlib.Path(vp).read_text(encoding='utf-8')
    pathlib.Path(vp).write_text(text + '\n用户批注\n', encoding='utf-8')
    time.sleep(1.1)
    r4 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'conflict_vault_edited' for r in r4['items'])

    # 5. 删原文件 → original_missing；Vault 副本保留
    src.unlink()
    r5 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'original_missing' for r in r5['items'])
    assert pathlib.Path(vp).exists()

    # 6. 不变再跑 → 仍是 original_missing（src 仍缺）
    r6 = sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    assert any(r['status'] == 'original_missing' for r in r6['items'])

    # 7. worktree 场景留到独立测试


# ========== wikilink_form 字段（basename 同名消歧）==========


def test_wikilink_form_no_collision(tmp_path):
    """单一 basename → wikilink_form 直接用 basename。"""
    repo = tmp_path / 'p'; _make_repo(repo)
    src = repo / 'unique-name.md'; src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert data[0]['wikilink_form'] == '[[unique-name]]'


def test_wikilink_form_with_collision_uses_subdir(tmp_path):
    """spec + plan 同 basename → 各自带子目录消歧。"""
    repo = tmp_path / 'p'; _make_repo(repo)
    src1 = repo / 'specs' / 'shared.md'
    src1.parent.mkdir()
    src1.write_text('# spec body', encoding='utf-8')
    src2 = repo / 'plans' / 'shared.md'
    src2.parent.mkdir()
    src2.write_text('# plan body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [
        {'path': str(src1), 'type': 'spec', 'context': 't', 'created': '2026-05-28'},
        {'path': str(src2), 'type': 'plan', 'context': 't', 'created': '2026-05-28'},
    ])
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    forms = sorted(d['wikilink_form'] for d in data)
    assert forms == ['[[plans/shared|plan]]', '[[specs/shared|spec]]']


# ========== S7/S8/S9 backfill 模式专属端到端 ==========


def test_S7_backfill_mode_filters_out_already_archived(tmp_path):
    """S7: backfill 只处理无 vault_path 的条目；已归集的跳过。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    src1 = repo / 'a.md'; src1.write_text('# a', encoding='utf-8')
    src2 = repo / 'b.md'; src2.write_text('# b', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [
        {'path': str(src1), 'type': 'spec', 'context': 'a', 'created': '2026-05-28'},
        {'path': str(src2), 'type': 'spec', 'context': 'b', 'created': '2026-05-28'},
    ])
    # 第一次 backfill apply：两条都新归集
    r1 = sync(vault_root=str(vault), pending_path=pending, mode='backfill', apply=True)
    assert len(r1['new_archived']) == 2
    # 第二次 backfill：两条已有 vault_path，应跳过
    r2 = sync(vault_root=str(vault), pending_path=pending, mode='backfill', apply=True)
    assert r2['items'] == []  # backfill 过滤后无任何条目处理


def test_S8_backfill_with_missing_source(tmp_path):
    """S8: backfill 原文件已丢 → 死条目（无 vault_path+original_missing+path不存在）被 prune 清理。"""
    vault = tmp_path / 'V'; vault.mkdir()
    nonexistent = tmp_path / 'gone' / 'x.md'  # 永不创建
    pending = _write_pending(tmp_path, [
        {'path': str(nonexistent), 'type': 'spec', 'context': 'x', 'created': '2026-05-28'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending, mode='backfill', apply=True)
    # 原文件不存在 = 死条目 → 被 prune（去重后不在 items，出现在 pruned）
    assert len(result['pruned']) == 1
    assert any(r.get('path') == str(nonexistent) for r in result['pruned'])
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert data == []                                      # 条目被删


def test_S2_mtime_changed_but_hash_unchanged(tmp_path):
    """S2: mtime/size 变化但 hash 一致 → skipped_unchanged 且 mtime cache 更新。

    场景：touch 文件或重写相同内容 → mtime 变了但内容 hash 不变。
    短路失败后走 hash 四分支，应落到「分支 A：双方都没变」→ skipped_unchanged。
    """
    repo = tmp_path / 'p'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    sync(vault_root=str(vault), pending_path=pending, mode='incremental', apply=True)
    # 第一次记录的 source_mtime
    data1 = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    mtime1 = data1[0].get('source_mtime')
    # 改写相同内容：mtime / size 都会变（size 不变但 mtime 必变），但 hash 一致
    time.sleep(1.1)
    src.write_text('# body', encoding='utf-8')
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=True)
    # 应该是 skipped_unchanged（mtime 变但 hash 一致）
    statuses = [r['status'] for r in result['items']]
    assert 'skipped_unchanged' in statuses
    # mtime cache 更新到新值
    data2 = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    mtime2 = data2[0].get('source_mtime')
    assert mtime2 != mtime1
    assert mtime2 == os.path.getmtime(str(src))


def test_S10_concurrent_sync_serializes_via_lock(tmp_path):
    """S10: 两进程同时 sync 同一 pending-docs，锁串行化，文件状态不损坏。"""
    import threading
    repo = tmp_path / 'p'; _make_repo(repo)
    src = repo / 'x.md'; src.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [{
        'path': str(src), 'type': 'spec', 'context': 't', 'created': '2026-05-28',
    }])
    results = [None, None]
    def run(i):
        results[i] = sync(vault_root=str(vault), pending_path=pending,
                          mode='incremental', apply=True)
    t1 = threading.Thread(target=run, args=(0,))
    t2 = threading.Thread(target=run, args=(1,))
    t1.start(); t2.start()
    t1.join(); t2.join()
    # 至少一个 sync 成功（不应都因锁失败）
    assert not (results[0].get('error') == 'lock_timeout'
                and results[1].get('error') == 'lock_timeout')
    # pending-docs.json 状态完整（未损坏，可解析）
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert len(data) == 1
    assert data[0].get('vault_path')  # 已归集


def test_S14_original_missing_since_expired_prompt(tmp_path):
    """S14: original_missing_since 超过 90 天 → expired_missing 列表。

    阈值 ORIGINAL_MISSING_PROMPT_DAYS=90，超过则在 sync 输出 expired_missing 数组，
    SKILL.md 第四步会显式呈现给用户。
    """
    import datetime as _dt
    vault = tmp_path / 'V'; vault.mkdir()
    long_ago = (_dt.date.today() - _dt.timedelta(days=120)).isoformat()
    pending = _write_pending(tmp_path, [
        {'path': '/nonexistent/old.md', 'type': 'spec', 'context': 'x',
         'created': '2026-01-01', 'original_missing': True,
         'original_missing_since': long_ago, 'vault_path': '/V/x.md'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=True)
    assert 'expired_missing' in result
    assert len(result['expired_missing']) == 1
    assert result['expired_missing'][0]['days_missing'] >= 120
    assert result['expired_missing'][0]['path'] == '/nonexistent/old.md'


def test_S14b_original_missing_within_90_days_not_in_expired(tmp_path):
    """S14b: 失踪时长 ≤ 90 天 → 不进入 expired_missing 列表。"""
    import datetime as _dt
    vault = tmp_path / 'V'; vault.mkdir()
    recent = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    pending = _write_pending(tmp_path, [
        {'path': '/nonexistent/recent.md', 'type': 'spec', 'context': 'x',
         'created': '2026-05-01', 'original_missing': True,
         'original_missing_since': recent, 'vault_path': '/V/x.md'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=True)
    assert result['expired_missing'] == []


def test_S9_backfill_adopt_existing_vault_md(tmp_path):
    """S9: backfill 场景 basename 同名已存在 Vault 副本（无 vault_source_*）→ 走 adopt 分支。"""
    repo = tmp_path / 'p'; _make_repo(repo)
    src = repo / 'shared.md'; src.write_text('# new body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'
    target_dir.mkdir(parents=True)
    target = target_dir / 'shared.md'
    # 早期手工副本，无 vault_source_*
    target.write_text('---\ntitle: hand-archived\n---\nold body', encoding='utf-8')
    pending = _write_pending(tmp_path, [
        {'path': str(src), 'type': 'spec', 'context': 's', 'created': '2026-05-28'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending, mode='backfill', apply=True)
    assert len(result['adopted']) == 1
    # Vault 副本正文不变
    text = target.read_text(encoding='utf-8')
    assert 'old body' in text
    # frontmatter 加了 vault_source_*
    assert 'vault_source_repo' in text


# ========== 死条目 prune（含 path 实时校验）==========


def test_prune_removes_dead_entry_with_backup(tmp_path):
    """无 vault_path + original_missing + path 不存在 → apply 时被 prune；.bak 生成。"""
    vault = tmp_path / 'V'; vault.mkdir()
    nonexistent = tmp_path / 'gone' / 'x.md'  # 永不创建
    pending = _write_pending(tmp_path, [
        {'path': str(nonexistent), 'type': 'spec', 'context': 'x',
         'created': '2026-05-28', 'original_missing': True,
         'original_missing_since': '2026-05-28'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=True)
    assert len(result['pruned']) == 1
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert data == []                                      # 死条目被删
    assert pathlib.Path(pending + '.bak.1').exists()        # 已备份(轮转保留最近5份)
    # 去重：被删条目不再出现在 original_missing / items
    assert all(r.get('path') != str(nonexistent) for r in result['original_missing'])
    assert all(r.get('path') != str(nonexistent) for r in result['items'])


def test_prune_keeps_live_file_with_original_missing(tmp_path):
    """original_missing=True 但 path 现存（活文件）→ 不被 prune，重新归集。"""
    repo = tmp_path / 'proj'; _make_repo(repo)
    live = repo / 'live.md'; live.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    pending = _write_pending(tmp_path, [
        {'path': str(live), 'type': 'spec', 'context': 'x',
         'created': '2026-06-02', 'original_missing': True,
         'original_missing_since': '2026-06-02'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=True)
    assert result['pruned'] == []                          # 活文件不删
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert len(data) == 1
    assert data[0].get('vault_path')                       # 被重新归集


def test_prune_dry_run_plans_without_deleting(tmp_path):
    """dry-run 输出 pruned_planned，不实际删。"""
    vault = tmp_path / 'V'; vault.mkdir()
    nonexistent = tmp_path / 'gone' / 'x.md'
    pending = _write_pending(tmp_path, [
        {'path': str(nonexistent), 'type': 'spec', 'context': 'x',
         'created': '2026-05-28', 'original_missing': True,
         'original_missing_since': '2026-05-28'},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=False)
    assert len(result['pruned_planned']) == 1
    data = json.loads(pathlib.Path(pending).read_text(encoding='utf-8'))
    assert len(data) == 1                                  # 未删


def test_prune_dedup_expired_missing(tmp_path):
    """dry-run 下死条目(超90天)只进 pruned_planned，不双报 expired_missing。"""
    import datetime as _dt
    vault = tmp_path / 'V'; vault.mkdir()
    long_ago = (_dt.date.today() - _dt.timedelta(days=120)).isoformat()
    nonexistent = tmp_path / 'gone' / 'old.md'
    pending = _write_pending(tmp_path, [
        {'path': str(nonexistent), 'type': 'spec', 'context': 'x',
         'created': '2026-01-01', 'original_missing': True,
         'original_missing_since': long_ago},
    ])
    result = sync(vault_root=str(vault), pending_path=pending,
                  mode='incremental', apply=False)
    assert len(result['pruned_planned']) == 1
    # 已从 expired_missing 剔除（不双报）
    assert all(r.get('path') != str(nonexistent) for r in result['expired_missing'])
