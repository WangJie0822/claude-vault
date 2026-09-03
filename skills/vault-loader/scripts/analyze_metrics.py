#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vault-loader 指标分析器。读本机 metrics，出报表；支持 near-miss 抽样标注。

报表可能被喂进模型上下文，而笔记路径是不可信外部输入 —— 故默认只显示路径 hash，
并在报表顶部带隔离声明。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

# 确保能 import 同级模块（direct `python analyze_metrics.py` 调用时 scripts/ 本身
# 不在 sys.path 上，需先把父目录 skills/vault-loader/ 插入，与 session_start_load.py/
# prompt_submit_load.py/migrate_config.py 同一约定）；pytest 场景下已由 rootdir 提供，
# insert 是幂等的。
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import _metrics
from scripts._entrypoint import INTERACTIVE_ENTRYPOINTS
from scripts._output import INJECTION_NOTICE, sanitize_injected_text


def load_records(home: Path) -> Iterator[dict]:
    """**流式**产出全部会话记录。坏行必须计数并经 stderr 报出，不能静默跳过。

    同一 session 被多进程并发写同一文件会产生撕裂行（实测丢失 31%~34%）——
    这是已知且刻意接受的限制（加锁的代价大于收益，且 metrics 是侧信道）。
    但「静默跳过」会让数据损坏完全不可见，与 prune_expired 走 stderr 的既有惯例
    也不一致。

    **返回生成器而非 list（P3 修复）**：旧实现把全部历史记录堆进一个 list 再返回，
    终审实测 90 session x 20 条宽记录（105 MB）耗时 **7032 ms、峰值内存 574 MB**
    （对照 30 session x 5 条窄记录仅 7.1 ms / 0.49 MB）。`--report`/`--review`
    是用户交互命令，随保留期内的使用量单调恶化且无自然上限。改成逐行 yield 后
    峰值只取决于「单条记录 + 消费端聚合器」，与语料总量无关。
    **逐行读而非 `read_text().splitlines()`**：后者仍会把整个文件读进内存，
    单个大文件就能把峰值顶上去，等于只流式了一半。

    **坏行汇总在迭代结束时报出**——生成器没有「返回前」这个时机。代价是消费端
    若提前 break 就收不到该提示；现有两个消费端（`summarize` / `sample_near_miss`）
    都会完整迭代，故不受影响。

    **只扫 `_metrics.event_month_dirs` 返回的真实事件目录，不用 `rglob("*.jsonl")`
    扫全树**（H1 修复）：旧实现会把顶层 `annotations.jsonl`（人工标注，`--review`
    产生，同样带 `_schema` 字段）当成事件文件一起统计——标注记录没有 `gate` 字段，
    `summarize()` 里 `gate[r.get("gate") or "ok"]` 会把每条标注都记成一次成功召回，
    用户越积极标注、报表越失真，与功能目的正相反。
    """
    skipped = 0
    coerced = 0
    dropped_ver = 0
    for d in _metrics.event_month_dirs(home):
        for f in sorted(d.glob("*.jsonl")):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:                      # 逐行，不把整个文件物化
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            skipped += 1
                            continue
                        if not isinstance(r, dict):
                            skipped += 1
                            continue
                        if r.get("_schema") not in SUPPORTED_SCHEMAS:
                            dropped_ver += 1
                            continue
                        coerced += _drop_bad_numeric_fields(r)
                        yield r
            except OSError:
                continue
    if skipped:
        print(f"[vault-loader] 跳过 {skipped} 行损坏记录"
              f"（同一 session 并发写会产生撕裂行，属已知限制）", file=sys.stderr)
    if coerced:
        print(f"[vault-loader] 丢弃 {coerced} 个非数值字段"
              f"（手工编辑或磁盘异常；该字段按缺失处理，记录其余部分照常统计）",
              file=sys.stderr)
    if dropped_ver:
        print(f"[vault-loader] 丢弃 {dropped_ver} 条 schema 版本不受支持的记录"
              f"（当前支持 {sorted(SUPPORTED_SCHEMAS)}）", file=sys.stderr)


# 受支持的落盘 schema 版本集合。**不是 `== _metrics.SCHEMA` 严格相等**：
#
# `_metrics.py` 的 schema 演进约定写着「bump 必须同步给 load_records 加一条
# 『丢弃了 N 条旧版本记录』的 stderr 提示」——这条同步工作从约定确立那天起就没做，
# 于是 bump 的实际代价被固定成「**静默**丢弃全部历史」。结果是一个自我强化的棘轮：
# 每次演进都只能再加一个可选字段绕开它，下次绕开的成本更低、修复的动机更弱，
# `SCHEMA` 永远停在 1，任何需要**语义变更**（而非加字段）的演进都无路可走。
#
# 改成受支持集合 + 计数报出之后：今天行为完全不变（集合只有 {1}），
# 但 bump 时只需往集合里加一个版本号，历史记录不再静默消失。
SUPPORTED_SCHEMAS = frozenset({1, 2})


# 顶层数值字段。值非数值时**删键**而不是置 0：下游用 `"inj_chars" in r` 判存在，
# 置 0 会把「这条记录写于加字段之前」和「它真的是 0」混成同一个值。
_NUMERIC_TOP_FIELDS = ("n_admitted", "inj_chars", "n_excluded", "ts")


