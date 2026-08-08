# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import _metrics
from scripts.analyze_metrics import load_records, summarize, render_report


def _seed(home):
    for i in range(3):
        _metrics.write_record(home, "sess-A", {
            "_schema": 1, "ts": 1.0 + i, "session": "sess-A", "prompt_id": f"p{i}",
            "cwd_h": "abc123", "kw_h": ["h1", "h2"], "n_kw": 2,
            "gate": "" if i else "too_few_keywords", "relaxed": False,
            "admitted": [{"path": "n/a.md", "topical": 7.0, "total": 9.0,
                          "arm": "topical", "dedup": "", "hits": ["内存"]}] if i else [],
            "near_miss": [{"path": "n/b.md", "topical": 5.8}],
            "n_excluded": 100, "ft": {"path": "n/a.md" if i == 2 else "", "arm": ""},
        })


def test_summarize_counts(tmp_path):
    _seed(tmp_path)
    s = summarize(load_records(tmp_path))
    assert s["n_events"] == 3
    assert s["gate_dist"]["too_few_keywords"] == 1
    assert s["arm_dist"]["topical"] == 2
    assert s["fulltext_rate"] == 1 / 3


def test_load_records_ignores_top_level_annotations(tmp_path):
    """H1 修复：顶层 `annotations.jsonl`（`--review` 产生的人工标注）不得被
    当成事件记录统计进报表。

    标注记录同样带 `_schema` 字段（`save_annotation` 写入
    `{"_schema":1,"path":...,"verdict":...}`），旧实现 `root.rglob("*.jsonl")`
    只按 `_schema` 判定、不看目录层级，会把它和真实事件（`<YYYY-MM>/*.jsonl`）
    混在一起——标注记录没有 `gate` 字段，`summarize()` 的
    `gate[r.get("gate") or "ok"]` 会把每条标注都记成一次成功召回。**本用例在
    同一 tmp_path 下同时造两类数据**——这正是本轮 30 个测试集体漏掉的合流场景
    （`write_record` 与 `save_annotation` 各有测试却从未同时造数据）。
    """
    from scripts.analyze_metrics import save_annotation

    _metrics.write_record(tmp_path, "sess-A", {
        "_schema": 1, "ts": 1.0, "session": "sess-A", "prompt_id": "p0",
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [{"path": "n/a.md", "topical": 7.0, "total": 9.0,
                      "arm": "topical", "dedup": "", "hits": []}],
        "n_admitted": 1, "arm_counts": {"topical": 1}, "admitted_k": 20,
        "near_miss": [], "n_excluded": 0, "ft": {"path": "", "arm": ""},
    })
    save_annotation(tmp_path, "n/b.md", "relevant")
    save_annotation(tmp_path, "n/c.md", "irrelevant")
    save_annotation(tmp_path, "n/d.md", "unsure")

    # load_records 现在是生成器（P3 流式化），需显式物化才能取长度；
    # 且生成器只能迭代一次，故这里另起一个给 summarize 消费。
    recs = list(load_records(tmp_path))
    assert len(recs) == 1, f"应只统计 1 条真实事件记录，annotations 混入后实际 {len(recs)} 条"
    s = summarize(load_records(tmp_path))
    assert s["n_events"] == 1
    # 3 条标注若混入会各贡献一次 gate="ok"（标注记录无 gate 字段），令 n_events 变 4
    assert s["gate_dist"] == {"ok": 1}


def test_report_hides_paths_and_carries_notice(tmp_path):
    _seed(tmp_path)
    out = render_report(summarize(load_records(tmp_path)))
    assert "知识库历史内容" in out          # INJECTION_NOTICE
    assert "n/b.md" not in out              # 默认不露明文路径
    out2 = render_report(summarize(load_records(tmp_path)), show_paths=True)
    assert "n/b.md" in out2


def test_near_miss_id_is_stable_across_processes():
    """路径占位 ID 必须跨进程确定性——不能用内建 `hash()`（对 str 默认按
    PYTHONHASHSEED 每进程随机化，同一路径在两次独立解释器里得到不同 ID）。

    **断言目标是预先算好的确定值，不是「同进程调用两次比对」**：后者测不出
    hash() 的 bug，因为 hash() 在单进程内本来就是稳定的，只有跨进程才会翻车。
    预期值 = hashlib.sha1(b"n/b.md").hexdigest()[:8]，与实现算法独立核对，
    不依赖当前进程的 PYTHONHASHSEED。
    """
    from scripts.analyze_metrics import _stable_path_id

    assert _stable_path_id("n/b.md") == "#e808b633"
    assert _stable_path_id("技术笔记/hook.md") == "#affd2af5"


