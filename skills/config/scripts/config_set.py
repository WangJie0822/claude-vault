#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全改写 context-vault 的配置项。

安全边界**继承自同仓 `migrate_config.py`**（读写同一份 config.json、已过一轮安全评审），
不重新发明：重定向守卫、备份到独立目录、原子写、restore 都直接复用它的实现。

四条只属于本工具的约束：

1. **写入源必须是原始 JSON，绝不是 `load_config()` 的返回值。** 后者是合并后的完整
   配置，写回去会一次性物化 66 个叶子键 —— 此后该用户**所有**默认值演进永久静默失效
   （`_config_loader` 的 `_MINIMAL_STUB` 占位策略正是为避免这件事）；且 `_config_version`
   / `_comment` 在合并时已被剥除，写回等于删掉迁移版本判据和指向 SKILL.md 的唯一线索。
2. **写入白名单**：只接受 `DEFAULT_CONFIG` 里存在、且未废弃、且非元键的叶子路径。
   `obsidian_cli.*` / `work_log_dir` 首版只读 —— 它们不在 `DEFAULT_CONFIG` 里（无默认值与
   类型判据），且 `obsidian_cli` 只认 canonical 而本工具走 `resolve_config_path()`，
   legacy 安装上两者分叉，写了等于没写还会让读端显示一个不生效的值。
3. **隐私旋钮需显式确认**：三个 `session_topic_*` 是 int 型「送出去多少篇」的开关，
   类型校验对它们完全无效。
4. **写后回读核验**，核验不过报错而非静默成功。

2.4.0 起另有四条（spec 2026-10-13 §5.3、§11）：被取代的旧键拒写、写新键时一并删掉旧键；
文件在但读不出时拒写（此前当成空 dict 写回，整份配置被一个键替换）；读改写全程持 `lease_lock`
（面板的「设为默认」与 /config 可能同时写）；入参可以是 bool / int / list，统一转文本再解析。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_VL = _HERE.parents[2] / "vault-loader"

sys.path.insert(0, str(_HERE.parent))
import config_inventory as inv  # noqa: E402  （同目录，读端与写端共用判据）

# 备份目录（与 migrate_config 共用）只留最近这么多份 `config-*.json`【初值】
BACKUP_KEEP = 20
# 等配置写锁的上限（秒）；用例会调小
_LOCK_TIMEOUT = 2.0
# 新键 → 被它取代的旧键
_OLD_OF = {new: old for old, new in inv.SUPERSEDED.items()}