def _drop_bad_numeric_fields(r: dict) -> int:
    """就地删掉值不可转成数的顶层字段，返回删除个数。

    **为什么必须有这一道**：`load_records` 原本只校验 `isinstance(r, dict)` 与
    `_schema` 相等，字段级类型一概不管，而消费侧到处是裸 `int()`/`float()`。
    实测把一条记录的 `inj_chars` 改成字符串，`--report` 直接 `exit=1`、
    **stdout 完全为空**——同文件里的合法记录连同上千条历史一起拿不到，用户看到的是
    Python 堆栈而不是「跳过 N 行损坏记录」。`--report` 恰恰是排障入口。

    同模块的 `load_annotations` 早已按「逐字段校验 + 计数 + stderr 报出」处理同类文件，
    这里补齐的是两套标准里缺的那半。撕裂行由 `JSONDecodeError` 分支接住，与本函数无关。
    """
    dropped = 0
    for k in _NUMERIC_TOP_FIELDS:
        if k not in r:
            continue
        v = r[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            # bool 是 int 的子类，但 `inj_chars: true` 显然是坏值，不该当 1 用
            del r[k]
            dropped += 1
    return dropped


class _Acc:
    """`summarize` 的累加器。

    **为什么拆成 `_Acc` + 三个 `_acc_*`**：拆之前 `summarize` 是单个 95 行、
    圈复杂度 37 的循环体，同时维护 6 个 Counter + 7 个标量。它是本文件复杂度
    第一（第二名 24），而本轮两处口径分叉（判据未走单点、两榜总体不一致）
    恰恰都发生在这个函数里 —— 可读性下降与缺陷密度在这里是因果关系，不是巧合。

    **不能拆成「各自遍历 records 的多个函数」**：`load_records` 返回生成器且
    刻意不物化（P3 修复：旧实现在 105 MB 语料上实测 7032 ms / 峰值 574 MB）。
    所以拆的是「每条记录如何贡献到累加器」，遍历仍然只有一趟。
    """
    __slots__ = ("arm", "gate", "near", "near_dedup", "suppressed",
                 "max_notes_seen", "src_dist", "n", "n_ok", "n_admitted",
                 "ft", "n_legacy_near", "inj_chars", "inj_n",
                 "dedup_full", "n_dedup_full", "n_dedup_legacy",
                 "n_pre_epoch", "ft_by_threshold", "ss_n", "ss_inj_chars")

    def __init__(self) -> None:
        self.arm: Counter = Counter()
        self.gate: Counter = Counter()
        self.near: Counter = Counter()
        self.near_dedup: Counter = Counter()
        self.suppressed: Counter = Counter()
        self.max_notes_seen: Counter = Counter()
        self.src_dist: Counter = Counter()
        self.n = self.n_ok = self.n_admitted = self.ft = 0
        self.n_legacy_near = self.inj_chars = self.inj_n = 0
        # 全量成因计数（新记录）与被截断窗口口径（旧记录）严格分开：混算既不是
        # 全量也不是窗口，而两者的 dedup 占比实测差一个数量级以上。
        self.dedup_full: Counter = Counter()
        self.n_dedup_full = self.n_dedup_legacy = self.n_pre_epoch = 0
        self.ft_by_threshold: dict = {}
        # SessionStart 通道单独计，绝不并入 UPS 的任何指标（见
        # _metrics.build_session_start_record 的隔离说明）。
        self.ss_n = self.ss_inj_chars = 0


def _acc_gate_and_admitted(r: dict, a: _Acc) -> None:
    """闸门分布、入选臂、候选池计数，以及两个自描述配置字段。"""
    g = r.get("gate") or "ok"
    a.gate[g] += 1
    if g == "ok":
        a.n_ok += 1

    # 来源分布。**这是黑名单判据的自观测**：`_is_human_src` 默认放行，若 harness
    # 日后开始下发一个新的自动化来源值（`agent` / `slash_command` / …），它会静默
    # 进入 `--review` 的人类标注池，没有任何信号。把分布打进报表，至少让「src 从
    # 恒空变成有值」这件事第一时间可见 —— 上一次正是因为没人看得见这个字段的真实
    # 取值，白名单判据才空转了整整 8 天没被发现。
    src = r.get("src")
    if isinstance(src, str):
        a.src_dist[src or "(空)"] += 1

    # 渲染层配置随记录落盘（自描述），报表据此说「实际渲染几篇」而不是硬编码默认值。
    # 用 Counter 而非取单值：该配置可能在样本期内被改过，多值时报表要如实说明。
    mn = r.get("max_notes")
    if isinstance(mn, int) and not isinstance(mn, bool) and mn > 0:
        a.max_notes_seen[mn] += 1

    if "n_admitted" in r and "arm_counts" in r:
        a.n_admitted += int(r.get("n_admitted") or 0)   # 顶层字段已由 load_records coerce
        ac = r.get("arm_counts")
        # arm_counts 是嵌套 dict，不在 _NUMERIC_TOP_FIELDS 的覆盖范围内，
        # 单独兜住：非 dict 直接跳过，值非数值当 0（该臂这一轮不计）。
        for arm_name, cnt in (ac if isinstance(ac, dict) else {}).items():
            if isinstance(cnt, bool) or not isinstance(cnt, (int, float)):
                continue
            a.arm[arm_name if isinstance(arm_name, str) and arm_name else "?"] += int(cnt)
    else:
        for x in r.get("admitted") or []:
            if not isinstance(x, dict):
                continue
            a.arm[x.get("arm") or "?"] += 1
            a.n_admitted += 1


def _acc_cost(r: dict, a: _Acc) -> None:
    """代价面：全文注入次数与注入正文字符数。"""
    if (r.get("ft") or {}).get("path"):
        a.ft += 1
    # 注入正文长度：**用 `in` 判存在而非 `.get(..., 0)`** —— 后者会把「写于加
    # 字段之前的旧记录」与「本轮真的零注入」混成同一个 0，把均值系统性拉低。
    # 与 `_acc_near_miss` 对 dedup 的三态处理同一惯例。
    if "inj_chars" in r:
        a.inj_chars += int(r.get("inj_chars") or 0)
        a.inj_n += 1


def _acc_near_miss(r: dict, a: _Acc) -> None:
    """三组 near-miss 统计。**三者的总体口径在这一个函数里说清**，别再分散。

    - `near`（真·擦肩榜）：只认新格式记录，且走 `scorelow_entries` 施加 topical 下限
    - `suppressed`（对照榜）：同样只认新格式记录 —— 两榜并排才当得起「对照」
    - `near_dedup`（成因分布）：**刻意保持全量口径**，它回答「excluded 都因为什么
      落榜」，本就该看全部数据；这个不对称由报表文案标明
    """
    # 榜单只认生成侧已排除去重条目的 near_miss_scorelow；旧记录只计数、不入榜。
    # 理由见 _metrics.scorelow_entries：旧记录的 near_miss 样本在 top-k 截断阶段
    # 就被高 topical 的去重条目挤占（39% 的轮次一条 score-low 都没留下），
    # 对它做过滤只会得到有系统性偏斜的残差。
    #
    # **必须走 `scorelow_entries` 而不是内联读裸键**：上一版这里内联了一份，
    # 于是 NUDGE_TOPICAL_FLOOR 在榜单上完全失效——topical=0 的笔记照样进榜，
    # 而榜首表头写的正是「调 tags/keywords 可能救回来」。同一个抽象被声明为
    # 「三个消费者的单点」却只有 1/3 在用，这是本轮 High finding 的成因。
    # 全量成因计数优先：`near_miss` 是按 topical 降序取 top-k 的截断样本
    # （实测采样率 1.38%、100% 的轮次都在截断），而 dedup 条目 topical 天然更高，
    # 于是窗口内 dedup 占比 45% vs 真实量级约 1%。有 dedup_counts 就用它。
    dc = r.get("dedup_counts")
    if isinstance(dc, dict):
        a.n_dedup_full += 1
        for _k, _v in dc.items():
            if isinstance(_v, int) and not isinstance(_v, bool) and isinstance(_k, str):
                a.dedup_full[_k] += _v      # 原样保留写端 key，标签映射在渲染层
    elif r.get("near_miss"):
        a.n_dedup_legacy += 1

    is_new = isinstance(r.get("near_miss_scorelow"), list)
    if is_new:
        for p, _t in _metrics.scorelow_entries(r):
            a.near[p] += 1
    elif r.get("near_miss"):
        a.n_legacy_near += 1

    for nm in r.get("near_miss") or []:
        if not isinstance(nm, dict):
            continue              # 手工改坏的 jsonl：跳过该条，不让 CLI 崩
        # 三态必须分清：缺键=写于加字段之前；""=打分不够；其余=被去重抑制
        # （即其实已经成功召回过，不该算「擦肩而过」）。用 `in` 而非真值判断
        # ——"" 是合法取值，真值判断会把它并进「未知」。
        if "dedup" not in nm:
            a.near_dedup["未知(旧记录)"] += 1
            continue
        d = nm["dedup"]
        if not isinstance(d, str):
            continue              # dedup 被改成 list/dict 会让 Counter 抛 unhashable
        a.near_dedup[d or "打分不够"] += 1
        # **对照榜收敛到新记录**：它与真·擦肩榜并排渲染、表头写着「对照」，读者会拿
        # 两栏计数直接相比。而真·擦肩榜排除旧记录、对照榜若跨新旧就是
        # apples-to-oranges —— 真实数据上更极端：新格式记录为 0 时一栏全空、另一栏
        # 全满，读者的自然结论「我没有擦肩笔记」恰好是错的。
        if is_new and d and isinstance(nm.get("path"), str) and nm["path"]:
            a.suppressed[nm["path"]] += 1


def _acc_epoch_and_threshold(r: dict, a: _Acc) -> None:
    """埋点时代切分 + 全文注入率按阈值制度分组。

    **时代判据**：有 `src` 键（走到打分的新记录）或 gate 非空（被拦的新记录）。
    gate 埋点上线之前，被闸门拦下的轮次**一条都不落盘**，把那段时期的记录计入
    分母会系统性抬高「走到打分」的占比（真实语料实测 +6.5 个百分点）。不能只判
    `"src" in r`——被拦的新记录只写 5 个键、不含 src。
    实测该组合判据在 1625 条真实记录上仅 2 条边界模糊（0.15%，0.9.0 部署过渡期）。

    **阈值分组**：`fulltext_topical_threshold` 在样本期内由 6 改成 10，两期实测
    全文注入率 65.4% vs 40.1%，而合并值 45.9% 不描述任何一个时期。
    """
    if not (("src" in r) or r.get("gate")):
        a.n_pre_epoch += 1
    if r.get("gate"):
        return                      # 被拦的轮次不参与全文注入率
    ftt = r.get("ft_topical")
    key = f"{float(ftt)}" if isinstance(ftt, (int, float)) and not isinstance(
        ftt, bool) else "(未记录)"
    slot = a.ft_by_threshold.setdefault(key, [0, 0])
    slot[0] += 1
    if isinstance(r.get("ft"), dict) and r["ft"].get("path"):
        slot[1] += 1


def _acc_to_dict(a: _Acc) -> dict:
    return {
        "n_events": a.n, "n_ok": a.n_ok, "n_admitted": a.n_admitted,
        "ss_n": a.ss_n, "ss_inj_chars": a.ss_inj_chars,
        "dedup_full": dict(a.dedup_full), "n_dedup_full": a.n_dedup_full,
        "n_dedup_legacy": a.n_dedup_legacy, "n_pre_epoch": a.n_pre_epoch,
        "ft_by_threshold": a.ft_by_threshold,
        "arm_dist": dict(a.arm), "gate_dist": dict(a.gate),
        # 分母收敛到 gate=="ok"：闸门早退的轮次压根没走到全文判定，把它们计进
        # 分母会稀释出一个既不是「注入率」也不是「命中率」的数（实测 36.2% vs 48.7%）。
        "fulltext_rate": (a.ft / a.n_ok) if a.n_ok else 0.0,
        "n_fulltext": a.ft,
        "inj_chars_total": a.inj_chars, "inj_chars_n": a.inj_n,
        "near_miss_top": a.near.most_common(20),
        "near_miss_suppressed_top": a.suppressed.most_common(10),
        "n_legacy_near_records": a.n_legacy_near,
        "max_notes_dist": dict(a.max_notes_seen),
        "src_dist": dict(a.src_dist),
        "near_miss_dedup_dist": dict(a.near_dedup),
    }


def summarize(records: Iterable[dict]) -> dict:
    """聚合报表。

    **`n_admitted`/`arm_dist` 优先读落盘的 `n_admitted`/`arm_counts` 标量与字典**
    （P1 修复：`build_record` 现在把 `admitted` 数组截断到 `admitted_k` 条展示样本，
    若仍靠遍历 `r["admitted"]` 累加，截断后的记录会把真实计数**低报**成截断后的
    条数）。这两个字段是 `build_record` 在截断**之前**算好的，覆盖截断造成的信息
    损失。**旧记录**（截断改动前落盘、没有这两个字段）回退到遍历 `admitted` 数组
    ——彼时 `admitted` 未截断，等价于全量，遍历口径与新字段口径一致，不会算错。
    """
    a = _Acc()
    for r in records:        # 单趟、不物化：records 是生成器（P3 修复的硬约束）
        if r.get("channel") == "session_start":
            # 另一条通道：只累加它自己的两个数，**不进** UPS 的任何统计。
            # 缺 channel 键的旧记录落到下面，按 UPS 处理（向后兼容）。
            a.ss_n += 1
            try:
                a.ss_inj_chars += int(r.get("inj_chars") or 0)
            except (TypeError, ValueError):
                pass
            continue
        a.n += 1
        _acc_gate_and_admitted(r, a)
        _acc_cost(r, a)
        _acc_near_miss(r, a)
        _acc_epoch_and_threshold(r, a)
    return _acc_to_dict(a)


def _stable_path_id(p: str) -> str:
    """跨进程稳定的路径占位 ID，供 `render_report` 默认（隐去路径）分支展示。

    **不用内建 `hash()`**：CPython 对 str 默认开启 hash 随机化
    （`PYTHONHASHSEED` 每进程各异），同一路径在两次独立 `--report` 调用中会显示
    不同 ID，无法跨报表对照，也会让 Task 12 `--review` 的按 ID 标注对不上对象
    （实证：coordinator 两个独立进程对同一路径分别得到 `52960092`/`78863186`）。

    改用 `hashlib.sha1` 前 8 位十六进制——跨进程/跨机器确定性输出，足够避免
    展示层面的常见碰撞。**刻意不加盐**：这个 ID 只是显示占位，不承担隐私职责
    ——真正的隐私边界在 `--show-paths` 开关（默认隐藏、显式选择才展开明文），
    加盐反而会让同一笔记在启用/未启用某次 salt 轮转前后显示不一致，无实际收益。
    """
    return f"#{hashlib.sha1(p.encode('utf-8')).hexdigest()[:8]}"


def _shown_path(p: str, show_paths: bool) -> str:
    """路径的展示形态 —— 所有榜单必须共用这一个出口。

    默认隐去为稳定 ID；`--show-paths` 才展开明文，且**必须**过 `sanitize_injected_text`
    ——报表可能被喂进模型上下文，而笔记路径是不可信外部输入（见模块 docstring）：
    未净化的 path 可嵌换行/控制字符伪造报表行。新增榜单直接 f-string 拼 path 会同时
    退化隐私默认值与净化，两处都无声。
    """
    return sanitize_injected_text(p, keep_newlines=False)[:80] if show_paths \
        else _stable_path_id(p)


def _render_span(s: dict) -> str:
    """「实际渲染几篇」的文案 —— 按记录里落盘的 `max_notes` 真值，不硬编码默认值。

    ⚠️ 这里此前写死「实际渲染 ≤4 篇」，**两处都错**：
    ① 全文那篇是**计入** `max_notes` 之内的（`prompt_submit_load.py:341` 是
       `rest = [...][: max_notes - 1]`，加上 ft 恰好 max_notes 篇），
       写成「max_notes 条清单 + 至多 1 篇全文」等于把全文额外加了一次；
    ② `max_notes` 可配（`_config_loader.py:28` 默认 3），硬编码 3 或 4 都会在用户
       改配置后开始说假话 —— 而这一行恰恰是「口径订正」那笔改动的招牌行。
    """
    dist = s.get("max_notes_dist") or {}
    if not dist:
        return "实际渲染篇数见配置 user_prompt_submit.max_notes —— 旧记录未落该字段"
    mn = max(dist, key=lambda k: dist[k])       # 最常见值
    if len(dist) > 1:
        return (f"实际渲染 ≤{mn} 篇（含全文那篇；样本期内该配置变动过："
                f"{dict(sorted(dist.items()))}）")
    return f"实际渲染 ≤{mn} 篇（含全文那篇，不是额外加一篇）"


def render_report(s: dict, show_paths: bool = False) -> str:
    if not s.get("n_events"):
        # 无数据时早退：否则会打出「新口径只统计本版之后落盘的记录」这类
        # 暗示「你有数据、只是太旧」的解释语，以及零路径时毫无意义的
        # 「路径已隐去」——两句都在把「没开」误导成「开了但没内容」。
        return "\n".join([
            INJECTION_NOTICE, "", "📊 vault-loader 指标报表", "",
            "无数据。metrics 默认关闭 —— 在 config.json 设 `metrics.enabled: true` "
            "后再提问若干轮，再回来看这份报表。"])

    n_ok = s.get("n_ok", 0)
    inj_n, inj_total = s.get("inj_chars_n", 0), s.get("inj_chars_total", 0)
    lines = [INJECTION_NOTICE, "", "📊 vault-loader 指标报表", "",
             # 「累计入选」曾被读成「注入了这么多篇」。它是**候选池**累计：
             # 真实数据 mean 73.1 / median 62.0 篇/轮（下面这行算的是 mean），
             # 而真正渲染给用户的只有 max_notes 篇（见 _render_span）。
             f"事件数 {s['n_events']}（走到打分 {n_ok}） · "
             f"候选池累计 {s['n_admitted']} 篇"
             + (f"（均 {s['n_admitted'] / n_ok:.0f} 篇/轮，{_render_span(s)}）"
                if n_ok else ""), ""]
    lines.append(f"全文注入 {s.get('n_fulltext', 0)} 次 = 走到打分轮次的 "
                 f"{s['fulltext_rate']:.1%}")
    if inj_n:
        # 标明「UPS」：SessionStart 通道完全不落 metrics（该文件对 _metrics 引用数为 0），
        # 不标注的话读者会把这个数当成 vault-loader 的注入开销总量。
        lines.append(f"UPS 注入正文 {inj_total:,} 字符 / {inj_n} 轮 "
                     f"= 均 {inj_total / inj_n:.0f} 字符每轮"
                     + ("" if s.get("ss_n") else
                        "（不含 SessionStart 通道，它不落 metrics）"))
    if s.get("ss_n"):
        _ss_avg = s["ss_inj_chars"] / s["ss_n"] if s["ss_n"] else 0
        lines.append(f"SessionStart 注入 {s['ss_inj_chars']:,} 字符 / "
                     f"{s['ss_n']} 次会话 = 均 {_ss_avg:,.0f} 字符每次")
    else:
        lines.append("UPS 注入正文字符数: 无数据（该字段自 0.9.1 起记录，旧记录没有）")
    # 来源分布：黑名单判据的自观测出口。`_is_human_src` 默认放行，所以「harness 开始
    # 下发一个新的自动化来源值」这件事本身是静默的 —— 打出来至少让它可见。
    # 目前实测恒为 `(空)`（promptSource 不在 hook stdin payload 里）。
    src_dist = s.get("src_dist") or {}
    if src_dist and set(src_dist) != {"(空)"}:
        lines.append(f"来源分布: {src_dist}"
                     f"（非 {list(_NON_HUMAN_SRC)} 的值都会进 --review 的人类标注池）")
    _n_full, _n_leg = s.get("n_dedup_full", 0), s.get("n_dedup_legacy", 0)
    if _n_full:
        _cov = _n_full / (_n_full + _n_leg) * 100 if (_n_full + _n_leg) else 100.0
        lines.append("")
        _shown = {("打分不够" if not _k else _k): _v
                  for _k, _v in (s.get("dedup_full") or {}).items()}
        lines.append(f"excluded 成因分布（全量口径，覆盖 {_n_full}/{_n_full + _n_leg} "
                     f"= {_cov:.1f}% 的记录）: {_shown}")
        if _n_leg:
            lines.append(f"  ⚠️ 另有 {_n_leg} 条旧记录只有被截断的 top-k 窗口样本，"
                         f"其 dedup 占比会被结构性放大，未并入上行")
    _ftt = s.get("ft_by_threshold") or {}
    if len(_ftt) > 1:
        lines.append("")
        lines.append("全文注入率按阈值分期（合并值跨制度、不描述任何一个时期）:")
        for _k in sorted(_ftt, key=lambda x: (x == "(未记录)", x)):
            _rounds, _n = _ftt[_k]
            _pct = _n / _rounds * 100 if _rounds else 0.0
            lines.append(f"    阈值 {_k}: {_n}/{_rounds} = {_pct:.1f}%")
    lines += ["", f"闸门分布: {s['gate_dist']}", f"入选臂分布: {s['arm_dist']}",
              # 成因分布是**全量口径**（含旧记录），与下面两榜的总体不同 —— 不标注的话
              # 读者会把三组数当成同一批样本的切分。且其「打分不够」桶正是本轮论证为
              # 「有系统性偏斜」的那批残差（旧记录的 near_miss 样本被去重条目挤占）。
              f"near-miss 成因分布（**被截断的 top-k 窗口口径**，非全量 excluded；"
              f"共 {sum((s.get('near_miss_dedup_dist') or {}).values())} 个条目）: "
              f"{s.get('near_miss_dedup_dist', {})}",
              "  ⚠️ 该窗口按 topical 降序取样，而去重条目的 topical 天然更高"
              "（不看分就被排除），故其中 dedup 类占比被结构性放大"
              + ("；真实占比以上方「全量口径」行为准"
                 if s.get("n_dedup_full") else
                 "；当前全部记录都是旧格式，没有可用的全量口径数据"),
              ""]
    if s.get("n_pre_epoch"):
        lines += [f"⚠️ {s['n_pre_epoch']} 条记录（共 {s['n_events']} 条）产自 gate 埋点前，"
                  f"那时被闸门拦下的轮次一条都不落盘 —— 上面「走到打分」的占比因此被"
                  f"系统性抬高，按同时代口径重算才可比", ""]

    legacy = s.get("n_legacy_near_records", 0)
    lines.append("near-miss · 真·擦肩（够不着精度闸门，调 tags/keywords 可能救回来）:")
    for p, c in s.get("near_miss_top", []):
        lines.append(f"  {c:>4} 次  {_shown_path(p, show_paths)}")
    if not s.get("near_miss_top"):
        lines.append("  （无。新口径只统计本版之后落盘的记录）")
    if legacy:
        lines.append(f"  ⚠️ 另有 {legacy} 条旧记录未纳入本榜——它们落盘时未区分成因，"
                     f"且样本已被去重条目挤占，过滤只会得到偏斜的残差")

    sup = s.get("near_miss_suppressed_top", [])
    if sup:
        lines.append("")
        # 两榜同源（都只统计新格式记录），才当得起「对照」二字 —— 见 summarize 里
        # suppressed 的 `is_new` 条件。
        #
        # ⚠️ 但「同源」只保证了**记录集**相同，不保证**采样窗口**相同，而两栏的
        # `N 次` 正是从两个饱和度截然不同的窗口里数出来的（真实语料实测：
        # near_miss 窗口 314/314 = 100% 恒满，near_miss_scorelow 仅 33/314 = 11%）。
        # 于是本榜的 N 实际含义是「该笔记在多少轮里挤进了 top-k 窗口」——被窗口
        # 硬性截断的下界；而上榜的 N 才接近真实出现次数。两个量纲不同的数并排
        # 渲染成同一种「N 次」，读者会直接比大小。
        lines.append("对照 · 被去重抑制（其实已经成功召回过，**不是**漏召回）:")
        lines.append("  ⚠️ 本栏 N 是「挤进 top-k 窗口的轮次数」，受窗口截断影响、是下界；"
                     "与上一栏的 N 量纲不同，两栏不可直接比大小")
        for p, c in sup:
            lines.append(f"  {c:>4} 次  {_shown_path(p, show_paths)}")
    if not show_paths and (s.get("near_miss_top") or sup):
        lines.append("  （路径已隐去，加 --show-paths 展开）")
    return "\n".join(lines)


VERDICTS = ("relevant", "irrelevant", "unsure")

# 三类标注：near_miss 问「该不该被召回」；两个 admitted_* 问「召回得对不对」。
# 分开是因为代价不对等——全文注入单篇上限 8192 字节，清单条目只有一行 summary，
# 混成一个桶之后「全文错了 30%」与「清单错了 30%」在数据里长得一模一样。
KINDS = ("near_miss", "admitted_fulltext", "admitted_list")
DEFAULT_KIND = "near_miss"   # 旧记录（写于加字段之前）一律归此类


def _transcript_index(home: Path) -> dict[str, Path]:
    """session_id -> transcript 文件。只扫一次，`--review` 全程复用。

    transcript 落在 `~/.claude/projects/<项目目录编码>/<session_id>.jsonl`，
    文件名即 session_id —— 所以不需要把 `transcript_path` 落进 metrics
    （那个路径含项目目录名，而 `cwd` 特意只落了 hash，落它是隐私回退）。
    实测本机 387/388 = 99.7% 的 metrics session 能在这里找到对应文件。
    """
    idx: dict[str, Path] = {}
    root = home / ".claude" / "projects"
    if not root.is_dir():
        return idx
    try:
        for d in root.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                idx.setdefault(f.stem, f)
    except OSError:
        pass
    return idx


def _iter_user_texts(f: Path):
    """产出 transcript 里每条 user message 的纯文本。坏行跳过，不抛。"""
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict) or o.get("type") != "user":
                    continue
                msg = o.get("message")
                c = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(c, list):
                    text = " ".join(x.get("text", "") for x in c
                                    if isinstance(x, dict) and x.get("type") == "text")
                elif isinstance(c, str):
                    text = c
                else:
                    continue
                if text:
                    yield text
    except OSError:
        return