def test_report_near_miss_id_matches_stable_id(tmp_path):
    """报表实际展示的 ID 与 `_stable_path_id` 一致——防止 `render_report` 内联
    另一套（可能仍是 `hash()`）实现绕过上面那条单测。"""
    _seed(tmp_path)
    out = render_report(summarize(load_records(tmp_path)))
    assert "#e808b633" in out


def test_sample_near_miss_ranks_by_frequency(tmp_path):
    from scripts.analyze_metrics import sample_near_miss
    recs = [
        {"_schema": 1, "near_miss": [{"path": "a.md", "topical": 5.8},
                                     {"path": "b.md", "topical": 4.1}]},
        {"_schema": 1, "near_miss": [{"path": "a.md", "topical": 5.9}]},
    ]
    out = sample_near_miss(recs, k=2)
    assert out[0]["path"] == "a.md" and out[0]["count"] == 2
    assert out[0]["topical_max"] == 5.9


def test_annotations_roundtrip_and_last_write_wins(tmp_path):
    from scripts.analyze_metrics import save_annotation, load_annotations, annotations_path
    save_annotation(tmp_path, "a.md", "relevant")
    save_annotation(tmp_path, "b.md", "irrelevant")
    save_annotation(tmp_path, "a.md", "unsure")
    got = load_annotations(tmp_path)
    assert got == {"a.md": "unsure", "b.md": "irrelevant"}
    assert annotations_path(tmp_path).parent.name == "vault-loader-metrics"


def test_save_annotation_rejects_unknown_verdict(tmp_path):
    from scripts.analyze_metrics import save_annotation
    import pytest as _pytest
    with _pytest.raises(ValueError):
        save_annotation(tmp_path, "a.md", "maybe")


def test_load_annotations_reports_bad_lines_not_silent(tmp_path, capsys):
    """评审修 1：坏行必须计数并经 stderr 报出，不能像旧实现那样静默丢弃。

    两类坏行给**不同的条数**（2 行解析/结构异常 + 1 行 verdict 非法），断言钉在
    每一类各自的数量与类别文案上——弱断言「stderr 非空 + 某个数字出现过」测不出
    「只留一类计数、另一类被删掉重新变回静默」这种回归：复评变异（只删
    bad_verdict 那半边计数与消息）已证实旧断言在该缺陷下仍然全绿，因为
    `bad_record=1` 本身就满足了 `"1" in err`。两类给同一条数（都是 1）同样测不
    出「数字对调」，所以特意让两类条数不同。
    """
    from scripts.analyze_metrics import save_annotation, load_annotations, annotations_path
    save_annotation(tmp_path, "a.md", "relevant")
    p = annotations_path(tmp_path)
    with open(p, "a", encoding="utf-8") as f:
        f.write("not json at all\n")                                       # 解析失败 #1
        f.write("{broken\n")                                                # 解析失败 #2
        f.write(json.dumps({"path": "b.md", "verdict": "maybe"}) + "\n")    # verdict 非法 #1
    got = load_annotations(tmp_path)
    assert got == {"a.md": "relevant"}          # 坏行不进结果，好行不受影响
    err = capsys.readouterr().err
    assert "2 行解析/结构异常" in err, f"应报出 2 行解析/结构异常，实际 stderr={err!r}"
    assert "1 行 verdict 非法" in err, f"应报出 1 行 verdict 非法，实际 stderr={err!r}"


def test_load_annotations_rejects_non_string_path(tmp_path, capsys):
    """path 非字符串必须按坏行处理，不得让 CLI 崩溃或静默吞掉标注（L-SEC-2）。

    旧判据是 `r.get("path")` 的真值：
    - `{"path": ["a.md"]}` 通过判据 → `out[list]` 抛 **unhashable type**，
      `--review` 未捕获崩溃，用户已存的标注一条也读不出来；
    - `{"path": 123}` 更隐蔽——int 可哈希，静默存成 int 键，此后与任何字符串
      路径都不相等，那条标注等于凭空消失，且全程零告警。

    annotations.jsonl 是本机文件，损坏来源是手工编辑或磁盘异常，不能当可信输入。
    条数刻意取 3（不是 1），避免与 verdict 那类的计数对调后仍然对得上。
    """
    from scripts.analyze_metrics import save_annotation, load_annotations, annotations_path
    save_annotation(tmp_path, "good.md", "relevant")
    with open(annotations_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps({"path": ["a.md"], "verdict": "relevant"}) + "\n")   # 不可哈希
        f.write(json.dumps({"path": 123, "verdict": "relevant"}) + "\n")        # 可哈希但非串
        f.write(json.dumps({"path": "   ", "verdict": "relevant"}) + "\n")      # 空白串

    got = load_annotations(tmp_path)                 # 不得抛异常
    assert got == {"good.md": "relevant"}, f"坏行污染了结果：{got}"
    assert all(isinstance(k, str) for k in got), "结果里混入了非字符串键"
    assert "3 行解析/结构异常" in capsys.readouterr().err


