"""vault-loader config 收敛脚本（spec §8.2）：清理存量 config.json 的「首跑物化残留」。

背景：Task 6（commit 42f3fc8）停止了首跑全量物化 DEFAULT_CONFIG——新装用户只写最小占位，
默认值演进即时生效。但存量用户盘上的 config.json 可能是旧版本首跑物化的全量默认值快照；
其中若某键盘上值恰好等于该键**历史上任一版本**的默认值（而非用户显式改过），deep-merge
会把它当"用户覆盖"永久锁死，未来该键的默认值演进对这个用户静默失效。

本脚本是**手动执行的收敛工具**，不在任何 hook 里自动调用（S-4/R11 处置：hook 内自动
删写 config 有 symlink/并发/超时撕裂三重破坏面，故真正的删除动作必须是用户/维护者
显式触发的一次性操作）：

    python migrate_config.py                  # dry-run（默认）：只读扫描并打印将删除的键
    python migrate_config.py --apply          # 真正执行：备份 + 原子写回清理后的 config
    python migrate_config.py --restore <备份文件路径>   # 从备份还原 config.json（同样先备份当前值）

安全约束（对应安全评审 S-4/S-5/R11 逐条落实）：
    - 仅 `_config_history.ALLOWLIST_PREFIXES` 四段前缀下的非布尔数值叶子键可能被判定为
      残留；`_config_history.EXCLUDED_KEYS` 命中的 leaf 名任何情况下不删
    - 默认 dry-run，只读不落盘；只有显式 `--apply` 才会写文件
    - `--apply` / `--restore` 前若目标路径**任意一段**经符号链接 / NTFS junction 重定向
      （全路径 `os.path.realpath` 比对，非 `Path.is_symlink()` 的末段判断），整体放弃、
      不做任何改动（防止通过链接越权写到 config.json 之外的文件）；确属自己的 dotfiles
      软链布局时可加 `--force` 显式放行
    - **任何**覆盖写之前都先把被覆盖的内容完整备份到独立目录
      `~/.claude/projects/vault-loader-backups/config-<UTC 时间戳>.json`
      （与 config.json 物理隔离，且不落在插件仓库或任何项目工作树内）——`--apply` 与
      `--restore` 同等适用：`--restore` 若备份不出去就拒绝还原，`--apply` 之后用户手工
      做的调参因此永远有恢复路径。
      ⚠️ 此处**不保证**「不在任何 git 仓内」——把 `~/.claude` 整体纳入版本管理是常见做法，
      那样备份目录就落在该仓库里，是否被跟踪完全取决于它自己的 `.gitignore`。备份内容是
      用户 config 全文（含 `vault_path` 等本机绝对路径），推送到公开仓库前请自行确认
    - `--restore` 的源文件须通过顶层键白名单校验（DEFAULT_CONFIG 的键 ∪ 内部标记键），
      避免「任意 JSON → 任意路径写」；不合规时拒绝，可用 `--force` 显式放行
    - `--apply` 与 `--restore` 语义互斥，由 argparse 互斥组直接报 usage 错误
    - 所有写盘（备份文件本身、清理后的 config、`--restore` 目标）一律 tempfile +
      `os.replace` 原子替换，不会留下半写文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

# 确保能 import 同级模块（direct `python migrate_config.py` 调用时 scripts/ 本身
# 不在 sys.path 上，需先把父目录 skills/vault-loader/ 插入，与 session_start_load.py/
# prompt_submit_load.py 同一约定）；pytest 场景下已由 rootdir 提供，insert 是幂等的。
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts._config_history import ALLOWLIST_PREFIXES, EXCLUDED_KEYS, HISTORICAL_DEFAULTS
from scripts._config_loader import DEFAULT_CONFIG, _CONFIG_META_KEYS

# `--restore` 源文件的顶层键白名单：只认 vault-loader 自己的 schema（DEFAULT_CONFIG 顶层键
# ∪ 内部标记键）。防止把任意 JSON 当备份还原到任意路径。
ALLOWED_TOP_LEVEL_KEYS = frozenset(DEFAULT_CONFIG) | frozenset(_CONFIG_META_KEYS)


# ── 路径解析 ──────────────────────────────────────────────────────────────

def default_config_path() -> Path:
    """未显式传 --path 时的默认 config.json 路径（与 _config_loader.load_config 一致）。"""
    return Path.home() / ".claude" / "skills" / "vault-loader" / "config.json"


def backup_dir_path() -> Path:
    """备份目录：与 config.json 物理隔离（防 S-6 逃逸 gitignore），不落在插件仓库/项目工作树内。

    ⚠️ 不保证「不在任何 git 仓内」：用户若把 `~/.claude` 纳入版本管理，本目录就在那个仓库里，
    是否被跟踪取决于该仓库自己的 `.gitignore`——本函数无从控制，也不做检测。
    """
    return Path.home() / ".claude" / "projects" / "vault-loader-backups"


# ── allowlist 数值叶子键遍历 ──────────────────────────────────────────────

def iter_leaf_segments(
    d: dict, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    """深度优先遍历 dict，yield (**真实 key 分段元组**, 值)——仅数值叶子（int/float，
    显式排除 bool，因为 bool 是 int 子类）。dict 递归下钻；list/str/None 等其它类型
    不是"数值调参键"，直接跳过（这类值的残留判定语义不同，本脚本不处理）。

    路径必须以「分段元组」而非点分字符串在内部流转：字面 key 里本身含点时
    （`{"scoring.prompt_keyword_hit": 3}`，用户笔误或旧工具产物）它与真嵌套路径
    `{"scoring": {"prompt_keyword_hit": 3}}` 会产生**同一个**点分字符串，按 `split(".")`
    下钻只能删到嵌套那份 → 打印"已删除"但盘上原封不动（谎报）。分段元组天然区分二者。
    """
    for key, value in d.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            yield from iter_leaf_segments(value, path)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            yield path, value


def iter_numeric_leaves(d: dict, prefix: str = "") -> Iterator[tuple[str, object]]:
    """`iter_leaf_segments` 的点分字符串视图——供「查 HISTORICAL_DEFAULTS 表」与
    「展示给用户」这两类天然按字符串索引的场景使用（DEFAULT_CONFIG 自身不含字面
    点键，对它调用无歧义）。删除动作一律走 `iter_leaf_segments` 的分段元组。
    """
    for segments, value in iter_leaf_segments(d):
        path = ".".join(segments)
        yield (f"{prefix}.{path}" if prefix else path), value


def format_path(segments: Sequence[str]) -> str:
    """展示用路径：默认点分；若任一分段自身含点（字面 key 含点，非嵌套），显式标注，
    避免用户把它误读成嵌套路径（两者展示形态相同，但删除位置不同）。
    """
    dotted = ".".join(segments)
    if any("." in seg for seg in segments):
        return f"{dotted}（字面键名含点，非嵌套路径）"
    return dotted


def is_allowlisted(path: str | Sequence[str]) -> bool:
    """path 是否落在允许收敛的范围内：起手于 ALLOWLIST_PREFIXES 任一前缀，且路径上
    每一段都不命中 EXCLUDED_KEYS（EXCLUDED_KEYS 按 leaf 名在任意深度匹配即排除，
    覆盖 session_start.enabled/user_prompt_submit.enabled 这类嵌套开关键）。

    接受点分字符串或分段序列；EXCLUDED_KEYS 判定一律在**点分展开后**逐段进行——
    字面点键 `{"session_start.enabled": 1}` 也因此被正确排除（比按元组分段更严）。
    """
    dotted = path if isinstance(path, str) else ".".join(path)
    if not dotted.startswith(ALLOWLIST_PREFIXES):
        return False
    return not any(seg in EXCLUDED_KEYS for seg in dotted.split("."))


def find_residue(raw: dict) -> list[tuple[tuple[str, ...], object]]:
    """在盘上原始（未经 deep-merge）config 里找出「物化残留」候选：allowlist 内、
    值落在该键历史默认值集合中的数值叶子。未登记进 HISTORICAL_DEFAULTS 的键一律
    跳过（不删未知键——宁可漏删，不可误删）。返回 (分段元组, 值) 列表。
    """
    residue: list[tuple[tuple[str, ...], object]] = []
    for segments, value in iter_leaf_segments(raw):
        if not is_allowlisted(segments):
            continue
        history = HISTORICAL_DEFAULTS.get(".".join(segments))
        if history and value in history:
            residue.append((segments, value))
    return residue


def remove_path(d: dict, segments: Sequence[str]) -> bool:
    """按分段序列从嵌套 dict 中删除叶子键（原地修改）。**返回是否真的删掉了**——
    调用方只对返回 True 的路径打印"已删除"，杜绝"没删到却报成功"（旧实现
    `node.pop(seg, None)` 的 None 默认值把没删到吞成静默成功）。
    """
    if not segments:
        return False
    node = d
    for seg in segments[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            return False
        node = nxt
    leaf = segments[-1]
    if leaf in node:
        node.pop(leaf)
        return True
    return False


# ── 文件 I/O：读原始 config / 原子写 / 备份 ───────────────────────────────

def is_path_redirected(path: Path) -> bool:
    """路径（含每一层父目录）是否经符号链接 / NTFS junction / mount point 重定向。

    不能用 `Path.is_symlink()`：它①只查**末段**②不识别 NTFS junction（reparse point）——
    在路径前缀里放一个目录 symlink 或 junction 即可完全绕过越权写守卫。改为整条路径
    解析后比对：`os.path.realpath` 会展开路径上任意一层的链接/junction，与规范化后的
    原路径不等即说明存在重定向。判定本身失败时保守返回 True（宁可放弃，不可误写）。
    """
    try:
        target = os.path.abspath(str(path))
        real = os.path.realpath(target)
    except (OSError, ValueError):
        return True
    return os.path.normcase(real) != os.path.normcase(target)


def _abort_if_redirected(config_path: Path, force: bool) -> bool:
    """重定向守卫的统一出口。返回 True 表示调用方应整体放弃。"""
    if not is_path_redirected(config_path):
        return False
    try:
        real = os.path.realpath(os.path.abspath(str(config_path)))
    except (OSError, ValueError):
        real = "<无法解析>"
    if force:
        print(f"[migrate_config] 警告：{config_path} 经符号链接/junction 实际指向 {real}，"
              f"已指定 --force，继续写入", file=sys.stderr)
        return False
    print(f"[migrate_config] {config_path} 经符号链接/junction 实际指向 {real}，"
          f"为防越权写入整体放弃（确属自己的软链布局可加 --force）", file=sys.stderr)
    return True


def unknown_top_level_keys(data: dict) -> list[str]:
    """返回 data 里不属于 vault-loader config schema 的顶层键（`--restore` 白名单校验）。

    键名含点时按**首段**判定：`--apply` 的备份会原样保留用户笔误写下的字面点键
    （如 `"scoring.prompt_keyword_hit"`），若按整键比对会把本工具自己产出的备份挡在
    门外、堵死唯一的撤销路径；而首段仍须是合法配置段，任意 JSON 依旧被拒。
    """
    return sorted(
        str(k) for k in data
        if not isinstance(k, str) or k.split(".", 1)[0] not in ALLOWED_TOP_LEVEL_KEYS
    )


def load_raw_config(path: Path) -> dict:
    """读盘上原始 JSON（不经 DEFAULT_CONFIG 合并）。root 非 object 视为损坏。

    用 `utf-8-sig` 而非 `utf-8`：PowerShell 5.1 的 `Out-File -Encoding utf8` 与多个编辑器
    默认写出带 BOM 的 UTF-8，`utf-8` 解码会把 BOM 留在首字符导致 JSON 解析失败、整份
    config 被判损坏跳过；`utf-8-sig` 对无 BOM 输入同样正确。写入端保持 `utf-8`（不写 BOM）。
    """
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("config root 必须为 object")
    return data


def atomic_write_json(path: Path, data: dict) -> None:
    """tempfile + os.replace 原子写：写完 tmp 才替换目标，不留半写文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".migrate_config_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, str(path))