def _migrate_config():
    """加载 vault-loader 的 migrate_config，复用其已评审的守卫。"""
    path = _VL / "scripts" / "migrate_config.py"
    spec = importlib.util.spec_from_file_location("_cv_migrate_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _atomic_write(path: Path, data: dict) -> None:
    """原子写。单独抽出来是为了让回读核验的守卫能注入故障。"""
    _migrate_config().atomic_write_json(path, data)


def _parse_value(raw: str, default: Any) -> tuple[bool, Any, str]:
    """按同键默认值的类型解析入参。返回 (ok, value, error)。

    **bool 必须先于 int 判断**：`isinstance(True, int)` 为真，不特判就会让
    `true` 悄悄写进一个 int 键。
    """
    s = raw.strip()
    if isinstance(default, bool):
        low = s.lower()
        if low in ("true", "1", "yes", "on"):
            return True, True, ""
        if low in ("false", "0", "no", "off"):
            return True, False, ""
        return False, None, f"类型不符：该键是布尔值，收到 {raw!r}"
    if isinstance(default, int):
        if s.lower() in ("true", "false"):
            return False, None, f"类型不符：该键是整数，收到布尔值 {raw!r}"
        try:
            return True, int(s), ""
        except ValueError:
            return False, None, f"类型不符：该键是整数，收到 {raw!r}"
    if isinstance(default, float):
        try:
            return True, float(s), ""
        except ValueError:
            return False, None, f"类型不符：该键是浮点数，收到 {raw!r}"
    if isinstance(default, list):
        try:
            v = json.loads(s)
        except ValueError:
            return False, None, f"类型不符：该键是列表，请用 JSON 写法如 [\"a\",\"b\"]，收到 {raw!r}"
        if not isinstance(v, list):
            return False, None, f"类型不符：该键是列表，收到 {type(v).__name__}"
        return True, v, ""
    if isinstance(default, dict):
        try:
            v = json.loads(s)
        except ValueError:
            return False, None, f"类型不符：该键是对象，请用 JSON 写法，收到 {raw!r}"
        if not isinstance(v, dict):
            return False, None, f"类型不符：该键是对象，收到 {type(v).__name__}"
        return True, v, ""
    return True, s, ""


def _set_path(d: dict, segments: list[str], value: Any) -> None:
    cur = d
    for seg in segments[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segments[-1]] = value


def _get_path(d: dict, segments: list[str]) -> tuple[bool, Any]:
    cur: Any = d
    for seg in segments:
        if not isinstance(cur, dict) or seg not in cur:
            return False, None
        cur = cur[seg]
    return True, cur


def _del_path(d: dict, segments: list[str]) -> None:
    cur: Any = d
    for seg in segments[:-1]:
        cur = cur.get(seg) if isinstance(cur, dict) else None
    if isinstance(cur, dict):
        cur.pop(segments[-1], None)


def _as_text(raw_value: Any) -> str:
    """入参统一成文本再交 `_parse_value`：CLI 传 str，hook 里面板的「设为默认」传 bool / int。
    列表与对象用 JSON 写法——`str(["a"])` 是 Python 写法，`_parse_value` 解析不了。"""
    if isinstance(raw_value, str):
        return raw_value
    if isinstance(raw_value, bool):
        return "true" if raw_value else "false"
    if isinstance(raw_value, (list, dict)):
        return json.dumps(raw_value, ensure_ascii=False)
    return str(raw_value)


def _prune_backups(mc) -> None:
    """备份目录只留最近 BACKUP_KEEP 份备份（按名排序即按时间）。目录与 migrate_config 共用，它的备份
    也按同一规则淘汰。**只认备份生产者的文件名形态** `config-<UTC 时间戳>.json`
    （`migrate_config.write_backup` 的 `%Y%m%dT%H%M%S%fZ`：8 位日期、T、12 位时分秒微秒、Z）：
    用户手工放进来的 `config-*.json` 副本、别的文件、链接一概不碰（淘汰是不可逆的删除）。"""
    try:
        files = sorted(
            p for p in mc.backup_dir_path().glob("config-*.json")
            if re.fullmatch(r"config-\d{8}T\d{12}Z\.json", p.name)
            and p.is_file() and not p.is_symlink())
    except OSError:
        return
    for p in files[:-BACKUP_KEEP]:
        try:
            p.unlink()
        except OSError:
            pass


def _config_lock(path: Path):
    """配置文件的写锁。插件根已由 `inv._config_path` 放进 sys.path；拿不到实现时由调用方拒写
    （fail-closed：少一道互斥可能静默覆盖另一处的写入，拒写只是这次没改成）。"""
    from context_vault.atomic import lease_lock
    return lease_lock(path, timeout=_LOCK_TIMEOUT)


def set_key(key: str, raw_value: Any, *, home: Path | None = None,
            assume_yes: bool = False, privacy_ack: bool = False) -> dict:
    """改一个配置项。返回 {"ok", "error", "backup", "old", "new", "path", "removed"}。

    `raw_value` 可以是 str（CLI）或 bool / int / list / dict（hook 里面板的「设为默认」），先转文本。
    `removed`：写新键时一并删掉的被取代旧键（全路径）。
    """
    defaults = inv.load_default_config()
    flat_default = inv.flatten(defaults)

    # —— 写入白名单 ——
    if key in inv.SUPERSEDED:
        new = inv.SUPERSEDED[key]
        return {"ok": False, "error":
                f"`{key}` 已由 `{new}` 取代（旧键仍被兼容读取）。请改写 `{new}`："
                f"写入新键时会一并删除旧键。"}
    if key in inv.DEPRECATED:
        return {"ok": False, "error":
                f"`{key}` 已废弃、运行时不读，改它不会有任何效果。{inv.DEPRECATED[key]}"}
    if key in inv.READONLY_EXTRA:
        return {"ok": False, "error":
                f"`{key}` 在本工具中只读：{inv.READONLY_EXTRA[key]}"}
    if key not in flat_default:
        return {"ok": False, "error":
                f"`{key}` 不是可配键。用 `config_inventory.py {key.split('.')[-1]}` 查找正确键名。"}

    if raw_value is None:
        # `_as_text(None)` 会得到字面量 "None"，str 型键（如 relevance.fine_model）会照写并返回 ok
        return {"ok": False, "error": f"缺少值：`{key}` 没有给出要写入的新值。"}

    # —— 隐私旋钮 ——
    if key in inv.PRIVACY_KNOBS and not privacy_ack:
        return {"ok": False, "error":
                f"`{key}` 是隐私旋钮：它控制**每会话有多少篇笔记被发往外部模型端点**。"
                f"确认要改请显式加 --i-understand-privacy。"}

    ok, value, err = _parse_value(_as_text(raw_value), flat_default[key])
    if not ok:
        return {"ok": False, "error": err}

    mc = _migrate_config()
    path = inv._config_path(home)

    # —— 重定向守卫（继承 migrate_config：路径任意一段经 symlink/junction 即拒）——
    try:
        if mc.is_path_redirected(path):
            return {"ok": False, "error":
                    f"目标路径存在重定向（符号链接 / junction）：{path}。"
                    f"出于安全考虑拒绝写入。"}
    except Exception:
        pass  # 守卫自身异常不应变成写入失败以外的行为，交由下面的核验兜底

    try:
        lock = _config_lock(path)
    except ImportError as exc:
        return {"ok": False, "error":
                f"无法加载配置写锁（context_vault.atomic）：{exc}。为免与另一处写入互相覆盖，未写入。"}
    try:
        with lock:
            return _write_locked(mc, path, key, value, home)
    except TimeoutError:                       # 必须排在 OSError 之前：TimeoutError 是它的子类
        return {"ok": False, "error":
                f"配置文件正被另一处写入（等锁 {_LOCK_TIMEOUT} 秒超时），未写入，请稍后再试。"}
    except OSError as exc:
        # lease_lock 在 POSIX 上遇 PermissionError（配置目录只读等）原样抛出，不转成超时。
        # `_write_locked` 内部的 OSError 都已就地处理（备份/写入/淘汰各自返回 dict），到这里的只会是取锁失败。
        return {"ok": False, "error": f"无法创建配置写锁文件：{exc}。未写入。"}


def _write_locked(mc, path: Path, key: str, value: Any, home: Path | None) -> dict:
    """持锁时的读改写。写入源是**原始 JSON**，不是合并后的配置（见模块 docstring 第 1 条）。"""
    raw, _p, existed, readable = inv.read_raw_config_strict(home)
    if existed and not readable:
        return {"ok": False, "error":
                f"配置文件存在但读不出（不是合法的 JSON 对象）：{path}。为免覆盖你的内容，未写入；"
                f"请先修好它，或用 --restore 从备份还原。"}
    new_data = deepcopy(raw)
    segments = key.split(".")
    _, old = _get_path(new_data, segments)
    _set_path(new_data, segments, value)
    removed: list[str] = []
    old_key = _OLD_OF.get(key)
    if old_key is not None and _get_path(new_data, old_key.split("."))[0]:
        _del_path(new_data, old_key.split("."))
        removed.append(old_key)

    backup_path = ""
    if existed and raw:
        try:
            backup_path = str(mc.write_backup(raw))
        except Exception as exc:
            return {"ok": False, "error": f"备份失败，已中止写入：{exc}"}
        _prune_backups(mc)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, new_data)
    except Exception as exc:
        return {"ok": False, "error": f"写入失败：{exc}", "backup": backup_path}

    # —— 回读核验：新值落盘、被取代的旧键已删 ——
    verify, _p2, _e = inv.read_raw_config(home)
    found, got = _get_path(verify, segments)
    if not found or got != value:
        return {"ok": False, "backup": backup_path, "error":
                f"写后回读核验失败：期望 {value!r}，实际 {got!r}。配置可能未真正落盘。"}
    left_over = [k for k in removed if _get_path(verify, k.split("."))[0]]
    if left_over:
        return {"ok": False, "backup": backup_path, "error":
                f"写后回读核验失败：被取代的旧键 `{left_over[0]}` 仍在盘上。配置可能未真正落盘。"}

    return {"ok": True, "error": "", "backup": backup_path, "old": old, "new": value,
            "path": str(path), "removed": removed}


