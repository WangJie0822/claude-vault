"""单文件归集：path → Vault 副本，atomic rename + frontmatter 合并 + hash 记录。

返回 dict 含 status / vault_path / source_content_hash / vault_content_hash / reason。

状态分支：
- new_archived              新副本写入 Vault
- adopted                   早期手工副本被认领（无 vault_source_*）
- path_invalid              path 非绝对 / 含 ~ / 含 $
- denied_sensitive          命中敏感 deny-list（路径 glob 或内容启发式）
- in_vault_short_circuit    path 本身就在 Vault 内
- conflict                  目标存在 + 源不匹配 + adopt 关闭
- conflict_existing_self    目标存在 + 源匹配（sync 层决定如何处理）
- symlink_blocked           目标路径是 symlink
- error                     其他 I/O / frontmatter 错误
"""
import os
import re
import sys
import json
import hashlib
import shutil
import tempfile
import pathlib
import datetime
from typing import Optional

# scripts/ 同级模块导入（apply_migration.py:12 等同款风格）
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _frontmatter import upsert_fields
from _path_resolver import (
    resolve_project_root,
    resolve_vault_target,
    is_inside_vault,
)
from _sensitive_patterns import is_sensitive_path, is_sensitive_content

# UTF-8 BOM (﻿)
_BOM = '﻿'