def write_backup(raw: dict) -> Path:
    """把改动前的原始 config 备份到 backup_dir_path()，文件名含 UTC 时间戳
    （精确到微秒，避免同一秒内连续多次 --apply 互相覆盖）。返回备份文件路径。
    """
    backup_dir = backup_dir_path()
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"config-{ts}.json"
    atomic_write_json(backup_path, raw)
    return backup_path


# ── CLI 动作 ──────────────────────────────────────────────────────────────

def _do_dry_run(config_path: Path) -> int:
    if not config_path.exists():
        print(f"[migrate_config] {config_path} 不存在，无需处理")
        return 0
    try:
        raw = load_raw_config(config_path)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[migrate_config] 读取失败：{exc}", file=sys.stderr)
        return 1

    residue = find_residue(raw)
    if not residue:
        print(f"[migrate_config] dry-run：{config_path} 未发现物化残留，无需操作")
        return 0

    print(f"[migrate_config] dry-run：{config_path} 发现 {len(residue)} 个物化残留键"
          f"（加 --apply 执行清理，不加不会改动任何文件）：")
    for segments, value in residue:
        print(f"  将删除 {format_path(segments)}={value!r}")
    return 0


def _do_apply(config_path: Path, force: bool = False) -> int:
    if _abort_if_redirected(config_path, force):
        return 1
    if not config_path.exists():
        print(f"[migrate_config] {config_path} 不存在，无需处理")
        return 0
    try:
        raw = load_raw_config(config_path)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[migrate_config] 读取失败：{exc}", file=sys.stderr)
        return 1

    residue = find_residue(raw)
    if not residue:
        print(f"[migrate_config] {config_path} 未发现物化残留，无需操作")
        return 0

    backup_path = write_backup(raw)
    cleaned = deepcopy(raw)
    # 只对**真的删掉了**的路径打印"已删除"——remove_path 返回 False 说明该路径在盘上
    # 结构里并不存在（理论上不该发生，find_residue 刚遍历出来的），此时宁可少报也不谎报。
    removed = [(segments, value) for segments, value in residue
               if remove_path(cleaned, segments)]
    atomic_write_json(config_path, cleaned)

    print(f"[migrate_config] 已备份原始 config 到 {backup_path}")
    print(f"[migrate_config] 已清理 {len(removed)} 个物化残留键：")
    for segments, value in removed:
        print(f"  已删除 {format_path(segments)}={value!r}")
    skipped = len(residue) - len(removed)
    if skipped:
        print(f"[migrate_config] 另有 {skipped} 个候选键实际未在盘上定位到，已跳过未删",
              file=sys.stderr)
    print(f"[migrate_config] 如需撤销：python migrate_config.py --restore {backup_path}")
    return 0