def test_purge_and_review_are_mutually_exclusive(tmp_path, monkeypatch):
    """`--purge --review` 必须在解析阶段报错，不能让 purge 静默胜出（L-PY4）。

    旧实现靠 `main()` 里 if/elif 的**书写顺序**定优先级：purge 分支在前，于是
    同传两个 flag 时用户以为要去逐条标注，实际把包括人工标注在内的全部数据
    删了——而 purge 不可逆、标注不可重新生成，且整个过程零提示。

    断言不止于「抛了 SystemExit」，还要断言**没有任何东西被删**：只看退出码的话，
    「先删了再报错」同样满足，而那正是最坏的结果。
    """
    import sys as _sys

    from scripts import _metrics
    from scripts.analyze_metrics import annotations_path, main, save_annotation

    save_annotation(tmp_path, "keep.md", "relevant")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(_sys, "argv", ["analyze_metrics.py", "--purge", "--review"])

    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 2, f"argparse 互斥应以 exit 2 拒绝，实际 {ei.value.code}"
    assert annotations_path(tmp_path).exists(), "拒绝执行时不得删除任何数据"
    assert _metrics.count_annotations(tmp_path) == 1


def test_review_devnull_stdin_returns_2_and_writes_no_annotation(tmp_home):
    """评审修 2：Windows 上 stdin=DEVNULL 时 `sys.stdin.isatty()` 实测返回 True
    （NUL 是字符设备，`_isatty()` 对任意字符设备一律判真），前置护栏对这个最常见
    的自动化重定向形态形同虚设。真正兜底在循环体内的 EOFError 分支——一条都没
    保存过就撞上 EOFError，必须与护栏走同一套契约：返回码 2，且不写任何标注。

    这条测的是 CLI 子进程的退出码契约（真实起进程、真实 stdin=DEVNULL），不是
    `input()` 本身，符合 brief「交互循环不进单测」的约束——`main()` 内部的
    `input()` 调用不受任何 mock/monkeypatch，全部走真实解释器。
    """
    script = Path(__file__).resolve().parents[1] / "scripts" / "analyze_metrics.py"
    _metrics.write_record(tmp_home, "sessA", {
        "_schema": 1, "ts": 1.0, "session": "sessA", "prompt_id": "p0",
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [], "near_miss": [{"path": "notes/foo.md", "topical": 6.2}],
        "n_excluded": 5, "ft": {"path": "", "arm": ""},
    })
    env = dict(os.environ)
    # HOME/USERPROFILE 双设：Windows 上 Path.home() 读 USERPROFILE 而非 HOME，
    # 只设一个无效（照抄 tests/test_metrics_optout.py::_run 的既有写法）。
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home)})
    r = subprocess.run([sys.executable, str(script), "--review"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       env=env, encoding="utf-8", cwd=str(script.parents[1]),
                       # 本用例断言的正是「非交互环境下不得挂起」。不设 timeout 的话，
                       # 一旦 TTY 护栏退化成真去读 stdin，这里会**永远等下去**——
                       # 该失败的用例反而把整个测试跑挂死，且没有任何堆栈可看。
                       # 超时抛 TimeoutExpired => 红，正是要的行为。
                       timeout=60)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    from scripts.analyze_metrics import annotations_path
    assert not annotations_path(tmp_home).exists(), "非交互环境下不应写入任何标注"


def test_review_eof_after_saving_one_returns_0(tmp_home, monkeypatch):
    """评审修 2 的另一条分支（`saved >= 1` 后 EOFError → 维持 return 0）此前零
    覆盖：复评把 `saved == 0` 条件删掉、让 EOFError 一律返回 2，9 个既有用例全绿。

    这条**不走 subprocess**——该分支不涉及 isatty()/DEVNULL 边界，且经真实管道
    在结构上不可达（`isatty()` 对 PIPE 正确返回 False，护栏在读取任何输入前就
    已经 return 2，喂给子进程的内容根本不会被消费）。改用纯 in-process
    monkeypatch：`sys.stdin.isatty()` 打桩为 True（模拟护栏被绕过的场景，与修 2
    要处理的真实场景一致）、`builtins.input` 打桩为「第一次答 r，第二次抛
    EOFError」（模拟用户交互到一半 Ctrl-D）、`Path.home` 指向 tmp_home。

    两个近似 near-miss 条目保证 todo 至少 2 条：第一条被正常回答保存
    （saved 变成 1），第二条触发 EOFError——此时必须走 `saved >= 1` 分支，
    维持 `return 0`，且已保存的那条必须真的落盘（只断言返回码，「返回 0 但
    没保存」这种缺陷版本也能蒙混过去）。
    """
    import builtins
    from scripts import analyze_metrics as am
    _metrics.write_record(tmp_home, "sessA", {
        "_schema": 1, "ts": 1.0, "session": "sessA", "prompt_id": "p0",
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [], "near_miss": [{"path": "notes/foo.md", "topical": 6.2},
                                        {"path": "notes/bar.md", "topical": 3.0}],
        "n_excluded": 5, "ft": {"path": "", "arm": ""},
    })

    class _FakeTTYStdin:
        def isatty(self):
            return True

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_home))
    monkeypatch.setattr(sys, "argv", ["analyze_metrics.py", "--review"])
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin())
    answers = iter(["r"])

    def _fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", _fake_input)

    rc = am.main()
    assert rc == 0
    from scripts.analyze_metrics import load_annotations
    assert load_annotations(tmp_home) == {"notes/foo.md": "relevant"}