def lookup_prompt(home: Path, session: str, prompt_h: str, salt: bytes,
                  index: dict[str, Path] | None = None) -> str:
    """按 (session_id, prompt_h) 从 transcript 精确取回 prompt 原文。取不到返回 ""。

    **这是标注能否成立的前提**：没有它，`--review` 只能显示「这篇被召回 N 次」，
    而「该不该被召回」这个问题在不知道当时问了什么时无法回答。

    精确匹配而非时间戳就近：对每条 user message 用**同一个盐**算 `_metrics.h`，
    与落盘的 `prompt_h` 比对。时间戳方案实测只有 82.8% 能唯一定位，剩下 17.2%
    落在「同一秒多条消息」的歧义里 —— 人工标注不可再生，不能建在猜上。

    隐私：prompt 原文始终只存在于 transcript（本来就在本机、本来就是全文），
    metrics 侧只有 hash。本函数在标注时**读**它，不写任何新文件。
    """
    if not (session and prompt_h):
        return ""
    idx = _transcript_index(home) if index is None else index
    f = idx.get(session)
    if f is None:
        return ""
    for text in _iter_user_texts(f):
        if _metrics.h(text, salt) == prompt_h:
            return text
    return ""


def sample_admitted_events(records: Iterable[dict], path: str, kind: str,
                           limit: int = 3) -> list[tuple[str, str]]:
    """取该笔记被召回的前 `limit` 次事件的 (session, prompt_h)，供 --review 展示上下文。

    只取前几次而不是全部：标注者需要的是「几个有代表性的场景」，
    63 次全列出来既读不完，也会把终端刷屏。
    """
    out: list[tuple[str, str]] = []
    for r in records:
        ph = r.get("prompt_h")
        s = r.get("session")
        if not (isinstance(ph, str) and ph and isinstance(s, str) and s):
            continue
        hit = False
        if kind == "admitted_fulltext":
            hit = (r.get("ft") or {}).get("path") == path
        elif kind == "admitted_list":
            hit = any(isinstance(a, dict) and a.get("path") == path
                      for a in (r.get("admitted") or []))
        else:                                     # near_miss
            hit = any(isinstance(nm, dict) and nm.get("path") == path
                      for nm in (r.get("near_miss_scorelow") or []))
        if hit:
            out.append((s, ph))
            if len(out) >= limit:
                break
    return out


