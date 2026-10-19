# -*- coding: utf-8 -*-
"""精排相关配置的单点（spec 2.4.0 §5.2 / §5.3 / §10.3 / §10.4）。

三处共用同一套校验（F17）：读 config、mod 面板的会话设置、「设为默认」（persist）。调用方不得各自
`rel.get(...)` 再算一份——`effective_pin_max` 的默认值曾散在 4 处，改一处漏三处（1A-L2）。

旧键兼容的两条读取规则（对经 `load_config_ex` 的 config 两键恒相等，规则只为直接构造的 dict 兜底）：
- `fine_rank_on`：`fine_rank` 与 `session_topic` 任一为 false 即关。新键按 §5.2 只认 bool；旧键沿用
  2.3.0 的真值化口径（`bool(v)`），不改变存量配置的含义。
- `pin_max_raw`：新键、旧键中**不等于默认值**的那个胜出，新键优先。新键按 §5.2 拒 bool；旧键按
  Ruling 9（bool 按 int、数字字符串按 int、垃圾回落默认）。

加载路径（`_config_loader.resolve_superseded`）经 `explicit_value` 复用同一份归一：新键类型不合格 ⇒ 当缺失、
不镜像；类型合格但越界 ⇒ 取截断值。所以新键的坏值与越界值，经加载与直接构造 dict 读出来结论一致；
唯一例外是新键显式写成默认值、旧键写了别的值：加载按原始 override 判出「新键显式」取新键，直接构造的
dict 分不出显式与默认、按上面的规则取旧键（F34 的已知取舍，经加载器的 config 不受影响）。

`apply_session_settings` 返回的 config 已把全部精排键归一成校验后的值（并镜像旧键），之后整轮代码
只读这份 config，`max_notes` 等面板项的会话值因此对下游透明。
"""
from __future__ import annotations

import math
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field

from ._config_loader import DEFAULT_CONFIG, resolve_superseded  # noqa: F401 — 后者重导出

PANEL_KEYS: tuple = ("coarse_inject", "fine_rank", "fine_supplement", "auto_fulltext",
                     "user_prompt_submit.max_notes", "fine_pin_max", "fine_max_notes", "fine_model")
BOOL_KEYS: tuple = ("coarse_inject", "fine_rank", "fine_supplement", "auto_fulltext", "fine_catalog")
INT_RANGES: dict = {"fine_pin_max": (0, 8), "fine_max_notes": (0, 8), "fine_timeout_sec": (10, 300),
                    "fine_max_calls": (1, 500), "fine_session_max_bytes": (0, 20_000_000),
                    "fine_catalog_max_chars": (0, 400_000), "fine_recent_prompts": (0, 10),
                    "user_prompt_submit.max_notes": (0, 8)}
# 不得以 `-` 开头（会被解析成命令行选项）、不含 cmd 元字符（npm 的 .CMD 垫片经 cmd.exe）、长度 ≤ 64
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}")
DEFAULT_MODEL = "haiku"
SUPERSEDED: dict = {"relevance.session_topic": "relevance.fine_rank",
                    "relevance.session_pin_max": "relevance.fine_pin_max"}

_UPS = "user_prompt_submit."
_KNOWN = frozenset(BOOL_KEYS) | frozenset(INT_RANGES) | {"fine_model", "fine_exclude_tags"}
# apply_session_settings 归一写回的全部键（面板 8 键在前）
_ALL_KEYS = tuple(dict.fromkeys(PANEL_KEYS + BOOL_KEYS + tuple(INT_RANGES)
                                + ("fine_model", "fine_exclude_tags")))
_NARROW_INTS = ("user_prompt_submit.max_notes", "fine_pin_max", "fine_max_notes")


def _split(key: str) -> "tuple[str, str]":
    if key.startswith(_UPS):
        return "user_prompt_submit", key[len(_UPS):]
    return "relevance", key


def _section(config, name: str) -> dict:
    d = config.get(name) if isinstance(config, dict) else None
    return d if isinstance(d, dict) else {}


def default_of(key: str):
    """DEFAULT_CONFIG 里的默认值（深拷贝）；未知键 None。"""
    if key not in _KNOWN:
        return None
    sec, leaf = _split(key)
    return deepcopy(DEFAULT_CONFIG[sec][leaf])