def restore(backup: str, *, home: Path | None = None, assume_yes: bool = False) -> dict:
    """从备份还原。还原前同样先备份当前值（继承 migrate_config 的做法）。

    备份必须是 JSON 对象（2.4.0 full-review DO-M2）：数组、标量写进去后 loader 判 config 读坏、整份回落默认值，
    精排、目录层等隐私旋钮随之回到默认的「开」。不是对象就拒绝，不写盘（不备份当前值、不动目标文件）。"""
    src = Path(backup)
    if not src.is_file():
        return {"ok": False, "error": f"备份文件不存在：{backup}"}
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {"ok": False, "error": f"备份文件不是合法 JSON：{exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error":
                f"备份文件的顶层不是 JSON 对象（是 {type(data).__name__}），不是配置备份，未还原。"}

    mc = _migrate_config()
    path = inv._config_path(home)
    cur, _p, existed = inv.read_raw_config(home)
    pre = str(mc.write_backup(cur)) if existed and cur else ""
    _atomic_write(path, data)
    return {"ok": True, "error": "", "backup": pre, "path": str(path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安全修改 context-vault 的配置项")
    ap.add_argument("key", nargs="?", help="配置键（全路径，如 relevance.min_topical_score）")
    ap.add_argument("value", nargs="?", help="新值")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--i-understand-privacy", dest="privacy_ack", action="store_true",
                    help="确认知晓隐私旋钮会改变外发篇数")
    ap.add_argument("--restore", metavar="BACKUP", help="从备份还原")
    args = ap.parse_args(argv)

    if args.restore:
        res = restore(args.restore, assume_yes=args.yes)
    elif args.key and args.value is not None:
        res = set_key(args.key, args.value, assume_yes=args.yes,
                      privacy_ack=args.privacy_ack)
    else:
        ap.error("需要 <key> <value>，或 --restore <备份路径>")
        return 2

    if res["ok"]:
        print(f"已写入 {res.get('path', '')}")
        if "old" in res:
            print(f"  {args.key}: {res['old']!r} -> {res['new']!r}")
        for old_key in res.get("removed") or []:
            print(f"  已删除被取代的旧键：{old_key}")
        if res.get("backup"):
            print(f"  备份：{res['backup']}")
        return 0
    print(f"未修改：{res['error']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    # 输出含配置路径与键值：控制台编码不是 UTF-8 时，GBK 之外的字符会让 print 崩（同 vault-loader migrate_config）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 — 被替换成不支持 reconfigure 的流时保持原样
            pass
    sys.exit(main())