REPLACEMENT_CHAR = "\ufffd"


def sample_events_with_hits(records: Iterable[dict], path: str, kind: str,
                            limit: int = 12) -> list[tuple[str, str, list[str]]]:
    """同 `sample_admitted_events`，但附带该笔记当轮的命中词。

    **limit 默认给得比展示条数大**：`pick_readable_contexts` 要在其中筛可读的，
    而实测约 45.7% 的上下文行不可读（33.3% 乱码 + 12.4% 回查不到），候选池按 3
    取会经常挑不满。

    `hits` 只在 `admitted` 数组里有真实值——`_decision` 对 excluded 条目的
    hits 是**未计算的占位值** `[]`（性能护栏），所以 near_miss 一律返回空列表，
    不假装有。
    """
    out: list[tuple[str, str, list[str]]] = []
    for r in records:
        ph, s = r.get("prompt_h"), r.get("session")
        if not (isinstance(ph, str) and ph and isinstance(s, str) and s):
            continue
        if kind == "admitted_fulltext":
            hit = (r.get("ft") or {}).get("path") == path
        elif kind == "admitted_list":
            hit = any(isinstance(a, dict) and a.get("path") == path
                      for a in (r.get("admitted") or []))
        else:
            hit = any(isinstance(nm, dict) and nm.get("path") == path
                      for nm in (r.get("near_miss_scorelow") or []))
        if not hit:
            continue
        hits: list[str] = []
        for a in (r.get("admitted") or []):
            if isinstance(a, dict) and a.get("path") == path:
                h = a.get("hits")
                if isinstance(h, list):
                    hits = [x for x in h if isinstance(x, str)]
                break
        out.append((s, ph, hits))
        if len(out) >= limit:
            break
    return out


def pick_readable_contexts(events, resolve, want: int = 3):
    """从候选事件里挑出**可读**的前 want 条，并分类统计不可读的成因。

    `resolve(session, prompt_h) -> str` 由调用方注入（生产传 `lookup_prompt`），
    使本函数保持可测的纯逻辑。

    两类不可读必须分开计数——它们的成因与可处置性完全不同，而此前被同一句
    「（transcript 里找不到，可能已被清理）」一并解释掉了：
    - `corrupt`：transcript 里存的就是 U+FFFD，写入方在存盘前已解码坏，不可逆；
    - `unresolved`：hash 对不上。实测 50 条里 48 条 transcript 就在盘上，
      真实原因是 hook 拿到原始 prompt、transcript 存的是 slash/skill 展开后的形态。

    凑够 want 条即停：每条都要回查一次 transcript，不能白扫。

    第三个返回值 `picked` 是**被选中那几条**的 `(session, prompt_h)`，与 `items`
    一一对应、顺序一致，供 `save_annotation` 落盘 —— 标注若不带 query 关联，
    就算不出任何排序指标（实测：已有 74 条标注正因缺这个而无法用作 ground truth）。
    只交出标识符、不交出原文：原文按需回查，落盘一份就等于作废「prompt 原文不落盘」契约。
    """
    items: list[tuple[str, list[str]]] = []
    picked: list[tuple[str, str]] = []
    reasons = {"corrupt": 0, "unresolved": 0}
    for s, ph, hits in events:
        if len(items) >= want:
            break
        text = resolve(s, ph)
        if not text:
            reasons["unresolved"] += 1
        elif REPLACEMENT_CHAR in text:
            reasons["corrupt"] += 1
        else:
            items.append((" ".join(text.split())[:160], hits))
            picked.append((s, ph))
    return items, reasons, picked