def _normalize(key: str, value) -> "tuple[bool, object, bool]":
    """归一的唯一实现。返回 (原样合法?, 归一后的值, 类型合格?)。

    「类型合格」= 归一值源自用户写的那个值（越界截断、剔除坏元素都算）；不合格 = 归一值只是兜底默认，
    与用户写的东西无关。`validate_value` 只要前两项，`explicit_value` 要后一项——同一份判断，不写两遍。
    """
    if key not in _KNOWN:
        return False, None, False
    default = default_of(key)
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return True, value, True
        return False, default, False
    if key in INT_RANGES:
        lo, hi = INT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, default, False
        if isinstance(value, float):
            if not math.isfinite(value):
                return False, default, False
            iv = int(value)
            exact = float(iv) == value
        else:
            iv = value
            exact = True
        clipped = max(lo, min(hi, iv))
        return exact and clipped == iv, clipped, True
    if key == "fine_model":
        if isinstance(value, str) and MODEL_RE.fullmatch(value):
            return True, value, True
        return False, DEFAULT_MODEL, False
    # fine_exclude_tags。失败方向是「多排除」：单个字符串当一个 tag，空串与其他类型回落默认 ["private"]，
    # 不归一成 []（那等于什么都不排除，2.4.0 full-review S-L1）。list 照旧：剔非 str 与空串，空 list 是用户的显式选择
    if isinstance(value, str):
        return (False, [value], True) if value.strip() else (False, default, False)
    if not isinstance(value, list):
        return False, default, False
    kept = [t for t in value if isinstance(t, str) and t]
    return len(kept) == len(value), kept, True


def validate_value(key: str, value) -> "tuple[bool, object]":
    """单点校验（§5.2）。返回 (原样合法?, 归一后的值)。

    bool 键：非 bool → (False, 默认)。整数键：bool、非数、非有限数 → (False, 默认)；越界截到端点
    → (False, 端点)；有小数的浮点取 int() 并判不合法。fine_model：不 fullmatch → (False, "haiku")
    （`fullmatch` 而非 `match`：结尾换行也不放过）。fine_exclude_tags：非空字符串 → (False, [该字符串])，
    空串与其他非 list → (False, 默认 ["private"])；list 剔非 str 与空串（剔过即 False）。未知键 → (False, None)。
    """
    ok, normalized, _typed = _normalize(key, value)
    return ok, normalized


def explicit_value(key: str, value) -> "tuple[bool, object]":
    """用户在 config 里写的值算不算对该键的「显式设置」（§5.3「新键显式」的判据）。返回 (算?, 归一值)。

    类型合格（含越界、有小数——取 §5.2 截断后的归一值）⇒ (True, 归一值)；类型不合格（bool 键给了非 bool，
    整数键给了 bool / 非数 / 非有限数，模型名不合法，tags 既不是 list 也不是非空字符串）⇒ (False, 兜底默认)，调用方当缺失处理、
    不得把原值镜像到别处。`_config_loader.resolve_superseded` 靠它让「加载路径」与 `fine_rank_on` /
    `pin_max_raw` 的直读对同一输入给出同一结论。
    """
    _ok, normalized, typed = _normalize(key, value)
    return typed, normalized


def fine_rank_on(config) -> bool:
    """精排是否开（新旧键任一为 false 即关）。"""
    try:
        rel = _section(config, "relevance")
        new = rel.get("fine_rank", True)
        new_on = new if isinstance(new, bool) else True
        old_on = bool(rel["session_topic"]) if "session_topic" in rel else True
        return new_on and old_on
    except Exception:  # noqa: BLE001 — 读取单点永不抛
        return bool(DEFAULT_CONFIG["relevance"]["fine_rank"])