def _sha256_file(path: str) -> str:
    """对文件按 64KB 分块计算 sha256。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def _sha256_str(content: bytes) -> str:
    """对 bytes 计算 sha256。"""
    return 'sha256:' + hashlib.sha256(content).hexdigest()


def _split_frontmatter_and_body(text: str) -> tuple:
    """剥离 frontmatter，返回 (frontmatter_block, body)。

    - 文件以 '---\\n' 开头，找下一个 '---\\n' 行 → frontmatter = 这两行之间含 fence + 后换行
    - 没有 frontmatter → ('', 全文)
    - frontmatter 未闭合 → ('', 全文)（当无 frontmatter 处理）
    """
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        return '', text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return '', text  # 未闭合，当无 frontmatter 处理
    fm_block = '\n'.join(lines[:end + 1]) + '\n'
    body = '\n'.join(lines[end + 1:])
    return fm_block, body


def _sha256_body(file_path: str) -> str:
    """正文（剥离 frontmatter 后）的 sha256。

    用于 vault_content_hash：frontmatter 是脚本权威字段，每次同步可能改写，不应参与 hash；
    正文是真正"内容"，用户手工编辑会改变；脚本同步用户不编辑时正文不变。
    """
    text = pathlib.Path(file_path).read_text(encoding='utf-8')
    _, body = _split_frontmatter_and_body(text)
    return 'sha256:' + hashlib.sha256(body.encode('utf-8')).hexdigest()


def _strip_bom(text: str) -> str:
    """去掉前导 BOM（若存在）。"""
    return text.lstrip(_BOM)


def _is_absolute(path: str) -> bool:
    """判定路径是否为绝对路径（兼容 POSIX 与 Windows 盘符）。"""
    if not path:
        return False
    if path.startswith('/') or path.startswith('\\'):
        return True
    # Windows 盘符 X:
    if len(path) >= 2 and path[1] == ':' and path[0].isalpha():
        return True
    return False


def _repo_relative_path(src_path: str, repo_root: Optional[str]) -> str:
    """返回 src 相对于 repo_root 的相对路径（/ 分隔）；无 repo_root 时退回 basename。"""
    if not repo_root:
        return os.path.basename(src_path)
    try:
        rel = pathlib.Path(src_path).resolve().relative_to(
            pathlib.Path(repo_root).resolve())
        return str(rel).replace('\\', '/')
    except ValueError:
        return os.path.basename(src_path)


def _atomic_write(target: str, content: bytes) -> None:
    """tempfile + os.replace 原子写入：写完 tmp 才 rename 覆盖 target。"""
    target_p = pathlib.Path(target)
    target_p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix='.archive_doc_', suffix='.tmp',
        dir=str(target_p.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(tmp, target)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _check_frontmatter_closed(text: str) -> bool:
    """若以 '---' 开头但未闭合 → 返回 False。无 frontmatter 算作合法。"""
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        return True
    # 寻找第二个 '---'
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            return True
    return False


def archive_doc(entry: dict, vault_root: str,
                allow_adopt: bool = True,
                rename_on_conflict: bool = False) -> dict:
    """归集单个 pending-docs 条目到 Vault。

    参数：
    - entry: pending-docs 条目，dict 含 path / type / context / created
    - vault_root: Vault 根目录绝对路径
    - allow_adopt: 目标存在但无 vault_source_* 时是否走 adopt 分支
    - rename_on_conflict: 冲突时是否自动 rename 为 stem-YYYYMMDD-HHMMSS.suffix

    返回 dict 含 status（见模块 docstring）"""
    src = entry.get('path', '')

    # 0. 前置校验：绝对路径 + 无 ~ / $
    if not _is_absolute(src) or '~' in src or '$' in src:
        return {'status': 'path_invalid', 'reason': 'non-absolute or env var',
                'path': src}

    # 路径敏感性
    if is_sensitive_path(src):
        return {'status': 'denied_sensitive', 'reason': 'sensitive path glob',
                'path': src}

    # path 已经在 Vault 内 → short-circuit
    if is_inside_vault(src, vault_root):
        return {
            'status': 'in_vault_short_circuit',
            'vault_path': src,
            'source_content_hash': _sha256_file(src) if os.path.exists(src) else None,
            'reason': 'path already inside vault',
        }

    if not os.path.exists(src):
        return {'status': 'original_missing',
                'reason': 'source file not exist',
                'path': src}

    # 内容启发式检查（仅对非 .md 文件做前 8KB 扫描）
    try:
        if not src.endswith('.md'):
            with open(src, 'rb') as f:
                raw = f.read(8192)
            head = raw.decode('utf-8', errors='ignore')
            if is_sensitive_content(head):
                return {'status': 'denied_sensitive',
                        'reason': 'sensitive content heuristic', 'path': src}
    except OSError as e:
        return {'status': 'error', 'reason': f'read source error: {e}',
                'path': src}

    # 0.5 frontmatter 未闭合 fail-fast
    try:
        src_text_preview = pathlib.Path(src).read_text(encoding='utf-8',
                                                       errors='ignore')
    except OSError as e:
        return {'status': 'error', 'reason': f'read source error: {e}',
                'path': src}
    if not _check_frontmatter_closed(_strip_bom(src_text_preview)):
        return {'status': 'error',
                'reason': 'source frontmatter not closed',
                'path': src}

    # 1. 推断主仓库 toplevel
    repo_root = resolve_project_root(src)
    project_basename = (os.path.basename(repo_root) if repo_root
                        else pathlib.Path(src).parent.name)

    # 2. Vault 目标目录
    doc_type = entry.get('type', 'other')
    vault_target_dir = resolve_vault_target(
        vault_root, project_basename, doc_type, original_path=src)

    # 3. 文件名（保持原 basename）
    fname = os.path.basename(src)
    vault_path = str(pathlib.Path(vault_target_dir) / fname)

    # 4. symlink 防护：检查目标本身或其父目录是否是 symlink
    if os.path.islink(vault_path):
        return {'status': 'symlink_blocked',
                'reason': 'vault target is a symlink', 'path': src,
                'vault_path': vault_path}

    # 5. 计算 source hash
    try:
        src_hash = _sha256_file(src)
    except OSError as e:
        return {'status': 'error', 'reason': f'hash source error: {e}',
                'path': src}

    # 6. 同名冲突决策
    if os.path.exists(vault_path):
        existing = pathlib.Path(vault_path).read_text(
            encoding='utf-8', errors='ignore')
        fm_match = re.match(r'^---\n(.*?)\n---\n', existing, re.DOTALL)
        existing_repo = None
        existing_relpath = None
        if fm_match:
            fm_text = fm_match.group(1)
            for line in fm_text.splitlines():
                if line.startswith('vault_source_repo:'):
                    val = line.split(':', 1)[1].strip()
                    # 剥离 _yaml_scalar 加的两侧双引号
                    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                        val = val[1:-1]
                    existing_repo = val
                if line.startswith('vault_source_path:'):
                    val = line.split(':', 1)[1].strip()
                    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                        val = val[1:-1]
                    existing_relpath = val

        repo_rel = _repo_relative_path(src, repo_root)
        is_self = (existing_repo == project_basename and
                   existing_relpath == repo_rel)

        if existing_repo is None and allow_adopt:
            # adopt 分支：早期手工归集副本 + 无 vault_source_* 字段
            return _do_adopt(
                src=src, vault_path=vault_path, src_hash=src_hash,
                repo_basename=project_basename, repo_rel=repo_rel,
                entry=entry)

        if is_self:
            # 走"副本正文手工编辑检测"——由 sync_pending_docs.py 调用方处理
            return {'status': 'conflict_existing_self',
                    'vault_path': vault_path,
                    'source_content_hash': src_hash,
                    'reason': 'existing vault copy matches; sync logic decides'}

        if rename_on_conflict:
            stem = pathlib.Path(fname).stem
            suffix = pathlib.Path(fname).suffix
            ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            fname2 = f'{stem}-{ts}{suffix}'
            vault_path = str(pathlib.Path(vault_target_dir) / fname2)
        else:
            return {'status': 'conflict',
                    'reason': 'vault path exists with different source',
                    'path': src, 'vault_path': vault_path}

    # 7. 复制原文件正文到 Vault（atomic rename）
    try:
        with open(src, 'rb') as f:
            src_bytes = f.read()
        src_text = _strip_bom(src_bytes.decode('utf-8'))
        _atomic_write(vault_path, src_text.encode('utf-8'))
    except (OSError, UnicodeDecodeError) as e:
        return {'status': 'error', 'reason': f'write vault error: {e}',
                'path': src}

    # 8. 预先计算 vault_content_hash（正文 sha256，剥离 frontmatter）
    # 此时 vault_path 还没有任何 vault_* frontmatter，正文等于源文件正文
    # frontmatter 改写不影响 body hash → stored hash 永远等于 body hash
    try:
        vault_hash = _sha256_body(vault_path)
    except OSError as e:
        return {'status': 'error',
                'reason': f'hash vault body error: {e}',
                'path': src, 'vault_path': vault_path}

    # 9. 合并 vault_* + category/subcategory/tags/summary/created/vault_content_hash
    # 单次 upsert_fields 写入，避免「先写其他字段 → 再写 vault_content_hash」时序错位
    repo_rel = _repo_relative_path(src, repo_root)
    today_iso = datetime.date.today().isoformat()
    fm_updates = {
        'vault_source_repo': project_basename,
        'vault_source_path': repo_rel,
        'vault_source_hash': src_hash,
        'vault_content_hash': vault_hash,
        'vault_archived_at': today_iso,
    }
    # 补齐缺失 category / subcategory / tags / summary
    # category 推断：从 vault_path 反推
    try:
        rel_to_vault = pathlib.Path(vault_path).resolve().relative_to(
            pathlib.Path(vault_root).resolve())
        parts = rel_to_vault.parts
        if len(parts) >= 1:
            fm_updates['category'] = parts[0]
        # 文件路径形如：category/subcategory/.../file → 第 2 段视为 subcategory
        if len(parts) >= 3:
            fm_updates['subcategory'] = parts[1]
    except ValueError:
        # vault_path 解析失败时不强制补 category/subcategory
        pass

    # tags：YAML inline 风格 [doc_type, archived]，由 _yaml_scalar 处理
    fm_updates['tags'] = [doc_type, 'archived']

    # summary（截 200 字）
    summary = entry.get('context', '')[:200]
    if summary:
        fm_updates['summary'] = summary

    # created
    fm_updates['created'] = today_iso

    try:
        upsert_fields(pathlib.Path(vault_path), fm_updates)
    except ValueError as e:
        # frontmatter 未闭合等异常
        return {'status': 'error',
                'reason': f'upsert frontmatter error: {e}',
                'path': src, 'vault_path': vault_path}

    return {
        'status': 'new_archived',
        'vault_path': vault_path,
        'source_content_hash': src_hash,
        'vault_content_hash': vault_hash,
        'source_mtime': os.path.getmtime(src),
        'source_size': os.path.getsize(src),
    }


def _do_adopt(src, vault_path, src_hash, repo_basename, repo_rel, entry):
    """adopt 早期手工归集副本：仅 upsert frontmatter，不动正文。

    vault_content_hash 用「正文（剥离 frontmatter 后）的 sha256」：
    - 此处 vault 副本已有 frontmatter，_sha256_body 会剥离它，body 就是 adopt 副本的原始正文
    - 这正是设计意图：body 内容定义"用户视为副本的内容"
    """
    today_iso = datetime.date.today().isoformat()
    try:
        vault_hash = _sha256_body(vault_path)
    except OSError as e:
        return {'status': 'error',
                'reason': f'hash vault body error: {e}',
                'path': src, 'vault_path': vault_path}
    try:
        upsert_fields(pathlib.Path(vault_path), {
            'vault_source_repo': repo_basename,
            'vault_source_path': repo_rel,
            'vault_source_hash': src_hash,
            'vault_content_hash': vault_hash,
            'vault_archived_at': today_iso,
            'adopted_from_existing': 'true',
        })
    except ValueError as e:
        return {'status': 'error',
                'reason': f'adopt upsert error: {e}',
                'path': src, 'vault_path': vault_path}
    return {
        'status': 'adopted',
        'vault_path': vault_path,
        'source_content_hash': src_hash,
        'vault_content_hash': vault_hash,
        'source_mtime': os.path.getmtime(src),
        'source_size': os.path.getsize(src),
    }