def format_context_lines(items, reasons) -> list[str]:
    """渲染标注上下文块。归因必须如实分类，见 `pick_readable_contexts`。"""
    lines: list[str] = []
    if items:
        lines.append("  ── 该笔记被召回时，你问的是 ──")
        for i, (text, hits) in enumerate(items, 1):
            lines.append(f"   {i}. {sanitize_injected_text(text, keep_newlines=False)}")
            if hits:
                # 命中词是「这篇为什么被召回」的直接答案，且早已明文落盘——
                # 在此之前 analyze_metrics 对它的引用数是 0，隐私代价白付了。
                shown = "、".join(sanitize_injected_text(h, keep_newlines=False)
                                  for h in hits[:8])
                lines.append(f"      命中词：{shown}")
    n_c = reasons.get("corrupt", 0)
    n_u = reasons.get("unresolved", 0)
    if n_c:
        lines.append(f"  ⚠️ 另有 {n_c} 轮的提问在写入 transcript 时就已编码损坏"
                     f"（含替换字符），原文不可恢复")
    if n_u:
        lines.append(f"  ⚠️ 另有 {n_u} 轮回查不到：hook 收到的是你敲的原文，而 "
                     f"transcript 存的是 slash command / skill 展开后的形态，"
                     f"两者 hash 对不上（**不是**历史被清掉了）")
    if not items and not (n_c or n_u):
        lines.append("  ── 无提问上下文（该条目写于 prompt_h 落盘之前）──")
    return lines


def session_entrypoint(session: str, tindex: dict) -> str:
    """按 session 从 transcript 取 entrypoint。取不到返回 ""。"""
    p = tindex.get(session)
    if p is None:
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 200:
                    return ""
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "user":
                    ep = rec.get("entrypoint")
                    return ep if isinstance(ep, str) else ""
    except OSError:
        return ""
    return ""


def human_records(records: Iterable[dict], tindex: dict) -> Iterator[dict]:
    """只放行人类交互会话的记录，供 `--review` 的抽样使用。

    **为什么写端闸门不够**：`_entrypoint.is_supported_session()` 只挡新产生的记录，
    而标注池吃的是历史语料 —— 实测 1103 个打分轮次里 35.3% 产自 `claude -p` 一类的
    程序化调用。用户实测反馈标注界面混进了不是他敲的内容（「先用 Bash 工具运行
    echo MODEL=…」），取证确认那条来自 `entrypoint='sdk-cli'` 的会话。拿派发指令去问
    「这篇该不该被召回」毫无意义，而人工标注不可再生。

    **未知来源一律保留**，与写端闸门同向：宁可漏禁，不可误删。误删一条人类样本的
    代价高于混进一条程序化样本，而 transcript 可达率实测 98.9%。

    判据常量从 `_entrypoint` 导入而非内联 —— 两处判据漂移是本仓库反复吃过亏的形态。
    """
    cache: dict[str, str] = {}
    for r in records:
        s = r.get("session")
        if not isinstance(s, str) or not s:
            yield r
            continue
        if s not in cache:
            cache[s] = session_entrypoint(s, tindex)
        ep = cache[s]
        if ep and ep not in INTERACTIVE_ENTRYPOINTS:
            continue
        yield r


def attach_contexts(items, records_factory, resolve, want: int = 3):
    """给每个待标注条目附上可读的提问上下文；**一条都读不出来的直接剔除**。

    用户实测反馈：标注界面里出现「无提问上下文」的条目，只能盲标 unsure。这类
    条目对精度评估零贡献，却占满标注池的名额——而人工标注是这套机制里唯一不可
    再生的数据，名额该让给能判断的条目。

    `records_factory` 必须是**可重复调用**的工厂：`load_records` 是生成器、只能
    迭代一次，每个条目都要重新扫一遍（刻意不物化，那会重演已修的 574MB 峰值）。

    预取还顺带消掉一次重复扫描：此前 `--review` 构造 todo 时扫一遍、显示上下文时
    又扫一遍。
    """
    out = []
    for it in items:
        events = sample_events_with_hits(records_factory(), it["path"],
                                         it.get("kind", "near_miss"))
        ctxs, reasons, picked = pick_readable_contexts(events, resolve, want=want)
        if not ctxs:
            continue
        enriched = dict(it)
        enriched["contexts"] = ctxs
        enriched["unreadable"] = reasons
        # 标注落盘用：只带标识符，原文留在 contexts 里供展示、不落盘
        enriched["context_ids"] = picked
        out.append(enriched)
    return out


def annotations_path(home: Path) -> Path:
    return _metrics.metrics_dir(home) / "annotations.jsonl"


REVIEW_CSS = """
:root{--bg:#fbfbfa;--fg:#23221f;--mut:#6b6862;--line:#e3e0da;--card:#fff;
--ok:#2f7d5b;--no:#b4453a;--un:#8a7a3d;--acc:#3a5c9e}
@media(prefers-color-scheme:dark){:root:not([data-t=l]){--bg:#1a1a19;--fg:#e8e6e1;
--mut:#9b968d;--line:#33322e;--card:#232320;--ok:#5cb98d;--no:#e08076;--un:#c4ae63;
--acc:#7ba0dd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:24px 16px 96px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:10px 0;margin-bottom:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar button{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);cursor:pointer}
.bar button:hover{border-color:var(--acc)}
.prog{margin-left:auto;color:var(--mut);font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:12px}
.card.done{border-color:var(--acc)}
.path{font-weight:600;word-break:break-all}
.meta{color:var(--mut);font-size:12px;margin:2px 0 10px}
.tag{display:inline-block;padding:1px 7px;border:1px solid var(--line);border-radius:99px;
font-size:11px;margin-right:6px}
.ctx{border-left:2px solid var(--line);padding:2px 0 2px 10px;margin:8px 0}
.ctx p{margin:0 0 2px;word-break:break-word}
.hits{color:var(--mut);font-size:12px}
.hits b{color:var(--acc);font-weight:600}
.warn{color:var(--un);font-size:12px;margin-top:6px}
.opts{display:flex;gap:6px;margin-top:12px;flex-wrap:wrap}
.opts label{padding:5px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;
font-size:13px;user-select:none}
.opts input{margin-right:5px}
.opts input:checked+span{font-weight:600}
.opts label:has(input[value=relevant]:checked){border-color:var(--ok);color:var(--ok)}
.opts label:has(input[value=irrelevant]:checked){border-color:var(--no);color:var(--no)}
.opts label:has(input[value=unsure]:checked){border-color:var(--un);color:var(--un)}
.foot{position:fixed;left:0;right:0;bottom:0;background:var(--card);
border-top:1px solid var(--line);padding:12px 16px;display:flex;justify-content:center;gap:12px}
.foot button{font:inherit;font-weight:600;padding:9px 22px;border-radius:8px;
border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--mut);
border-radius:6px;padding:10px 14px;margin-bottom:18px;color:var(--mut);font-size:13px}
"""


def _esc(s) -> str:
    import html as _h
    return _h.escape("" if s is None else str(s), quote=True)


