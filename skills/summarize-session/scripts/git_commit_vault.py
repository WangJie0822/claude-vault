"""知识库（Vault）自动 git commit：用 git status 枚举知识库目录内变更精确 add + commit。
只 commit 不 push；失败不阻塞（返回 status=failed）。不复用文件锁（靠 skill 串行 + git index.lock）。"""
import os
import re
import json
import argparse
import subprocess
import sys
from pathlib import Path
# 注入同目录，供本包内脚本按模块名互相 import（本文件自 2026-09-07 起不再直接
# 依赖 rebuild_index，但该路径注入是本目录脚本的既有约定，保留以免影响其他入口）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 2026-09-03：由「顶层目录白名单」改为「排除法」。
# 原白名单是 ('工作日志','Claude Code','项目笔记','缺陷全链路','技术笔记','偏好与习惯','参考资料','领域')，
# 实测漏掉某用户 Vault 里真实存在的 4 个顶层目录（Windows 系统 / 改进计划 / plans / specs）——
# 这些目录下只有系统索引能靠 is_system_index 放行，**普通笔记的新增与修改被静默漏提交**：
# 该 Vault 有 6 篇笔记长期滞留，其中 4 篇从未入过 git，且都被用户的 CLAUDE.local.md 直接引用
# （不入 git 则换机即丢）。白名单法每新增一个顶层目录就会重蹈覆辙，且失败是静默的，故反转判据。
#
# 排除的是工具/元数据目录（.obsidian / .meta / .trash / .git …），判据用「顶层段以 . 开头」
# 一次覆盖，不再逐个枚举——枚举法正是上面那个缺陷的成因。


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
    """限知识库目录内的 .md：除工具/元数据目录（顶层段以 . 开头）外一律纳入。
    越界防护（finding G）：拒绝 .. 与绝对路径。

    2026-09-03 由白名单改为排除法，理由见模块顶部注释——白名单漏掉真实存在的顶层目录，
    且漏掉时**没有任何信号**，笔记会一直滞留在工作区。
    """
    if not path.endswith('.md'):
        return False
    norm = path.replace('\\', '/')
    # L2：规范化后拒绝任意位置的 .. 段（非仅前缀）+ 绝对路径
    # ⚠️ 必须额外显式拒绝以 / 开头的路径：Windows 的 ntpath.isabs('/abs/x') 返回 **False**
    #    （无盘符不算绝对）。白名单时代这类路径靠「top 是空串、空串不在白名单里」被意外挡住；
    #    判据反转为排除法后空串不以 . 开头，会被放行。2026-09-03 由既有的
    #    test_is_knowledge_md_path_traversal_still_blocked 当场抓到。
    if os.path.isabs(path) or norm.startswith('/') or '..' in norm.split('/'):
        return False
    if norm == 'CLAUDE.md':
        return True
    # 工具与元数据目录：.obsidian / .meta / .trash / .git 等一律排除。
    # 根目录下的隐藏 .md（top 即文件名）同样被这条挡住。
    top = norm.split('/', 1)[0]
    # 空 top（形如 '/x.md'）已被上面的 startswith('/') 挡下，这里是第二道：
    # 判据反转后「未知形态默认放行」，任何拿不准的 top 都必须显式拒绝而非落到 return True。
    if not top or top.startswith('.'):
        return False
    # ⚠️ 2026-09-07：上面这道排除原先位于一个 `if is_system_index(norm): return True`
    #    **之后**。is_system_index 的规则是「文件名前缀 == 父目录名」，而工具目录下
    #    同样能构造出该形态（`.obsidian/.obsidian 索引.md`、`.meta/.meta 索引.md`），
    #    于是这批路径抢先被放行、绕过了点目录排除——同批新增的
    #    test_tool_and_metadata_dirs_are_excluded 用的样本全是**非**索引形态，正好
    #    测不到这个缺口。
    #    判据反转为排除法之后，该分支已无独立作用：非点目录下的 .md 本就一律放行，
    #    它只剩下这个抢先放行的副作用，故整体移除。
    #    （is_system_index 仍是「是不是系统索引」的唯一判据，只是这里不再需要问它。）
    return True


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