def _do_restore(backup_path: Path, config_path: Path, force: bool = False) -> int:
    if not backup_path.exists():
        print(f"[migrate_config] 备份文件 {backup_path} 不存在", file=sys.stderr)
        return 1
    if _abort_if_redirected(config_path, force):
        return 1
    try:
        data = load_raw_config(backup_path)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[migrate_config] 备份文件读取失败：{exc}", file=sys.stderr)
        return 1

    unknown = unknown_top_level_keys(data)
    if unknown and not force:
        print(f"[migrate_config] {backup_path} 含非 vault-loader config 的顶层键 "
              f"{unknown}，不像本工具产出的备份，拒绝还原（确认无误可加 --force）",
              file=sys.stderr)
        return 1
    if unknown:
        print(f"[migrate_config] 警告：备份含非 schema 顶层键 {unknown}，"
              f"已指定 --force，继续还原", file=sys.stderr)

    # 覆盖前先备份当前盘上内容：否则 `--apply` 之后用户手工做的调参会被 `--restore`
    # 静默抹掉且无任何恢复路径，与本工具"每次写入都先备份"的安全叙事矛盾。
    if config_path.exists():
        try:
            current = load_raw_config(config_path)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            if not force:
                print(f"[migrate_config] 当前 {config_path} 无法读取（{exc}），"
                      f"无法备份，拒绝还原（确认可丢弃当前内容再加 --force）",
                      file=sys.stderr)
                return 1
            print(f"[migrate_config] 警告：当前 config 无法读取（{exc}），"
                  f"已指定 --force，将直接覆盖且无备份", file=sys.stderr)
        else:
            try:
                pre_backup = write_backup(current)
            except OSError as exc:
                if not force:
                    print(f"[migrate_config] 备份当前 config 失败（{exc}），拒绝还原"
                          f"（确认可丢弃当前内容再加 --force）", file=sys.stderr)
                    return 1
                print(f"[migrate_config] 警告：备份当前 config 失败（{exc}），"
                      f"已指定 --force，将直接覆盖且无备份", file=sys.stderr)
            else:
                print(f"[migrate_config] 已把还原前的 config 备份到 {pre_backup}")

    atomic_write_json(config_path, data)
    print(f"[migrate_config] 已从 {backup_path} 还原 {config_path}")
    return 0