def build_review_html(items) -> str:
    """渲染标注页面。**所有外部内容都必须转义**。

    页面嵌入两类不可信内容：笔记路径（来自 Vault）与提问原文（来自 transcript）。
    二者都可能含 HTML 元字符，不转义就是注入面。内嵌 JSON 另外要把 `</` 打断，
    否则 `</script>` 会提前闭合标签、后面的内容被当 HTML 解析。

    表单本身零 JS 依赖（普通 POST）；JS 只做批量设置与进度显示，禁用了也能提交。
    """
    import json as _json

    cards = []
    for i, it in enumerate(items):
        ctx_html = []
        for text, hits in it.get("contexts") or []:
            hit_html = ""
            if hits:
                hit_html = ('<div class="hits">命中词：'
                            + "、".join(f"<b>{_esc(h)}</b>" for h in hits[:8])
                            + "</div>")
            ctx_html.append(f'<div class="ctx"><p>{_esc(text)}</p>{hit_html}</div>')
        un = it.get("unreadable") or {}
        warn = ""
        bits = []
        if un.get("corrupt"):
            bits.append(f"{un['corrupt']} 轮的提问在写入 transcript 时已编码损坏")
        if un.get("unresolved"):
            bits.append(f"{un['unresolved']} 轮回查不到（slash/skill 展开后 hash 对不上）")
        if bits:
            warn = f'<div class="warn">⚠️ 另有 {"；".join(_esc(b) for b in bits)}</div>'
        opts = "".join(
            f'<label><input type="radio" name="v{i}" value="{v}">'
            f'<span>{lbl}</span></label>'
            for v, lbl in (("relevant", "相关"), ("irrelevant", "不相关"),
                           ("unsure", "不确定")))
        cards.append(f"""<div class="card" data-i="{i}">
<div class="path">{_esc(it.get("path"))}</div>
<div class="meta"><span class="tag">{_esc(it.get("kind"))}</span>
被召回 {int(it.get("count") or 0)} 次 · topical≤{float(it.get("topical_max") or 0):.1f}</div>
{"".join(ctx_html)}{warn}
<input type="hidden" name="p{i}" value="{_esc(it.get("path"))}">
<input type="hidden" name="k{i}" value="{_esc(it.get("kind"))}">
<div class="opts">{opts}</div></div>""")

    payload = _json.dumps([{"path": it.get("path"), "kind": it.get("kind")}
                           for it in items], ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vault-loader 标注</title><style>{REVIEW_CSS}</style>
<script id="d" type="application/json">{payload}</script>
<div class="wrap"><h1>召回质量标注</h1>
<p class="sub">共 {len(items)} 条。勾选后点底部「提交」一次写入，可随时回改。</p>
<div class="note">「相关」= 这篇当时确实该被召回；「不相关」= 不该召回，属于误召；
「不确定」= 看了提问也判断不了。已标注过的条目不会再出现。</div>
<form method="post" action="/submit" id="f">
<div class="bar">
<button type="button" onclick="setAll('relevant')">全标相关</button>
<button type="button" onclick="setAll('irrelevant')">全标不相关</button>
<button type="button" onclick="setAll('')">清空</button>
<span class="prog" id="pg"></span></div>
{"".join(cards)}
<div class="foot"><button type="submit">提交</button></div></form></div>
<script>
const f=document.getElementById('f');
function setAll(v){{f.querySelectorAll('input[type=radio]').forEach(r=>{{
r.checked = v ? r.value===v : false;}});upd();}}
function upd(){{const n=document.querySelectorAll('.card').length;let d=0;
document.querySelectorAll('.card').forEach(c=>{{const on=c.querySelector('input:checked');
c.classList.toggle('done',!!on);if(on)d++;}});
document.getElementById('pg').textContent=`已标 ${{d}} / ${{n}}`;}}
f.addEventListener('change',upd);upd();
</script></html>"""


def apply_web_annotations(home: Path, payload, *, items=None) -> tuple[int, list[str]]:
    """把网页提交的标注写入 annotations.jsonl。返回 (保存条数, 错误列表)。

    **请求体来自浏览器，一律不可信**：verdict / kind 都必须对着白名单校验，
    非法值拒绝且不写入——`save_annotation` 自己也会 raise，但那会让整批中断，
    而这里要的是「坏条目跳过、好条目照存」。

    `context_ids` 同理**只从服务端自己的 `items` 里按 (path, kind) 查**，
    请求体里带的一概忽略：采信它等于让任何能访问该端口的人往这份不可再生的
    标注数据里写任意 session/prompt_h。按 (path, kind) 而非裸 path 配对，
    是因为同一篇笔记的两种 kind 本就是两个独立判断（见 `load_annotations`）。
    """
    id_map: dict[tuple, object] = {}
    for it in (items or []):
        if isinstance(it, dict):
            id_map[(it.get("path"), it.get("kind"))] = it.get("context_ids")
    saved, errs = 0, []
    for i, row in enumerate(payload or []):
        if not isinstance(row, dict):
            errs.append(f"#{i}: 不是对象")
            continue
        path, kind = row.get("path"), row.get("kind")
        verdict = row.get("verdict") or ""
        if not isinstance(path, str) or not path:
            errs.append(f"#{i}: path 非法")
            continue
        if not verdict:
            continue                       # 没勾选：跳过，不算错误
        if verdict not in VERDICTS:
            errs.append(f"#{i}: verdict 非法 {verdict!r}")
            continue
        if kind not in KINDS:
            errs.append(f"#{i}: kind 非法 {kind!r}")
            continue
        try:
            save_annotation(home, path, verdict, kind=kind,
                            context_ids=id_map.get((path, kind)))
            saved += 1
        except Exception as exc:           # noqa: BLE001
            errs.append(f"#{i}: 写入失败 {exc}")
    return saved, errs


def serve_review(home: Path, items, port: int = 0,
                 open_browser: bool = True) -> int:
    """起本地标注服务，提交后写盘并退出。只绑 127.0.0.1。

    绑定地址不可配：这个服务读写的是本机的标注数据，没有任何鉴权，暴露到
    0.0.0.0 等于把它交给同网段任何人。端口默认 0（由系统分配），避开占用。
    """
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    html = build_review_html(items)
    result = {"saved": 0, "errs": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):         # 不往 stderr 刷访问日志
            pass

        def _send(self, body: str, code: int = 200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self._send("<h1>404</h1>", 404)
                return
            self._send(html)

        def do_POST(self):
            if self.path != "/submit":
                self._send("<h1>404</h1>", 404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            rows = []
            for i in range(len(items)):
                rows.append({"path": (form.get(f"p{i}") or [""])[0],
                             "kind": (form.get(f"k{i}") or [""])[0],
                             "verdict": (form.get(f"v{i}") or [""])[0]})
            # items 传服务端自己的那份：rows 里的 path/kind 来自表单、可被改动，
            # 查不到对应条目时 apply_web_annotations 会不落标识符而非编一个。
            saved, errs = apply_web_annotations(home, rows, items=items)
            result["saved"], result["errs"] = saved, errs
            msg = f"<h1>已保存 {saved} 条</h1>"
            if errs:
                msg += "<p>以下条目被拒绝：</p><ul>" + "".join(
                    f"<li>{_esc(e)}</li>" for e in errs) + "</ul>"
            self._send(f'<!doctype html><meta charset="utf-8">'
                       f'<style>{REVIEW_CSS}</style><div class="wrap">{msg}'
                       f'<p class="sub">可以关闭本页了。</p></div>')
            threading.Thread(target=srv.shutdown, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"标注页已就绪：{url}")
    print("（在浏览器里勾选后点「提交」；提交后本服务自动退出）")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已取消，未写入任何标注")
        return 1
    finally:
        srv.server_close()
    if result["errs"]:
        print(f"已保存 {result['saved']} 条；{len(result['errs'])} 条被拒绝")
        return 1
    print(f"已保存 {result['saved']} 条标注")
    return 0


def collect_review_items(home: Path, tindex: dict, salt: bytes):
    """构造待标注条目（已剔除程序化来源与零可读上下文的），命令行版与网页版共用。

    抽成函数是因为两个入口的准备逻辑必须**逐字相同** —— 只要有一处漏了
    `human_records` 或 `attach_contexts`，那个入口的标注池就会重新混进程序化
    记录或盲条目，而人工标注不可再生。
    """
    done = load_annotations(home)
    todo = [x for x in sample_near_miss(human_records(load_records(home), tindex))
            if ("near_miss", x["path"]) not in done]
    for x in todo:
        x["kind"] = "near_miss"
    # 第二趟：精度侧。两类各自 k 条上限、不合并计数。
    # load_records 是生成器、只能迭代一次，所以这里**刻意**再扫一遍文件——
    # 用 IO 换内存，绝不改成一趟物化（会重演 574MB 峰值那个已修的回归）。
    todo += [x for x in sample_admitted(human_records(load_records(home), tindex))
             if (x["kind"], x["path"]) not in done]
    # 预取上下文：一条都读不出来的条目直接剔除（盲标只会产出 unsure）
    return attach_contexts(
        todo, lambda: human_records(load_records(home), tindex),
        lambda _s, _ph: lookup_prompt(home, _s, _ph, salt, tindex))


def sample_near_miss(records: Iterable[dict], k: int = 20) -> list[dict]:
    """按出现次数降序取 Top-K near-miss，供召回侧人工标注。纯函数、无 IO。

    **判据走 `_metrics.scorelow_entries`**，与 `summarize` 榜单、`flush` 的 nudge
    计数共用同一个出口。人工标注是唯一不可再生的数据：在污染池上抽样，等于让人
    对着「其实早就成功注入过」的笔记回答「它该不该被召回」——那是恒真判断，既浪费
    判断力，又会把这条通道的表观精度虚高。
    实测改动前的 20 个候选里 13 个以去重抑制为主，其中若干条 score-low 次数为 0。

    **上一版这里内联了一份判据、漏掉 topical 下限**，于是 topical=0 的条目照样进池——
    对一篇压根不相关的笔记问「它该不该被召回」，答案恒为否，标注者的判断力被白白消耗，
    而这份数据删了就再也生不回来。用 `scorelow_entries` 之后，池子与榜单、nudge 计数
    严格同源。
    """
    agg: dict[str, dict] = {}
    for r in records:
        if not isinstance(r.get("near_miss_scorelow"), list):
            continue        # 旧记录：样本已被挤占，理由见 _metrics.scorelow_entries
        for p, t in _metrics.scorelow_entries(r):
            cur = agg.setdefault(p, {"path": p, "count": 0, "topical_max": 0.0})
            cur["count"] += 1
            cur["topical_max"] = max(cur["topical_max"], t)
    return sorted(agg.values(), key=lambda x: (-x["count"], -x["topical_max"]))[:k]


# 非人类来源（需要排除的自动化轮次）。**判据从"白名单"翻转为"黑名单"**：
#
# 原实现是白名单 `("typed","queued","suggestion_accepted")`，配 `not in` 短路。
# 那组枚举值连同上一版注释里「事件级实测（259 条）：typed 113 / sdk 93 / ...」的
# 分布，都是从 **transcript**（`~/.claude/projects/*/*.jsonl` 的 user message）统计
# 来的 —— 那里确实有 `promptSource`。但 hook 的 **stdin payload 从不下发该键**
# （Claude Code 2.1.220 二进制实证：UserPromptSubmit 的 payload 构造里没有它），
# 于是 `build_record` 落盘的 `src` 恒为 `""`，白名单**一条都放不过**。
# 后果：本函数自 0.9.0 上线起对真实数据恒返回 []，精度标注通道 100% 空转，
# 而两条守卫用例手工构造 `src="typed"` 的 record，永远发现不了（生产中从无该形态）。
#
# 翻转成黑名单后与写端注释对齐 —— `build_record` 明写「'' 是合法取值（空串按用户
# 输入处理）」。harness 日后真的下发时，`sdk`/`system` 会被自动排除，无需再改代码。
_NON_HUMAN_SRC = ("sdk", "system")


def _is_human_src(src: object) -> bool:
    """src 是否可归因为人类提问。缺失/空串 = harness 未下发 ⇒ 按用户输入处理。"""
    return src not in _NON_HUMAN_SRC


def sample_admitted(records: Iterable[dict], k: int = 20) -> list[dict]:
    """按出现次数降序取 Top-K **已注入**条目，供精度侧人工标注。纯函数、无 IO。

    与 `sample_near_miss` 互补：那边问「该不该被召回」（漏召回），这边问
    「召回得对不对」（精度）。改动前只有前者，于是「注入了不相关笔记」这类
    问题在数据上完全不可见。

    评判对象是**用户实际看到的**——全文主候选 + top-3 清单，不是 admitted 全集
    （实测 n_admitted 中位数 66，而渲染出去的只有 max_notes=3 条 + 至多 1 篇全文，
    对全集标注 95% 的条目从未进过模型上下文）。

    有界聚合：按 (kind, path) 累加标量，**绝不物化 records**——`load_records`
    是生成器，物化会重演已修复的 574MB 峰值回归。
    """
    agg: dict[tuple[str, str], dict] = {}

    def _bump(kind: str, path: str, topical: float) -> None:
        if not isinstance(path, str) or not path:
            return
        cur = agg.setdefault((kind, path),
                             {"path": path, "kind": kind,
                              "count": 0, "topical_max": 0.0})
        cur["count"] += 1
        # topical 来自嵌套的 admitted 数组，不在 load_records 的顶层 coerce 范围内。
        # 磁盘 jsonl 可被手工编辑：实测 "high" 抛 ValueError、{} 抛 TypeError，
        # 而 --review 是人工标注入口，崩在这里等于整轮标注白做。
        try:
            t = float(topical or 0)
        except (TypeError, ValueError):
            t = 0.0
        cur["topical_max"] = max(cur["topical_max"], t)

    for r in records:
        # 只排除**确知**是自动化的来源；缺失/空串按人类处理（见 _NON_HUMAN_SRC 说明）。
        # 改动前是白名单 `not in _HUMAN_SRC`，而 src 在真实数据里恒为空 ⇒ 恒短路。
        if not _is_human_src(r.get("src")):
            continue
        ft_path = (r.get("ft") or {}).get("path") or ""
        admitted = r.get("admitted") or []
        by_path = {a.get("path"): a for a in admitted}
        if ft_path:
            # ft 按 topical 选、admitted 按 total 截断到 admitted_k，实测 12/170
            # 的 ft 不在落盘样本内 ⇒ 取不到 topical 时按 0 记，不要假定能 join 上
            _bump("admitted_fulltext", ft_path,
                  (by_path.get(ft_path) or {}).get("topical") or 0)
        # 清单侧取多少条，必须与渲染层一致 —— 本函数的 docstring 明写「评判对象是
        # 用户**实际看到的**」。
        # ⚠️ 此前硬编码取 3「以覆盖有/无全文两种形态」，那是错的：有全文时渲染层是
        # `rest = [...][: max_notes - 1]`（全文计入 max_notes 之内），只渲染 2 条清单，
        # 于是每个 ft 轮次都会有 1 条**从未进过模型上下文**的条目混进标注池——
        # 而人工标注是这套机制里唯一不可再生的数据。用户把 max_notes 调成 1 时偏差扩大到 2。
        mn = r.get("max_notes")
        if not (isinstance(mn, int) and not isinstance(mn, bool) and mn > 0):
            mn = 3                       # 旧记录未落该字段，回退到当时的默认值
        limit = mn - 1 if ft_path else mn
        shown = 0
        for a in admitted:
            if shown >= limit:
                break
            if not isinstance(a, dict) or a.get("path") == ft_path:
                continue
            _bump("admitted_list", a.get("path") or "", a.get("topical") or 0)
            shown += 1
    ranked = sorted(agg.values(),
                    key=lambda x: (-x["count"], -x["topical_max"]))
    return _apply_kind_quota(ranked, k)


def _apply_kind_quota(ranked: list[dict], k: int) -> list[dict]:
    """按 kind 分配额取样，两侧互相回填。`ranked` 须已按优先级降序。

    **为什么不能用全局 top-k**：清单侧每轮贡献 max_notes-1 条、全文侧每轮至多 1
    条，于是清单侧的 count 系统性更高。本机实测（0.9.1 后 283 轮）清单侧 497 个
    条目、count 最大 74，全文侧 209 个条目、count 最大 17，而全局 top20 的门槛是
    22 —— 全文侧一条都进不去，admitted_fulltext 的人工标注恒为 0。0.9.0 分出这个
    kind 的理由是「代价差一个量级……混成一类之后就分不出是哪边错了」，分了 kind
    却不分配额，那个理由就没有兑现。

    **互相回填**：某一侧不足配额时另一侧补满，否则数据少时池子会平白缩水 ——
    抽样池本就只有 k 条，而人工标注不可再生。

    全文优先排前：标注者中途停手时，先标到的是代价最大的那一类（单篇上限 8192
    字节，占 42% 的轮次）。
    """
    fts = [x for x in ranked if x["kind"] == "admitted_fulltext"]
    lists = [x for x in ranked if x["kind"] != "admitted_fulltext"]
    n_ft = min(len(fts), k // 2)
    n_list = min(len(lists), k - n_ft)
    n_ft = min(len(fts), k - n_list)        # 清单侧不足时，全文侧回填
    return fts[:n_ft] + lists[:n_list]


def _normalize_context_ids(raw) -> list[dict]:
    """把 `[(session, prompt_h), ...]` 归一成可落盘的 dict 列表。

    畸形输入一律降级为空列表，**绝不抛异常**：标注是这套机制里唯一不可再生的数据
    （见 `_metrics.purge` 的定性），为了一个附属字段让整条写入失败是本末倒置。
    落 dict 而非裸元组，是因为 JSON 没有元组、读回来会变成 list，
    带键名才能在日后加字段时不必猜位置。
    """
    out: list[dict] = []
    try:
        for item in (raw or []):
            s, ph = item                      # 长度不为 2 即抛，由下面兜住
            if isinstance(s, str) and isinstance(ph, str) and s and ph:
                out.append({"session": s, "prompt_h": ph})
    except Exception:                          # noqa: BLE001
        return []
    return out


def save_annotation(home: Path, path: str, verdict: str, *,
                    kind: str = DEFAULT_KIND, context_ids=None) -> None:
    """追加一条标注。后写覆盖先写（读取时按 (kind, path) 合并）。

    `kind` 强制 keyword-only：它与 `path`/`verdict` 是相邻同类型字符串，位置
    传参一旦对调会静默错位——同 `_metrics.build_record` 的 session_id/prompt_id，
    那处已实证「对调后 84 个既有测试全绿，类型系统与既有断言都拦不住」。
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS} 之一，收到 {verdict!r}")
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
    p = annotations_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"_schema": _metrics.SCHEMA, "path": path,
           "verdict": verdict, "kind": kind}
    ids = _normalize_context_ids(context_ids)
    if ids:
        # 不传 / 归一后为空则**不落该键**：旧记录与新记录都是合法形态，
        # 读端不必区分「没有这个字段」与「有但是空的」两种情况。
        rec["context_ids"] = ids
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_annotations(home: Path) -> dict[tuple[str, str], str]:
    """读取全部人工标注，返回 `{(kind, path): verdict}`。

    **键是 (kind, path) 而非裸 path**：同一篇笔记「作为擦肩候选该不该被召回」
    与「作为已注入内容召回得对不对」是两个独立判断，共用 path 键会让两者互相
    覆盖（`--review` 的去重也会因此漏抽）。缺 `kind` 的旧记录归 `near_miss`。

    **坏行必须计数并经 stderr 报出，不能静默跳过**——与
    `load_records`（`:27-58`）同一约定，且标注比普通 metrics 更该有可见性：
    `_metrics.purge` 的 docstring 把它定性为「用户逐条投入时间标出的、删了
    不可重新生成」的数据，静默丢一条，用户永远不知道自己的标注没了。

    两类坏数据分别计数：`bad_record`（JSON 解析失败，或解析成功但不是带 path
    的 dict——结构本身不像一条标注）、`bad_verdict`（结构合法但 verdict 不在
    `VERDICTS` 内，例如手工改坏文件、或未来 schema 演进遗留的旧值）。
    """
    out: dict[tuple[str, str], str] = {}
    p = annotations_path(home)
    if not p.exists():
        return out
    bad_record = bad_verdict = 0
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad_record += 1
                continue
            # path 必须是**非空字符串**：只判真值的话，`{"path": ["a"]}` 会通过，
            # 随后 `out[r["path"]]` 拿 list 当 dict 键直接抛 unhashable TypeError，
            # 整个 --review 未捕获崩溃；`{"path": 123}` 更糟——可哈希、静默存成 int 键，
            # 之后与字符串路径永不相等，标注等于凭空消失（L-SEC-2）。
            # annotations.jsonl 是本机文件，损坏可能来自手工编辑或磁盘异常，
            # 不是可信输入。
            if not (isinstance(r, dict) and isinstance(r.get("path"), str)
                    and r["path"].strip()):
                bad_record += 1
                continue
            verdict = r.get("verdict")
            if verdict not in VERDICTS:
                bad_verdict += 1
                continue
            # kind 缺失=写于加字段之前，一律归 near_miss（已完成的 20 条正属此类）。
            # 未知 kind 与非法 verdict 同等处理，计入 bad_record——手工编辑或未来
            # schema 演进都可能留下它，静默接受会让两类标注的统计悄悄混淆。
            kind = r.get("kind", DEFAULT_KIND)
            if kind not in KINDS:
                bad_record += 1
                continue
            out[(kind, r["path"])] = verdict      # 后写覆盖
    except OSError:
        pass
    if bad_record or bad_verdict:
        print(f"[vault-loader] annotations.jsonl 跳过 {bad_record} 行解析/结构异常 + "
              f"{bad_verdict} 行 verdict 非法（该文件不可重新生成，请检查）",
              file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    # 三个动作互斥（L-PY4）。原先靠下面 if/elif 的书写顺序决定优先级，
    # `--purge --review` 会让 **purge 静默胜出**——而 purge 是不可逆的：
    # 用户以为要去标注，实际把包括人工标注在内的全部数据删了，且没有任何提示。
    # 交给 argparse 在解析阶段直接报错退出，比任何执行期优先级都安全。
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--report", action="store_true")
    action.add_argument("--purge", action="store_true")
    action.add_argument("--review", action="store_true")
    action.add_argument("--review-web", action="store_true",
                        help="在浏览器里批量标注（本地服务，只绑 127.0.0.1）")
    # 只清 near-miss 累计计数这一份**派生**数据，不碰事件记录与人工标注。
    # 与 --purge 分开是因为二者代价差一个量级：purge 会删掉不可再生的 annotations。
    action.add_argument("--reset-counts", action="store_true")
    # --show-paths 是 --report 的修饰项，不参与互斥
    ap.add_argument("--show-paths", action="store_true")
    ap.add_argument(
        "--runtime", choices=("auto", "legacy", "claude", "codex", "all"), default="auto",
        help="metrics 命名空间；canonical 存在时 auto/all 同时处理 Claude 与 Codex",
    )
    args = ap.parse_args()
    home = Path.home()
    canonical_exists = (home / ".context-vault" / "config.json").exists()
    selected = args.runtime
    if selected == "auto":
        selected = "all" if canonical_exists else "legacy"
    # `all` **必须包含 legacy**。迁移是「复制」不是「移动」，legacy 目录在迁移后
    # 原样留在盘上（含加盐 hash、明文笔记路径、明文 session id、不可再生的人工标注）。
    # 漏掉它时 `--purge` 只清一半却报告「已清空 N 个数据文件」，`--report` 也看不到
    # 那半边——「没有数据」与「数据在另一个命名空间」不可区分。
    runtimes = ("legacy", "claude", "codex") if selected == "all" else (selected,)

    def all_records():
        for runtime in runtimes:
            _metrics.configure_context(runtime)
            yield from load_records(home)

    if args.reset_counts:
        n = 0
        for runtime in runtimes:
            _metrics.configure_context(runtime)
            n += _metrics.reset_counts(home)
        print(f"已清空 near-miss 累计计数（{n} 条）与 nudge 冷却戳。"
              f"事件记录与人工标注未受影响。")
        return 0
    if args.purge:
        # 先读标注条数——purge 之后就读不到了，而这是唯一不可重新生成的数据
        n_ann = 0
        n = 0
        scanned: list[str] = []
        try:
            for runtime in runtimes:
                _metrics.configure_context(runtime)
                scanned.append(str(_metrics.metrics_dir(home)))
                n_ann += _metrics.count_annotations(home)
                n += _metrics.purge(home)
        except OSError as exc:
            print(f"metrics 清理失败，仍有数据保留：{exc}", file=sys.stderr)
            return 2
        # 把实际扫过的目录打出来：这是不可逆删除，用户必须能看出「漏了哪一个」。
        # 只报一个总数时，「清了 0 个」与「压根没扫到那个目录」完全不可区分。
        msg = f"已清空 {n} 个数据文件（扫描目录：{'、'.join(scanned)}）"
        if n_ann:
            # 按非空行数计，不解析 JSON、不去重、不分类：near-miss 与精度两类
            # 混在同一个数里。刻意保持不解析——test_count_annotations_tolerates_
            # invalid_utf8 钉住了「文件损坏时仍能报出数量」，而这是不可逆删除前
            # 的最后一道知情提示，此时最不该因一行坏 JSON 报错或少报。
            msg += (f"（其中 {n_ann} 条人工标注已删除，不可恢复；"
                    f"含 near-miss 与精度两类，未去重）")
        print(msg)
        return 0
    if args.review_web:
        if len(runtimes) != 1:
            print("--review-web 必须显式指定 --runtime legacy|claude|codex",
                  file=sys.stderr)
            return 2
        _metrics.configure_context(runtimes[0])
        tindex = _transcript_index(home)
        try:
            salt = _metrics.get_salt(home)
        except OSError as exc:
            print(f"读取盐失败，无法回查提问上下文：{exc}", file=sys.stderr)
            return 2
        todo = collect_review_items(home, tindex, salt)
        if not todo:
            print("没有待标注的条目（有上下文可判断的都已标注完）")
            return 0
        return serve_review(home, todo)

    if args.review:
        if len(runtimes) != 1:
            print("--review 必须显式指定 --runtime legacy|claude|codex", file=sys.stderr)
            return 2
        _metrics.configure_context(runtimes[0])
        # 下面两条是**假设没有这层护栏**、裸 input() 会撞上的失败形态——正是这层
        # isatty() 检查想防的东西。都发生在无人能应答的环境里：
        #   stdin=DEVNULL      -> input() 立刻抛 EOFError，无兜底则整条堆栈打到
        #                         stderr、退出码 1（脚本里常见的显式重定向）
        #   stdin=空闲的 PIPE  -> **真实挂起**，实测 6 秒仍不退出，直到对端关闭
        # 后者最危险：CI/自动化继承一个没人写入的管道就会卡死，且毫无提示。
        # ⚠️ 但这层护栏本身不可靠：Windows 上 stdin=DEVNULL 时 `sys.stdin.isatty()`
        # 实测返回 True（NUL 是字符设备，`_isatty()` 对任意字符设备都判真）——
        # 这恰是自动化/CI 里最常见的重定向形态，此时护栏形同虚设。真正兜底不在
        # 这里，而在下面循环体内的 EOFError 分支：不管这层判断触没触发，「非交互
        # 环境下返回 2、且不误写任何标注」这条退出码契约都必须成立。
        if not sys.stdin.isatty():
            print("--review 需要交互式终端；当前 stdin 不是 TTY，已中止。",
                  file=sys.stderr)
            return 2
        # transcript 索引：既用于按 (session, prompt_h) 取回提问原文，也用于
        # `human_records` 按 session 判来源。**必须在抽样之前建好** —— 抽样已经
        # 依赖它了。索引与盐都只取一次，全程复用。
        tindex = _transcript_index(home)
        try:
            salt = _metrics.get_salt(home)
        except OSError as exc:
            print(f"读取盐失败，无法回查提问上下文：{exc}", file=sys.stderr)
            return 2
        todo = collect_review_items(home, tindex, salt)
        if not todo:
            print("没有待标注的条目（有上下文可判断的都已标注完）")
            return 0
        print(f"待标注 {len(todo)} 条。输入 r=相关 / i=不相关 / u=不确定 / q=退出")
        try:
            salt = _metrics.get_salt(home)
        except OSError:
            salt = b""
        code = {"r": "relevant", "i": "irrelevant", "u": "unsure"}
        saved = 0
        for item in todo:
            shown = sanitize_injected_text(item["path"], keep_newlines=False)[:100]
            # 每条重新扫一遍 records：load_records 是生成器、只能迭代一次，
            # 且这里刻意不物化（P3 修复，旧实现实测 574MB 峰值）。--review 是人工
            # 交互命令，每条多花几十毫秒 IO 换取不重演那个内存回归，是划算的。
            print("")
            for _ln in format_context_lines(item["contexts"], item["unreadable"]):
                print(_ln)
            try:
                # isatty() 撒谎时（见上方 Windows DEVNULL 案例）会一路执行到这里，
                # 第一次 input() 立刻 EOFError。区分两种 EOFError：
                #   一条都没保存过 -> 这是非交互环境，不是用户真在交互，退出码
                #                     必须与上面的护栏一致（2），否则调用方按
                #                     返回码判断「是否成功中止」会拿到错误信号
                #   已保存过至少一条 -> 用户交互到一半按 Ctrl-D 正常退出，维持 0
                ans = input(f"[{item['kind']}] [{item['count']}次 "
                            f"topical≤{item['topical_max']:.1f}] "
                            f"{shown} > ").strip().lower()
            except EOFError:
                if saved == 0:
                    print("--review 需要交互式终端；当前 stdin 不是 TTY，已中止。",
                          file=sys.stderr)
                    return 2
                print("\n输入流已结束，中止标注。", file=sys.stderr)
                break
            if ans == "q":
                break
            if ans in code:
                save_annotation(home, item["path"], code[ans], kind=item["kind"],
                                context_ids=item.get("context_ids"))
                saved += 1
        print(f"已标注，结果在 {annotations_path(home)}")
        return 0
    print(render_report(summarize(all_records()), show_paths=args.show_paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
