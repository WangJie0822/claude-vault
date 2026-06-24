"""Vault 目标路径解析：git common-dir + worktree 探测 + Glob 命中 + 启发式 fallback。"""
import os
import subprocess
import pathlib
from typing import Optional


def resolve_project_root(path: str) -> Optional[str]:
    """从原文件 path 推断主仓库 toplevel。

    - 在 worktree 内 → 返回主仓库 toplevel（用 --git-common-dir 探测）
    - 在普通 git 仓库内 → 返回 toplevel
    - 非 git → 返回 None"""
    if not path:
        return None
    p = pathlib.Path(path)
    dirname = str(p.parent if p.is_file() else p)
    try:
        proc = subprocess.run(
            ['git', '-C', dirname, 'rev-parse', '--git-common-dir'],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    common_dir = pathlib.Path(proc.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (pathlib.Path(dirname) / common_dir).resolve()
    # --git-common-dir 总是指向主仓库 .git 目录
    if common_dir.name == '.git':
        return str(common_dir.parent)
    return None


def is_inside_vault(path: str, vault: str) -> bool:
    """path 是否位于 vault 目录内（大小写不敏感 + 路径分隔符归一化）。"""
    if not path or not vault:
        return False
    try:
        rp_str = str(pathlib.Path(path).resolve()).replace('\\', '/').lower()
        rv_str = str(pathlib.Path(vault).resolve()).replace('\\', '/').lower()
        return rp_str.startswith(rv_str + '/') or rp_str == rv_str
    except OSError:
        return False


def _glob_first_match(vault: str, basename: str) -> Optional[str]:
    """Glob $VAULT/*/<basename>/ 找命中的现有目录，返回 <vault>/<X>/<basename>。"""
    vp = pathlib.Path(vault)
    if not vp.exists():
        return None
    for category_dir in vp.iterdir():
        if not category_dir.is_dir() or category_dir.name.startswith('.'):
            continue
        candidate = category_dir / basename
        if candidate.is_dir():
            return str(candidate)
    return None


def _subdir_for_type(doc_type: str) -> str:
    if doc_type == 'spec':
        return 'specs'
    if doc_type == 'plan':
        return 'plans'
    return ''


def resolve_vault_target(vault: str, project_basename: str, doc_type: str,
                         original_path: Optional[str] = None) -> str:
    """返回 Vault 内目标文件夹路径（不含文件名）。

    决策顺序：
    1. Glob $VAULT/*/<project_basename>/ 命中 → 用现有目录
    2. original_path 含 ~/.claude/ 或 .claude/worktrees/ → Claude Code/
    3. original_path 含 /skills/<name>/ → 缺陷全链路/<name>/
    4. 默认 → 项目笔记/<project_basename>/
    最终在末尾追加 specs/ 或 plans/ 子目录"""
    matched = _glob_first_match(vault, project_basename)
    if matched:
        base = matched
    elif original_path and ('/.claude/' in original_path.replace('\\', '/') or
                            '.claude/worktrees/' in original_path.replace('\\', '/')):
        base = str(pathlib.Path(vault) / 'Claude Code')
    elif original_path and '/skills/' in original_path.replace('\\', '/'):
        norm = original_path.replace('\\', '/')
        idx = norm.find('/skills/')
        rest = norm[idx + len('/skills/'):]
        skill_name = rest.split('/', 1)[0]
        base = str(pathlib.Path(vault) / '缺陷全链路' / skill_name)
    else:
        base = str(pathlib.Path(vault) / '项目笔记' / project_basename)
    sub = _subdir_for_type(doc_type)
    return str(pathlib.Path(base) / sub) if sub else base
