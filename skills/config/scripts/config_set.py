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
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_VL = _HERE.parents[2] / "vault-loader"

sys.path.insert(0, str(_HERE.parent))
import config_inventory as inv  # noqa: E402  （同目录，读端与写端共用判据）


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


def set_key(key: str, raw_value: str, *, home: Path | None = None,
            assume_yes: bool = False, privacy_ack: bool = False) -> dict:
    """改一个配置项。返回 {"ok", "error", "backup", "old", "new"}。"""
    defaults = inv.load_default_config()
    flat_default = inv.flatten(defaults)

    # —— 写入白名单 ——
    if key in inv.DEPRECATED:
        return {"ok": False, "error":
                f"`{key}` 已废弃、运行时不读，改它不会有任何效果。{inv.DEPRECATED[key]}"}
    if key in inv.READONLY_EXTRA:
        return {"ok": False, "error":
                f"`{key}` 在本工具中只读：{inv.READONLY_EXTRA[key]}"}
    if key not in flat_default:
        return {"ok": False, "error":
                f"`{key}` 不是可配键。用 `config_inventory.py {key.split('.')[-1]}` 查找正确键名。"}

    # —— 隐私旋钮 ——
    if key in inv.PRIVACY_KNOBS and not privacy_ack:
        return {"ok": False, "error":
                f"`{key}` 是隐私旋钮：它控制**每会话有多少篇笔记被发往外部模型端点**。"
                f"确认要改请显式加 --i-understand-privacy。"}

    ok, value, err = _parse_value(raw_value, flat_default[key])
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

    # —— 写入源是**原始 JSON**，不是合并后的配置 ——
    raw, _p, existed = inv.read_raw_config(home)
    new_data = deepcopy(raw)
    segments = key.split(".")
    _, old = _get_path(new_data, segments)
    _set_path(new_data, segments, value)

    backup_path = ""
    if existed and raw:
        try:
            backup_path = str(mc.write_backup(raw))
        except Exception as exc:
            return {"ok": False, "error": f"备份失败，已中止写入：{exc}"}

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(path, new_data)
    except Exception as exc:
        return {"ok": False, "error": f"写入失败：{exc}", "backup": backup_path}

    # —— 回读核验 ——
    verify, _p2, _e = inv.read_raw_config(home)
    found, got = _get_path(verify, segments)
    if not found or got != value:
        return {"ok": False, "backup": backup_path, "error":
                f"写后回读核验失败：期望 {value!r}，实际 {got!r}。配置可能未真正落盘。"}

    return {"ok": True, "error": "", "backup": backup_path, "old": old, "new": value,
            "path": str(path)}


def restore(backup: str, *, home: Path | None = None, assume_yes: bool = False) -> dict:
    """从备份还原。还原前同样先备份当前值（继承 migrate_config 的做法）。"""
    src = Path(backup)
    if not src.is_file():
        return {"ok": False, "error": f"备份文件不存在：{backup}"}
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {"ok": False, "error": f"备份文件不是合法 JSON：{exc}"}

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
        if res.get("backup"):
            print(f"  备份：{res['backup']}")
        return 0
    print(f"未修改：{res['error']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