def _legacy_pin(value, default: int) -> int:
    """旧键 session_pin_max 的 Ruling 9 口径：bool 按 int（false → 0 篇）、数字字符串按 int、垃圾回落默认。"""
    try:
        return max(0, min(8, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def pin_max_raw(config) -> int:
    """首轮点名上限（未计 session_pin_enabled）：不等于默认值的那个胜出，新键优先。"""
    default = int(DEFAULT_CONFIG["relevance"]["fine_pin_max"])
    try:
        rel = _section(config, "relevance")
        if "fine_pin_max" in rel:
            new_v = validate_value("fine_pin_max", rel["fine_pin_max"])[1]
            if new_v != default:
                return new_v
        if "session_pin_max" in rel:
            old_v = _legacy_pin(rel["session_pin_max"], default)
            if old_v != default:
                return old_v
        return default
    except Exception:  # noqa: BLE001
        return default


def get_value(config, key: str):
    """经校验归一后的有效值。fine_rank / fine_pin_max 走上面两条兼容规则。"""
    if key == "fine_rank":
        return fine_rank_on(config)
    if key == "fine_pin_max":
        return pin_max_raw(config)
    if key not in _KNOWN:
        return None
    sec, leaf = _split(key)
    d = _section(config, sec)
    if leaf not in d:
        return default_of(key)
    return validate_value(key, d[leaf])[1]


def fine_supplement_on(config) -> bool:
    return fine_rank_on(config) and bool(get_value(config, "fine_supplement"))


@dataclass
class SettingsView:
    config: dict                                    # 有效 config（深拷贝，全部精排键已归一并镜像旧键）
    effective: dict = field(default_factory=dict)   # PANEL_KEYS → 有效值（含会话值）
    session: dict = field(default_factory=dict)     # 本次被接受的会话值
    locked: list = field(default_factory=list)      # config 中为 false、会话不能打开的键
    source: dict = field(default_factory=dict)      # PANEL_KEYS → "session" | "config" | "default"
    rejected: list = field(default_factory=list)    # [{"what": "settings", "key", "reason"}]
    base: dict = field(default_factory=dict)        # PANEL_KEYS → 会话设置之前的 config 有效值


def _set(cfg: dict, key: str, value) -> None:
    sec, leaf = _split(key)
    cfg[sec][leaf] = deepcopy(value)


def apply_session_settings(config, session) -> SettingsView:
    """会话设置（mod 面板，§10.3）叠到 config 上：逐键校验、逐键覆盖（不深合并）。

    config 中有效值为 false 的 fine_rank / fine_supplement 被锁定：会话值 true 被拒（`locked`，§10.4）。
    session 不是 dict 时忽略（不算拒收）。不改入参。fail-open：异常时返回原 config 的视图、不应用会话值
    （`_failure_view`：effective / base / source 键集完整，session / locked / rejected 为空）。
    """
    try:
        cfg = deepcopy(config) if isinstance(config, dict) else deepcopy(DEFAULT_CONFIG)
        for sec in ("relevance", "user_prompt_submit"):
            if not isinstance(cfg.get(sec), dict):
                cfg[sec] = deepcopy(DEFAULT_CONFIG[sec])
        base = {k: get_value(cfg, k) for k in PANEL_KEYS}
        locked = [k for k in ("fine_rank", "fine_supplement") if base[k] is False]
        accepted: dict = {}
        rejected: list = []
        if isinstance(session, dict):
            for k, v in session.items():
                key = k if isinstance(k, str) else ""
                if key not in PANEL_KEYS:
                    rejected.append({"what": "settings", "key": key[:64], "reason": "unknown"})
                    continue
                ok, nv = validate_value(key, v)
                if not ok:
                    rejected.append({"what": "settings", "key": key, "reason": "bad_value"})
                    continue
                if key in locked and nv is True:
                    rejected.append({"what": "settings", "key": key, "reason": "locked"})
                    continue
                accepted[key] = nv
        effective = {**base, **accepted}
        for k in _ALL_KEYS:
            _set(cfg, k, effective[k] if k in effective else get_value(cfg, k))
        rel = cfg["relevance"]
        rel["session_topic"] = rel["fine_rank"]
        rel["session_pin_max"] = rel["fine_pin_max"]
        source = {k: "session" if k in accepted
                  else ("config" if base[k] != default_of(k) else "default") for k in PANEL_KEYS}
        return SettingsView(config=cfg, effective=effective, session=accepted, locked=locked,
                            source=source, rejected=rejected, base=base)
    except Exception as exc:  # noqa: BLE001 — 会话设置只是增强，绝不连累召回
        print(f"[vault-loader] 会话设置应用失败：{exc}", file=sys.stderr)
        return _failure_view(config)


def _failure_view(config) -> SettingsView:
    """异常兜底的 view：原 config、不应用会话值，但 effective / base / source 键集与正常路径相同，
    下游照常 `view.effective["fine_pin_max"]` 下标读取不会 KeyError。读 config 本身也可能出错（这就是
    我们在兜底的原因之一），所以再退一层：DEFAULT_CONFIG 的默认值。"""
    cfg = config if isinstance(config, dict) else deepcopy(DEFAULT_CONFIG)
    try:
        base = {k: get_value(cfg, k) for k in PANEL_KEYS}
        source = {k: "config" if base[k] != default_of(k) else "default" for k in PANEL_KEYS}
    except Exception:  # noqa: BLE001
        base = {k: default_of(k) for k in PANEL_KEYS}
        source = {k: "default" for k in PANEL_KEYS}
    return SettingsView(config=cfg, effective=dict(base), source=source, base=base)


def check_persist(key: str, value, view: SettingsView) -> "str | None":
    """「设为默认」只允许收窄（§10.4）：关开关、减篇数、模型改回 haiku；与 config 现值相同视为无变化、放行。

    判据是会话设置**之前**的 config 有效值（`view.base`），不是会话值——会话里放宽过的值不能借
    persist 写进 config。返回 None = 允许；否则 "bad_key" | "bad_value" | "widen"。
    """
    if key not in PANEL_KEYS:
        return "bad_key"
    ok, nv = validate_value(key, value)
    if not ok:
        return "bad_value"
    cur = (view.base or {}).get(key, get_value(view.config, key))
    if key in BOOL_KEYS:
        return None if (nv is False or nv == cur) else "widen"
    if key in _NARROW_INTS:
        return None if nv <= cur else "widen"
    if key == "fine_model":
        return None if nv in (DEFAULT_MODEL, cur) else "widen"
    return "bad_key"