# ===== P3（full-review High）：load_records 全量物化 =====

def _seed_bulk(home, n_sessions, per_session, admitted_n):
    """造一批体量可观的记录，用于峰值内存判据。"""
    from scripts import _metrics as m
    for s in range(n_sessions):
        rec = {
            "_schema": m.SCHEMA, "session": f"sess-{s}", "gate": "ok",
            "n_admitted": admitted_n,
            "arm_counts": {"topical": admitted_n},
            "admitted": [{"path": f"notes/very/long/path/segment/{i}-{'x'*40}.md",
                          "topical": 1.0, "total": 2.0, "arm": "topical",
                          "dedup": False, "hits": ["kw1", "kw2", "kw3"]}
                         for i in range(admitted_n)],
            "near_miss": [{"path": f"notes/nm/{i}.md", "topical": 3.0}
                          for i in range(10)],
            "ft": {"path": ""},
        }
        for _ in range(per_session):
            m.write_record(home, f"sess-{s}", rec)


def test_load_records_is_lazy_not_materialized(tmp_path):
    """`load_records` 必须惰性产出，不得先把全部记录堆成 list 再返回。

    终审实测原实现：90 session x 20 条宽记录（105MB）-> 7032ms / **574MB 峰值**；
    对照 30 session x 5 条窄记录仅 7.1ms / 0.49MB。`--report`/`--review` 是用户
    交互命令，随保留期内使用量单调恶化、无自然上限。
    """
    import types
    _seed_bulk(tmp_path, 2, 2, 5)
    got = load_records(tmp_path)
    assert isinstance(got, types.GeneratorType), (
        f"load_records 必须返回生成器以避免全量物化，实际 {type(got).__name__}")


def test_load_records_peak_memory_does_not_scale_with_corpus(tmp_path):
    """峰值内存不得随语料规模线性增长。

    判据用 `tracemalloc` 量 `summarize(load_records(...))` 全程峰值，并与磁盘
    总字节数比较——全量物化时峰值会与磁盘量同阶，流式则远小于它。
    """
    import tracemalloc
    _seed_bulk(tmp_path, 30, 12, 40)
    disk = sum(f.stat().st_size
               for d in _metrics.event_month_dirs(tmp_path)
               for f in d.glob("*.jsonl"))
    assert disk > 2_000_000, f"语料太小，判据无意义（{disk} 字节）"

    tracemalloc.start()
    s = summarize(load_records(tmp_path))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert s["n_events"] == 30 * 12
    # 流式下峰值主要由「单条记录 + 聚合计数器」决定，与总量无关。
    # 留足余量取磁盘量的 1/4：全量物化时峰值 >= 磁盘量同阶（终审实测 105MB -> 574MB）。
    assert peak < disk / 4, (
        f"峰值内存 {peak} 字节 vs 磁盘 {disk} 字节 —— 疑似全量物化")