def _doctor_merge(raw: dict) -> dict:
    """doctor 侧的配置合并。**必须与生产的 _deep_merge 同语义**——旧实现是
    `dict(DEFAULT_CONFIG)` + `update(...)` 浅合并，用户只写部分子对象时整段被替换，
    doctor 会把未写的键报成 None。

    注意保留元键过滤：旧实现用 `{k: v for k, v in raw.items() if k not in _CONFIG_META_KEYS}`
    排除 `_config_version` / `_comment`，深合并后必须保持同样的排除，否则元键会混进
    诊断输出。
    """
    from scripts._config_loader import DEFAULT_CONFIG, _deep_merge
    cleaned = {k: v for k, v in (raw or {}).items() if k not in _CONFIG_META_KEYS}
    return _deep_merge(DEFAULT_CONFIG, cleaned)


def _do_doctor(config_path: Path) -> int:
    """健康自检：把散落在 stderr（无人读）的诊断集中成一次用户可读的输出。

    **绝不写盘。** 这里刻意不用 `_config_loader.load_config`——它在文件缺失时会
    `mkdir(parents=True)` 并写入占位，于是 `--doctor --path <任意路径>` 就变成了
    「在任意位置创建目录树」，而 doctor 路径又没有 `_abort_if_redirected` 的重定向守卫。
    doctor 恰恰是最可能被模型经 Bash 工具代跑的命令，必须保持纯只读。

    **字段白名单输出**：只打印健康判定所需的最小集合，路径一律折叠 `~`。刻意不 dump
    `keyword_to_tags` / `opt_out_paths` / 任何 config 原始值——doctor 的输出会被贴进
    issue、被模型读进 transcript，里面全是用户的本机路径与项目代号。
    """
    from scripts._config_loader import compare_vault_paths
    from scripts._diagnostics import fold_home
    from scripts._frontmatter_reader import CACHE_VERSION, CacheStatus, load_cache_status

    print("vault-loader 健康自检（只读，不改动任何文件）")
    print(f"  config 路径      : {fold_home(config_path)}")

    raw: dict = {}
    if not config_path.exists():
        print("  config 状态      : 不存在（零配置新装，将使用全部默认值）")
    else:
        try:
            raw = load_raw_config(config_path)
            print("  config 状态      : ✅ 可解析")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            print(f"  config 状态      : ❌ 解析失败 —— {exc}")
            print("                     后果：vault_path、scoring 权重、relevance 阈值、")
            print("                     keyword_to_tags、opt_out_paths 全部回退默认值。")

    # 旧版首跑物化的残留（R-4）。doctor 此前对它完全沉默，而它恰恰是「装了新版、
    # 修复却只生效一半」的成因：存量用户的 config 里压着 scoring.prompt_keyword_hit=3，
    # 新装用户是 5，两者跑的是不同权重，且**没有任何征兆**——召回变差不会报错。
    # 文档把 --doctor 指定为排障入口，它必须能回答「我这台机器是不是这个状况」。
    #
    # 只报键名与计数，不报值：doctor 的输出会被贴进 issue、被模型读进 transcript。
    # 键名属 schema、不含用户数据；要看具体值请跑 dry-run（那是本地交互场景）。
    residue = find_residue(raw) if raw else []
    if residue:
        print(f"  旧版默认值残留   : ❌ {len(residue)} 项")
        for segments, _value in residue[:10]:
            print(f"                     · {format_path(segments)}")
        if len(residue) > 10:
            print(f"                     · …另有 {len(residue) - 10} 项")
        print("                     这些键的当前值等于某个历史默认，会压制新版默认值。")
        print("                     跑 migrate_config.py（dry-run）看详情，确认后 --apply。")
    elif raw:
        print("  旧版默认值残留   : ✅ 无")

    merged = _doctor_merge(raw)
    vault_path = Path(str(merged.get("vault_path", ""))).expanduser()

    print(f"  vault 路径       : {fold_home(vault_path)}")
    print(f"  vault 是否存在   : {'✅ 是' if vault_path.is_dir() else '❌ 否'}")

    entries, status = load_cache_status(vault_path)
    healthy = {CacheStatus.OK, CacheStatus.EMPTY, CacheStatus.ABSENT,
               CacheStatus.VERSION_MISMATCH}
    mark = "✅" if status in healthy else "❌"
    print(f"  索引状态         : {mark} {status.value}（读端期望 _version={CACHE_VERSION}）")
    print(f"  索引条目数       : {len(entries)}")
    if status is CacheStatus.ABSENT:
        print("                     索引尚未生成——跑一次 /summarize-session 即可。")
    elif status is CacheStatus.VERSION_MISMATCH:
        print("                     索引版本与读端不符，属预期过渡态；")
        print("                     summarize-session 下次运行会重建。")
    elif status.is_failure():
        print("                     索引不可用，召回本轮为空。跑 /summarize-session 重建。")

    # 跨 skill 一致性只报**布尔判定**，不打印两侧路径原文
    pair = compare_vault_paths(merged)
    print(f"  跨 skill 路径一致: {'❌ 否' if pair else '✅ 是'}")
    if pair:
        print("                     vault-loader 与 summarize-session 指向不同目录，")
        print("                     写入与读取会落在两处。（路径原文从略；见各自 config）")

    for key in ("enabled", "dry_run", "verbose_on_skip"):
        print(f"  {key:16s} : {merged.get(key)}")
    display = merged.get("display", {})
    if isinstance(display, dict):
        print(f"  display.user_visible: {display.get('user_visible', True)}")
    print(f"  metrics.enabled      = {merged['metrics']['enabled']}")
    print(f"  metrics.retention_days = {merged['metrics']['retention_days']}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_config.py",
        description="vault-loader config 收敛脚本：清理首跑物化残留（spec §8.2）",
    )
    parser.add_argument(
        "--path", type=Path, default=None,
        help="config.json 路径（默认 ~/.claude/skills/vault-loader/config.json）",
    )
    # --apply 与 --restore 语义互斥（一个清残留、一个整份覆盖回滚），此前 --restore
    # 静默胜出、同时传两个不会有任何提示；交给 argparse 直接报 usage 错误。
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply", action="store_true",
        help="真正执行清理并写盘（默认仅 dry-run 只读打印，不加此项不会改动任何文件）",
    )
    action.add_argument(
        "--restore", type=Path, default=None, metavar="BACKUP_PATH",
        help="从指定备份文件还原 config.json（覆盖前会先把当前内容备份出去）",
    )
    # 放进同一互斥组：--doctor 是纯只读自检，与两个写动作同时给没有意义，
    # 不进组的话 `--doctor --apply` 会静默取其一。
    action.add_argument(
        "--doctor", action="store_true",
        help="健康自检：只读打印 config / vault / 索引状态，不改动任何文件",
    )
    parser.add_argument(
        "--force", "-y", action="store_true",
        help="逃生阀：放行重定向路径（软链/junction）、放行非 schema 备份、"
             "以及在无法备份当前内容时仍然覆盖。刻意覆盖场景才用。",
    )
    args = parser.parse_args(argv)

    config_path = args.path if args.path is not None else default_config_path()

    if args.doctor:
        return _do_doctor(config_path)
    if args.restore is not None:
        return _do_restore(args.restore, config_path, args.force)
    if args.apply:
        return _do_apply(config_path, args.force)
    return _do_dry_run(config_path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 — reconfigure 在极少数流环境可能不可用，静默忽略
        pass
    raise SystemExit(main())
