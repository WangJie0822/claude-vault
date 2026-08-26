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
SUPPORTED_SCHEMAS = frozenset({1})


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
                 "ft", "n_legacy_near", "inj_chars", "inj_n")

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


def _acc_to_dict(a: _Acc) -> dict:
    return {
        "n_events": a.n, "n_ok": a.n_ok, "n_admitted": a.n_admitted,
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
        a.n += 1
        _acc_gate_and_admitted(r, a)
        _acc_cost(r, a)
        _acc_near_miss(r, a)
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
                     f"（不含 SessionStart 通道，它不落 metrics）")
    else:
        lines.append("UPS 注入正文字符数: 无数据（该字段自 0.9.1 起记录，旧记录没有）")
    # 来源分布：黑名单判据的自观测出口。`_is_human_src` 默认放行，所以「harness 开始
    # 下发一个新的自动化来源值」这件事本身是静默的 —— 打出来至少让它可见。
    # 目前实测恒为 `(空)`（promptSource 不在 hook stdin payload 里）。
    src_dist = s.get("src_dist") or {}
    if src_dist and set(src_dist) != {"(空)"}:
        lines.append(f"来源分布: {src_dist}"
                     f"（非 {list(_NON_HUMAN_SRC)} 的值都会进 --review 的人类标注池）")
    lines += ["", f"闸门分布: {s['gate_dist']}", f"入选臂分布: {s['arm_dist']}",
              # 成因分布是**全量口径**（含旧记录），与下面两榜的总体不同 —— 不标注的话
              # 读者会把三组数当成同一批样本的切分。且其「打分不够」桶正是本轮论证为
              # 「有系统性偏斜」的那批残差（旧记录的 near_miss 样本被去重条目挤占）。
              f"near-miss 成因分布（全部 {s['n_events']} 条记录，口径与下方两榜不同）: "
              f"{s.get('near_miss_dedup_dist', {})}", ""]

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
        lines.append("对照 · 被去重抑制（其实已经成功召回过，**不是**漏召回）:")
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


def annotations_path(home: Path) -> Path:
    return _metrics.metrics_dir(home) / "annotations.jsonl"


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
    return sorted(agg.values(),
                  key=lambda x: (-x["count"], -x["topical_max"]))[:k]


def save_annotation(home: Path, path: str, verdict: str, *,
                    kind: str = DEFAULT_KIND) -> None:
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
    # 只清 near-miss 累计计数这一份**派生**数据，不碰事件记录与人工标注。
    # 与 --purge 分开是因为二者代价差一个量级：purge 会删掉不可再生的 annotations。
    action.add_argument("--reset-counts", action="store_true")
    # --show-paths 是 --report 的修饰项，不参与互斥
    ap.add_argument("--show-paths", action="store_true")
    args = ap.parse_args()
    home = Path.home()
    if args.reset_counts:
        n = _metrics.reset_counts(home)
        print(f"已清空 near-miss 累计计数（{n} 条）与 nudge 冷却戳。"
              f"事件记录与人工标注未受影响。")
        return 0
    if args.purge:
        # 先读标注条数——purge 之后就读不到了，而这是唯一不可重新生成的数据
        n_ann = _metrics.count_annotations(home)
        n = _metrics.purge(home)
        msg = f"已清空 {n} 个数据文件"
        if n_ann:
            # 按非空行数计，不解析 JSON、不去重、不分类：near-miss 与精度两类
            # 混在同一个数里。刻意保持不解析——test_count_annotations_tolerates_
            # invalid_utf8 钉住了「文件损坏时仍能报出数量」，而这是不可逆删除前
            # 的最后一道知情提示，此时最不该因一行坏 JSON 报错或少报。
            msg += (f"（其中 {n_ann} 条人工标注已删除，不可恢复；"
                    f"含 near-miss 与精度两类，未去重）")
        print(msg)
        return 0
    if args.review:
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
        done = load_annotations(home)
        todo = [x for x in sample_near_miss(load_records(home))
                if ("near_miss", x["path"]) not in done]
        for x in todo:
            x["kind"] = "near_miss"
        # 第二趟：精度侧。两类各自 k 条上限、不合并计数。
        # load_records 是生成器、只能迭代一次，所以这里**刻意**再扫一遍文件——
        # 用 IO 换内存，绝不改成一趟物化（会重演 574MB 峰值那个已修的回归）。
        todo += [x for x in sample_admitted(load_records(home))
                 if (x["kind"], x["path"]) not in done]
        if not todo:
            print("没有待标注的条目")
            return 0
        print(f"待标注 {len(todo)} 条。输入 r=相关 / i=不相关 / u=不确定 / q=退出")
        # 标注上下文：按 (session_id, prompt_h) 从 transcript 取回当时的提问原文。
        # 没有它，「该不该被召回」这个问题无法回答（见 lookup_prompt 的说明）。
        # 索引与盐都只取一次，全程复用。
        tindex = _transcript_index(home)
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
            ctx = sample_admitted_events(load_records(home), item["path"], item["kind"])
            if ctx:
                print(f"\n  ── 该笔记被召回时，你问的是 ──")
                for i, (sess, ph) in enumerate(ctx, 1):
                    raw = lookup_prompt(home, sess, ph, salt, tindex)
                    if raw:
                        one = " ".join(raw.split())[:160]
                        print(f"   {i}. {sanitize_injected_text(one, keep_newlines=False)}")
                    else:
                        print(f"   {i}. （transcript 里找不到，可能已被清理）")
            else:
                print("\n  ── 无提问上下文（该条目写于 prompt_h 落盘之前）──")
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
                save_annotation(home, item["path"], code[ans], kind=item["kind"])
                saved += 1
        print(f"已标注，结果在 {annotations_path(home)}")
        return 0
    print(render_report(summarize(load_records(home)), show_paths=args.show_paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
