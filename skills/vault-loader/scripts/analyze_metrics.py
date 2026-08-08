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
                        if isinstance(r, dict) and r.get("_schema") == _metrics.SCHEMA:
                            yield r
            except OSError:
                continue
    if skipped:
        print(f"[vault-loader] 跳过 {skipped} 行损坏记录"
              f"（同一 session 并发写会产生撕裂行，属已知限制）", file=sys.stderr)


def summarize(records: Iterable[dict]) -> dict:
    """聚合报表。

    **`n_admitted`/`arm_dist` 优先读落盘的 `n_admitted`/`arm_counts` 标量与字典**
    （P1 修复：`build_record` 现在把 `admitted` 数组截断到 `admitted_k` 条展示样本，
    若仍靠遍历 `r["admitted"]` 累加，截断后的记录会把真实计数**低报**成截断后的
    条数）。这两个字段是 `build_record` 在截断**之前**算好的，覆盖截断造成的信息
    损失。**旧记录**（截断改动前落盘、没有这两个字段）回退到遍历 `admitted` 数组
    ——彼时 `admitted` 未截断，等价于全量，遍历口径与新字段口径一致，不会算错。
    """
    arm, gate, near = Counter(), Counter(), Counter()
    n_admitted = ft = n = 0
    for r in records:
        gate[r.get("gate") or "ok"] += 1
        if "n_admitted" in r and "arm_counts" in r:
            n_admitted += int(r.get("n_admitted") or 0)
            for arm_name, cnt in (r.get("arm_counts") or {}).items():
                arm[arm_name or "?"] += int(cnt)
        else:
            for a in r.get("admitted") or []:
                arm[a.get("arm") or "?"] += 1
                n_admitted += 1
        if (r.get("ft") or {}).get("path"):
            ft += 1
        for nm in r.get("near_miss") or []:
            near[nm.get("path", "?")] += 1
        n += 1               # 边遍历边计数：records 现在是生成器，len() 不再适用
    return {"n_events": n, "n_admitted": n_admitted, "arm_dist": dict(arm),
            "gate_dist": dict(gate), "fulltext_rate": (ft / n) if n else 0.0,
            "near_miss_top": near.most_common(20)}


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


def render_report(s: dict, show_paths: bool = False) -> str:
    lines = [INJECTION_NOTICE, "", "📊 vault-loader 指标报表", "",
             f"事件数 {s['n_events']} · 累计入选 {s['n_admitted']} 篇 · "
             f"全文注入率 {s['fulltext_rate']:.1%}", "",
             f"闸门分布: {s['gate_dist']}", f"入选臂分布: {s['arm_dist']}", "",
             "near-miss（最常擦肩而过的笔记）:"]
    for p, c in s["near_miss_top"]:
        shown = sanitize_injected_text(p, keep_newlines=False)[:80] if show_paths \
            else _stable_path_id(p)
        lines.append(f"  {c:>4} 次  {shown}")
    if not show_paths:
        lines.append("  （路径已隐去，加 --show-paths 展开）")
    return "\n".join(lines)


VERDICTS = ("relevant", "irrelevant", "unsure")


def annotations_path(home: Path) -> Path:
    return _metrics.metrics_dir(home) / "annotations.jsonl"


def sample_near_miss(records: Iterable[dict], k: int = 20) -> list[dict]:
    """按出现次数降序取 Top-K near-miss。纯函数、无 IO。"""
    agg: dict[str, dict] = {}
    for r in records:
        for nm in r.get("near_miss") or []:
            p = nm.get("path")
            if not p:
                continue
            cur = agg.setdefault(p, {"path": p, "count": 0, "topical_max": 0.0})
            cur["count"] += 1
            cur["topical_max"] = max(cur["topical_max"], float(nm.get("topical") or 0))
    return sorted(agg.values(), key=lambda x: (-x["count"], -x["topical_max"]))[:k]


def save_annotation(home: Path, path: str, verdict: str) -> None:
    """追加一条标注。后写覆盖先写（读取时按行序合并）。"""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS} 之一，收到 {verdict!r}")
    p = annotations_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"_schema": _metrics.SCHEMA, "path": path, "verdict": verdict}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_annotations(home: Path) -> dict[str, str]:
    """读取全部人工标注。**坏行必须计数并经 stderr 报出，不能静默跳过**——与
    `load_records`（`:27-58`）同一约定，且标注比普通 metrics 更该有可见性：
    `_metrics.purge` 的 docstring 把它定性为「用户逐条投入时间标出的、删了
    不可重新生成」的数据，静默丢一条，用户永远不知道自己的标注没了。

    两类坏数据分别计数：`bad_record`（JSON 解析失败，或解析成功但不是带 path
    的 dict——结构本身不像一条标注）、`bad_verdict`（结构合法但 verdict 不在
    `VERDICTS` 内，例如手工改坏文件、或未来 schema 演进遗留的旧值）。
    """
    out: dict[str, str] = {}
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
            out[r["path"]] = verdict      # 后写覆盖
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
    # --show-paths 是 --report 的修饰项，不参与互斥
    ap.add_argument("--show-paths", action="store_true")
    args = ap.parse_args()
    home = Path.home()
    if args.purge:
        # 先读标注条数——purge 之后就读不到了，而这是唯一不可重新生成的数据
        n_ann = _metrics.count_annotations(home)
        n = _metrics.purge(home)
        msg = f"已清空 {n} 个数据文件"
        if n_ann:
            msg += f"（其中 {n_ann} 条人工标注已删除，不可恢复）"
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
        todo = [x for x in sample_near_miss(load_records(home)) if x["path"] not in done]
        if not todo:
            print("没有待标注的 near-miss 条目")
            return 0
        print(f"待标注 {len(todo)} 条。输入 r=相关 / i=不相关 / u=不确定 / q=退出")
        code = {"r": "relevant", "i": "irrelevant", "u": "unsure"}
        saved = 0
        for item in todo:
            shown = sanitize_injected_text(item["path"], keep_newlines=False)[:100]
            try:
                # isatty() 撒谎时（见上方 Windows DEVNULL 案例）会一路执行到这里，
                # 第一次 input() 立刻 EOFError。区分两种 EOFError：
                #   一条都没保存过 -> 这是非交互环境，不是用户真在交互，退出码
                #                     必须与上面的护栏一致（2），否则调用方按
                #                     返回码判断「是否成功中止」会拿到错误信号
                #   已保存过至少一条 -> 用户交互到一半按 Ctrl-D 正常退出，维持 0
                ans = input(f"[{item['count']}次 topical≤{item['topical_max']:.1f}] "
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
                save_annotation(home, item["path"], code[ans])
                saved += 1
        print(f"已标注，结果在 {annotations_path(home)}")
        return 0
    print(render_report(summarize(load_records(home)), show_paths=args.show_paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
