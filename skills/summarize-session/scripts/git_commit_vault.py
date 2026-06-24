"""知识库（Vault）自动 git commit：用 git status 枚举知识库目录内变更精确 add + commit。
只 commit 不 push；失败不阻塞（返回 status=failed）。不复用文件锁（靠 skill 串行 + git index.lock）。"""
import os
import re
import json
import argparse
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rebuild_index import is_system_index

# 知识库目录白名单（相对 vault 顶层目录名）；CLAUDE.md 与系统生成索引(is_system_index,含根/category/遗留)额外放行
INCLUDE_TOP_DIRS = ('工作日志', 'Claude Code', '项目笔记', '缺陷全链路',
                    '技术笔记', '偏好与习惯', '参考资料', '领域')


def _git(vault, args):
    # encoding='utf-8'：git 输出 UTF-8，Windows subprocess text=True 默认用 cp936(GBK)
    # 解码中文路径会崩 UnicodeDecodeError；errors='replace' 兜底任何非 UTF-8 字节。
    return subprocess.run(['git', '-C', vault] + args, capture_output=True,
                          text=True, encoding='utf-8', errors='replace')


def is_git_repo(vault):
    r = _git(vault, ['rev-parse', '--is-inside-work-tree'])
    return r.returncode == 0 and r.stdout.strip() == 'true'


def sanitize_title(title, maxlen=60):
    """去换行/回车/控制字符 + 压缩空白 + 截断，防 commit message 注入/格式破坏（finding C）。"""
    t = re.sub(r'[\x00-\x1f\x7f]', ' ', title or '')
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:maxlen]


def _is_knowledge_md(path):
    """限知识库目录内的 .md：白名单顶层目录 / CLAUDE.md / 系统生成索引(is_system_index 精确规则)。
    越界防护（finding G）：拒绝 .. 与绝对路径。"""
    if not path.endswith('.md'):
        return False
    norm = path.replace('\\', '/')
    # L2：规范化后拒绝任意位置的 .. 段（非仅前缀）+ 绝对路径
    if os.path.isabs(path) or '..' in norm.split('/'):
        return False
    if norm == 'CLAUDE.md':
        return True
    # 系统生成的索引文件(精确规则,含根/category/遗留)一律放行,
    # 不限 INCLUDE_TOP_DIRS(改进计划/Windows 系统 不在白名单,只能靠此放行)
    if is_system_index(norm):
        return True
    top = norm.split('/', 1)[0]
    return top in INCLUDE_TOP_DIRS


def enumerate_changes(vault):
    """git status --porcelain -z 枚举知识库目录内变更/未跟踪 .md（-z 防文件名空格/特殊字符）。"""
    r = _git(vault, ['status', '--porcelain', '-z', '-u'])
    if r.returncode != 0:
        return []
    entries = r.stdout.split('\0')
    out, i = [], 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        status, path = e[:2], e[3:]
        if status and status[0] in ('R', 'C'):  # rename/copy：下一项是旧/源路径
            old = entries[i + 1] if i + 1 < len(entries) else ''
            # M2：rename 旧路径的删除也需 stage（否则 commit 记"凭空新增"+ 旧文件删除遗留）
            if status[0] == 'R' and _is_knowledge_md(old):
                out.append(old)
            i += 2
        else:
            i += 1
        if _is_knowledge_md(path):
            out.append(path)
    return out


def _count_untracked(vault):
    # -z：中文路径不被 git quotepath octal 转义（仅计数，引号不影响，但与解析口径统一）
    r = _git(vault, ['status', '--porcelain', '-z', '-u'])
    if r.returncode != 0:
        return 0
    return sum(1 for e in r.stdout.split('\0') if e.startswith('??'))


def baseline_preview(vault):
    """--baseline 真跑前的 dry-run：列出 git add -A 将纳入的完整清单（供 skill AskUserQuestion 确认）。"""
    if not is_git_repo(vault):
        return {'status': 'skipped', 'reason': 'not_git'}
    # L1：-z 解析，中文路径不被 octal 转义（否则用户确认清单乱码；Vault 顶层目录全中文）
    r = _git(vault, ['status', '--porcelain', '-z', '-u'])
    files, entries, i = [], r.stdout.split('\0'), 0
    while i < len(entries):
        e = entries[i]
        if not e:
            i += 1
            continue
        status, path = e[:2], e[3:]
        if status and status[0] in ('R', 'C'):  # rename/copy：跳过来源路径项
            i += 2
        else:
            i += 1
        files.append(path)
    return {'status': 'preview', 'files': files}


def commit_vault(vault, title, no_commit=False, baseline=False):
    if no_commit:
        return {'status': 'skipped', 'reason': 'no_commit_flag'}
    if not is_git_repo(vault):
        return {'status': 'skipped', 'reason': 'not_git'}
    msg = '[docs|vault|会话总结][公共]' + (sanitize_title(title) or '知识库更新')
    if baseline:
        add = _git(vault, ['add', '-A'])  # 全量（已由 skill 经 preview+确认放行）
        files = -1
    else:
        changes = enumerate_changes(vault)
        if not changes:
            return {'status': 'nothing', 'reason': 'no_vault_changes'}
        add = _git(vault, ['add', '--'] + changes)  # argv 形式，已过滤越界
        files = len(changes)
    if add.returncode != 0:
        return {'status': 'failed', 'reason': 'git_add: ' + add.stderr.strip()}
    staged = _git(vault, ['diff', '--cached', '--name-only'])
    if not staged.stdout.strip():
        return {'status': 'nothing', 'reason': 'nothing_staged'}
    c = _git(vault, ['commit', '-m', msg])  # argv 形式，禁 shell 拼接（finding C）
    if c.returncode != 0:
        return {'status': 'failed', 'reason': 'git_commit: ' + c.stderr.strip()}
    out = {'status': 'committed', 'message': msg, 'files': files}
    untracked = _count_untracked(vault)
    if untracked > 20:
        out['baseline_suggested'] = True
        out['untracked_count'] = untracked
    return out


def main():
    ap = argparse.ArgumentParser(description='Vault 自动 git commit（只 commit 不 push）')
    ap.add_argument('--vault', required=True)
    ap.add_argument('--title', default='')
    ap.add_argument('--no-commit', action='store_true')
    ap.add_argument('--baseline', action='store_true', help='全量基线 commit（须先 --baseline-preview 经用户确认）')
    ap.add_argument('--baseline-preview', action='store_true', help='列出 baseline 将纳入文件，不提交')
    a = ap.parse_args()
    if a.baseline_preview:
        res = baseline_preview(a.vault)
    else:
        res = commit_vault(a.vault, a.title, no_commit=a.no_commit, baseline=a.baseline)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
