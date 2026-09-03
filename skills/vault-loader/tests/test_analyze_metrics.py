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
            # near_miss 刻意不带 dedup、记录整体刻意不带 src —— 本 fixture 充当
            # 「旧记录」回归覆盖（本机已积累 259 条这种形态），补齐了就等于删掉
            # 新旧混读那几条测试的对照组。
            "near_miss": [{"path": "n/b.md", "topical": 5.8}],
            "n_excluded": 100, "ft": {"path": "n/a.md" if i == 2 else "", "arm": ""},
        })


def test_summarize_counts(tmp_path):
    _seed(tmp_path)
    s = summarize(load_records(tmp_path))
    assert s["n_events"] == 3
    assert s["gate_dist"]["too_few_keywords"] == 1
    assert s["arm_dist"]["topical"] == 2
    # 分母是**走到打分的轮次**（gate=="ok"），不是全部事件：闸门早退的轮次压根没
    # 走到全文判定，计进分母会稀释出一个既不是注入率也不是命中率的数
    # （真实数据实测两种口径相差 36.2% vs 48.7%）。3 条里 1 条 too_few_keywords。
    assert s["n_ok"] == 2
    assert s["fulltext_rate"] == 1 / 2


def _seed_new(home, session="sess-N"):
    """新格式记录：带 `near_miss_scorelow`（生成侧已排除去重条目）。

    与 `_seed` 的旧格式并存，是为了钉住「新旧混读」两条路径都不出错——
    旧记录不该进新榜单，新记录必须进。
    """
    _metrics.write_record(home, session, {
        "_schema": 1, "ts": 10.0, "session": session, "prompt_id": "pn",
        "cwd_h": "abc123", "kw_h": ["h1"], "n_kw": 1, "src": "",
        "gate": "", "relaxed": False,
        "admitted": [{"path": "n/hit.md", "topical": 7.0, "total": 9.0,
                      "arm": "topical", "dedup": "", "hits": ["内存"]}],
        "n_admitted": 1, "arm_counts": {"topical": 1}, "admitted_k": 20,
        "near_miss": [
            {"path": "n/sup.md", "topical": 9.9, "dedup": "fulltext_injected"},
            {"path": "n/low.md", "topical": 3.5, "dedup": ""},
        ],
        "near_miss_scorelow": [{"path": "n/low.md", "topical": 3.5}],
        "n_excluded": 50, "ft": {"path": "", "arm": ""}, "near_miss_k": 10,
    })


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
    _seed_new(tmp_path)
    out = render_report(summarize(load_records(tmp_path)))
    assert "知识库历史内容" in out          # INJECTION_NOTICE
    assert "n/low.md" not in out            # 默认不露明文路径
    out2 = render_report(summarize(load_records(tmp_path)), show_paths=True)
    assert "n/low.md" in out2


def test_report_suppressed_board_also_hides_paths(tmp_path):
    """**两个榜单都必须走同一个展示出口。**

    新增榜单最容易犯的错是直接 f-string 拼 path——那会同时退化两件事且都无声：
    ① 隐私默认值（默认隐去路径）；② `sanitize_injected_text` 净化（报表可能被喂进
    模型上下文，而笔记路径是不可信外部输入，可嵌换行伪造报表行）。
    """
    _seed_new(tmp_path)
    out = render_report(summarize(load_records(tmp_path)))
    assert "被去重抑制" in out               # 对照榜存在
    assert "n/sup.md" not in out             # 但默认同样不露明文
    assert "#6f4d3f3b" in out                # 走的是 _stable_path_id
    out2 = render_report(summarize(load_records(tmp_path)), show_paths=True)
    assert "n/sup.md" in out2


def test_report_marks_legacy_records_excluded_from_board(tmp_path):
    """旧记录不进新榜单，但必须在报表里**说出来**。

    静默排除会让用户以为「这周没有擦肩笔记」，而真相是「这批数据无法判断成因」。
    """
    _seed(tmp_path)                          # 全部是无 near_miss_scorelow 的旧记录
    out = render_report(summarize(load_records(tmp_path)))
    assert "旧记录未纳入本榜" in out
    assert "n/b.md" not in out               # 旧记录的 near_miss 不进榜单


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
    _seed_new(tmp_path)
    out = render_report(summarize(load_records(tmp_path)))
    assert "#7e12080b" in out               # n/low.md


def test_sample_near_miss_ranks_by_frequency(tmp_path):
    from scripts.analyze_metrics import sample_near_miss
    recs = [
        {"_schema": 1, "near_miss_scorelow": [{"path": "a.md", "topical": 3.8},
                                              {"path": "b.md", "topical": 3.1}]},
        {"_schema": 1, "near_miss_scorelow": [{"path": "a.md", "topical": 3.9}]},
    ]
    out = sample_near_miss(recs, k=2)
    assert out[0]["path"] == "a.md" and out[0]["count"] == 2
    assert out[0]["topical_max"] == 3.9


def test_sample_near_miss_excludes_dedup_suppressed(tmp_path):
    """**人工标注是唯一不可再生的数据，抽样池不能是污染的。**

    改动前 `sample_near_miss` 读未分类的 `near_miss`，于是让人对着「其实早就成功
    注入过」的笔记回答「它该不该被召回」——恒真判断，既浪费判断力，又把这条通道
    的表观精度虚高。真实数据实测：改动前 20 个候选里 13 个以去重抑制为主。
    """
    from scripts.analyze_metrics import sample_near_miss
    recs = [{
        "_schema": 1,
        # 被抑制的 topical 更高（它过了闸门才会被去重），若按 near_miss 排它必居首
        "near_miss": [{"path": "sup.md", "topical": 9.9, "dedup": "fulltext_injected"},
                      {"path": "low.md", "topical": 3.2, "dedup": ""}],
        "near_miss_scorelow": [{"path": "low.md", "topical": 3.2}],
    }]
    out = sample_near_miss(recs, k=5)
    assert [x["path"] for x in out] == ["low.md"], "被去重抑制的笔记不得进抽样池"


def test_sample_near_miss_skips_legacy_records(tmp_path):
    """旧记录（无 `near_miss_scorelow`）整条跳过，而不是回退到过滤 `near_miss`。

    回退过滤看似更宽容，实则拿到的是**有系统性偏斜的残差**：旧记录的 near_miss
    是「全部 excluded 按 topical 取 top-k」，而被去重的条目 topical 结构性更高
    （fulltext_injected 分支不看 topical 就 excluded，可达 11；score-low 必 < 4），
    真实数据里 39% 的轮次一条 score-low 都没留下。
    """
    from scripts.analyze_metrics import sample_near_miss
    recs = [{"_schema": 1, "near_miss": [{"path": "old.md", "topical": 3.5}]}]
    assert sample_near_miss(recs, k=5) == []


def test_annotations_roundtrip_and_last_write_wins(tmp_path):
    from scripts.analyze_metrics import save_annotation, load_annotations, annotations_path
    save_annotation(tmp_path, "a.md", "relevant")
    save_annotation(tmp_path, "b.md", "irrelevant")
    save_annotation(tmp_path, "a.md", "unsure")
    got = load_annotations(tmp_path)
    # 键是 (kind, path)：缺 kind 的旧记录与显式 near_miss 归同一键
    assert got == {("near_miss", "a.md"): "unsure",
                   ("near_miss", "b.md"): "irrelevant"}
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
    assert got == {("near_miss", "a.md"): "relevant"}   # 坏行不进结果，好行不受影响
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
    assert got == {("near_miss", "good.md"): "relevant"}, f"坏行污染了结果：{got}"
    # 键必须是 (kind, path) 二元组且两半都是字符串——非串 path 会让键悄悄变形，
    # 之后与真实标注永不相等，等于标注凭空消失（L-SEC-2 的同源风险）。
    assert all(isinstance(k, tuple) and len(k) == 2
               and all(isinstance(x, str) for x in k) for k in got), \
        f"结果里混入了非法键：{got}"
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


def _seed_review_context(tmp_home, session, prompt_text):
    """写一条 transcript，使 `--review` 能为该 session 回查出提问上下文。

    新逻辑会剔除「一条可读上下文都没有」的条目（盲标只会产出 unsure），所以
    测退出码契约的用例必须先让条目可判断，否则流程在「没有待标注的条目」处
    就提前返回了，走不到被测分支。返回该 prompt 的加盐 hash，供记录使用。
    """
    import json as _json
    proj = tmp_home / ".claude" / "projects" / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session}.jsonl").write_text(
        _json.dumps({"type": "user", "entrypoint": "cli",
                     "message": {"content": prompt_text}},
                    ensure_ascii=False) + "\n", encoding="utf-8")
    return _metrics.h(prompt_text, _metrics.get_salt(tmp_home))


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
    _ph = _seed_review_context(tmp_home, "sessA", "这轮问的是内存泄露")
    _metrics.write_record(tmp_home, "sessA", {
        "_schema": 1, "ts": 1.0, "session": "sessA", "prompt_id": "p0",
        "prompt_h": _ph,
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [], "near_miss": [{"path": "notes/foo.md", "topical": 3.2,
                                       "dedup": ""}],
        "near_miss_scorelow": [{"path": "notes/foo.md", "topical": 3.2}],
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
    _ph = _seed_review_context(tmp_home, "sessA", "这轮问的是内存泄露")
    _metrics.write_record(tmp_home, "sessA", {
        "_schema": 1, "ts": 1.0, "session": "sessA", "prompt_id": "p0",
        "prompt_h": _ph,
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [], "near_miss": [{"path": "notes/foo.md", "topical": 3.8,
                                       "dedup": ""},
                                      {"path": "notes/bar.md", "topical": 3.0,
                                       "dedup": ""}],
        "near_miss_scorelow": [{"path": "notes/foo.md", "topical": 3.8},
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
    assert load_annotations(tmp_home) == {("near_miss", "notes/foo.md"): "relevant"}

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
                          # 生产值域是 "" | "fulltext_injected" | "candidate_injected"
                          # 三种字符串（_decision.py:34），原先写 False 会让按 dedup
                          # 分桶的统计产出一个 False 桶。
                          "dedup": "", "hits": ["kw1", "kw2", "kw3"]}
                         for i in range(admitted_n)],
            # near_miss 刻意不带 dedup —— 本 fixture 充当「旧记录」回归覆盖，
            # 补齐了就等于删掉 test_summarize_buckets_near_miss_by_dedup_and_
            # tolerates_legacy 的对照组。
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


def test_summarize_buckets_near_miss_by_dedup_and_tolerates_legacy():
    """报表按 dedup 分桶；且必须容忍旧记录（259 条已落盘的 near_miss 无该键）。

    旧记录一律归入「未知」桶，不能 KeyError，也不能与真实的 "" 值混为一谈——
    前者是「这条记录写于加字段之前」，后者是「打分不够」，含义不同。
    """
    recs = [
        {"_schema": _metrics.SCHEMA, "gate": "", "ft": {"path": "", "arm": ""},
         "n_admitted": 0, "arm_counts": {},
         "near_miss": [{"path": "a.md", "topical": 9.0, "dedup": "fulltext_injected"},
                       {"path": "b.md", "topical": 8.0, "dedup": ""}]},
        # 旧记录：near_miss 无 dedup 键
        {"_schema": _metrics.SCHEMA, "gate": "", "ft": {"path": "", "arm": ""},
         "n_admitted": 0, "arm_counts": {},
         "near_miss": [{"path": "c.md", "topical": 7.0}]},
    ]
    s = summarize(recs)
    assert s["near_miss_dedup_dist"]["fulltext_injected"] == 1
    assert s["near_miss_dedup_dist"]["打分不够"] == 1
    assert s["near_miss_dedup_dist"]["未知(旧记录)"] == 1
    # 渲染不得崩，且要把分桶显示出来
    out = render_report(s)
    assert "fulltext_injected" in out


def test_annotations_separate_by_kind(tmp_path):
    """同一篇笔记在两类标注里互不顶掉。

    「作为擦肩候选该不该被召回」与「作为已注入内容召回得对不对」是两个独立
    判断；改动前 load_annotations 按 path 去重，两者会互相覆盖。
    """
    from scripts.analyze_metrics import save_annotation, load_annotations
    save_annotation(tmp_path, "n/x.md", "irrelevant", kind="near_miss")
    save_annotation(tmp_path, "n/x.md", "relevant", kind="admitted_fulltext")
    got = load_annotations(tmp_path)
    assert got[("near_miss", "n/x.md")] == "irrelevant"
    assert got[("admitted_fulltext", "n/x.md")] == "relevant"


def test_legacy_annotation_without_kind_reads_as_near_miss(tmp_path):
    """已完成的 20 条标注没有 kind 键，必须视为 near_miss——它们正是那一类。"""
    from scripts.analyze_metrics import annotations_path, load_annotations
    p = annotations_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_schema": _metrics.SCHEMA,
                             "path": "old.md", "verdict": "relevant"}) + "\n",
                 encoding="utf-8")
    assert load_annotations(tmp_path) == {("near_miss", "old.md"): "relevant"}


def test_save_annotation_rejects_unknown_kind(tmp_path):
    from scripts.analyze_metrics import save_annotation
    with pytest.raises(ValueError):
        save_annotation(tmp_path, "n/x.md", "relevant", kind="bogus")


def test_save_annotation_kind_is_keyword_only(tmp_path):
    """kind 与 path/verdict 是相邻同型字符串，位置传参会静默错位。

    用 inspect.signature 钉形态而非 pytest.raises(TypeError)——后者是弱断言，
    再加一个位置参数时仍会因「参数过多」抛 TypeError、照样绿，
    但它宣称守护的那件事已经变了。
    """
    import inspect
    from scripts.analyze_metrics import save_annotation
    params = inspect.signature(save_annotation).parameters
    positional = [n for n, p in params.items()
                  if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert positional == ["home", "path", "verdict"]
    assert params["kind"].kind is inspect.Parameter.KEYWORD_ONLY


def test_sample_admitted_only_human_sources():
    """精度抽样排除**确知**自动化的来源，其余（含 src 缺失/空串）一律纳入。

    sdk 事件的输入是 `Branch:/Range:` 提交元信息模板，无语义可判相关与否，
    放进抽样池只会浪费判断力。

    **判据方向是黑名单，不是白名单。** 原实现是白名单
    `src not in ("typed","queued","suggestion_accepted") ⇒ 跳过`，而 hook stdin
    根本不下发 `promptSource`（Claude Code 2.1.220 二进制实证），真实数据里 `src`
    恒为 `""` ⇒ **每条记录都被跳过，本函数自 0.9.0 上线起恒返回 []**，精度标注
    通道 100% 空转。而当时的用例手工构造 `src="typed"` 这种生产中从未存在过的
    形态，因此全绿——脚手架对它要测的那个缺陷免疫。
    下面的 `n/legacy.md` 断言方向即为此翻转（另见
    `test_sample_admitted_accepts_real_build_record_output` 的端到端覆盖）。
    """
    from scripts.analyze_metrics import sample_admitted
    recs = [
        {"_schema": _metrics.SCHEMA, "src": "typed",
         "ft": {"path": "n/full.md", "arm": "topical>=6+strong_evidence"},
         "admitted": [{"path": "n/full.md", "topical": 11.0, "total": 12.0},
                      {"path": "n/a.md", "topical": 7.0, "total": 8.0},
                      {"path": "n/b.md", "topical": 6.0, "total": 7.0}],
         "near_miss": []},
        {"_schema": _metrics.SCHEMA, "src": "sdk",
         "ft": {"path": "n/sdk.md", "arm": "topical>=6+strong_evidence"},
         "admitted": [{"path": "n/sdk.md", "topical": 11.0, "total": 12.0}],
         "near_miss": []},
        # src 键缺失：harness 未下发 ⇒ 按人类输入处理（这是生产中的**唯一**形态）
        {"_schema": _metrics.SCHEMA,
         "ft": {"path": "n/legacy.md", "arm": ""},
         "admitted": [{"path": "n/legacy.md", "topical": 9.0, "total": 9.0}],
         "near_miss": []},
        # src 为空串：同上。写端注释明写「'' 是合法取值（空串按用户输入处理）」
        {"_schema": _metrics.SCHEMA, "src": "",
         "ft": {"path": "n/empty.md", "arm": ""},
         "admitted": [{"path": "n/empty.md", "topical": 9.0, "total": 9.0}],
         "near_miss": []},
    ]
    got = sample_admitted(recs, k=20)
    paths = {x["path"] for x in got}
    assert "n/sdk.md" not in paths, "sdk 来源不得进入精度抽样池"
    assert "n/legacy.md" in paths, "src 缺失=harness 未下发，必须按人类输入纳入"
    assert "n/empty.md" in paths, "src 空串必须按人类输入纳入"
    kinds = {x["path"]: x["kind"] for x in got}
    assert kinds["n/full.md"] == "admitted_fulltext"
    assert kinds["n/a.md"] == "admitted_list"
    assert kinds["n/b.md"] == "admitted_list"


def test_sample_admitted_accepts_real_build_record_output(tmp_path):
    """**端到端**：`build_record` 的真实产出必须能通过精度抽样的来源闸门。

    这是本轮补的关键守卫。此前 `sample_admitted` 的两条用例都手工构造
    `{"src": "typed", ...}` 的 dict——一种**生产中从未存在过**的形态，与真正的
    `build_record` 完全解耦。于是当来源闸门与真实产出对不上时（`src` 恒为 `""`
    而白名单不含 `""`），功能 100% 空转、测试却全绿，直到有人去查真实数据。

    本用例刻意走「hook 侧实际传入的值」这条路：`prompt_submit_load` 传的是
    `hook_input.get("promptSource") or ... or ""`，而该键不在 stdin payload 里
    ⇒ 生产中恒为 `""`。把闸门改回白名单会让这条立刻转红。
    """
    from scripts._decision import Decision, EntryDecision
    from scripts.analyze_metrics import sample_admitted
    d = Decision(
        admitted=[EntryDecision(path="n/real.md", topical=11.0, total=12.0,
                                hits=["内存"], admitted=True, admit_arm="topical",
                                dedup="")],
        excluded=[], fulltext_path="n/real.md",
        fulltext_arm="topical>=6+strong_evidence", gate_reason="", relaxed=False,
        any_relevant=True,
    )
    rec = _metrics.build_record(
        d, ["内存"], tmp_path,
        session_id="s", prompt_id="p", salt=_metrics.get_salt(tmp_path),
        src="",          # ← 生产中的唯一真实取值
    )
    got = sample_admitted([rec], k=20)
    assert [x["path"] for x in got] == ["n/real.md"], (
        "build_record 的真实产出必须能进精度抽样池；"
        f"实际 src={rec.get('src')!r}，got={got}"
    )


def test_sample_admitted_respects_k():
    from scripts.analyze_metrics import sample_admitted
    recs = [{"_schema": _metrics.SCHEMA, "src": "typed",
             "ft": {"path": "", "arm": ""},
             "admitted": [{"path": f"n/{i}.md", "topical": 6.0, "total": 7.0}
                          for i in range(50)],
             "near_miss": []}]
    # 每轮最多取 top-3 清单条目，故 50 条 admitted 只产出 3 个候选
    assert len(sample_admitted(recs, k=20)) == 3


# ── 标注上下文：prompt_h + transcript 回查 ──────────────────────────────

def _write_transcript(home, session, texts):
    """造一个最小的 transcript，形态照 ~/.claude/projects/<proj>/<session>.jsonl。"""
    d = home / ".claude" / "projects" / "proj-X"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for t in texts:
        lines.append(json.dumps({
            "type": "user", "uuid": "u-" + t[:4], "sessionId": session,
            "timestamp": "2026-08-26T00:00:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": t}]},
        }, ensure_ascii=False))
    # 混入非 user 条目与坏行，确保解析器不被它们带偏
    lines.insert(0, json.dumps({"type": "assistant", "message": {"content": "noise"}}))
    lines.append("{ not json")
    (d / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_build_record_persists_prompt_hash(tmp_path):
    """prompt_h 必须落盘 —— 它是 --review 回查原文的唯一定位键。

    变异验证：删掉 build_record 里的 "prompt_h" 那行，本用例转红。
    """
    from scripts._decision import Decision, EntryDecision
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=True, relaxed=False, gate_reason="")
    salt = b"s" * 16
    rec = _metrics.build_record(d, ["kw"], tmp_path, session_id="s", prompt_id="p",
                                salt=salt, src="", prompt="我问了什么")
    assert rec["prompt_h"] == _metrics.h("我问了什么", salt)
    # 不落原文 —— 隐私边界不可放宽
    blob = json.dumps(rec, ensure_ascii=False)
    assert "我问了什么" not in blob, "prompt 原文绝不能出现在落盘记录里"


def test_build_record_prompt_hash_empty_when_no_prompt(tmp_path):
    """没有 prompt 时落空串，不落一个「空串的 hash」——后者会让读端误以为有上下文。"""
    from scripts._decision import Decision
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=True, relaxed=False, gate_reason="")
    rec = _metrics.build_record(d, ["kw"], tmp_path, session_id="s", prompt_id="p",
                                salt=b"x" * 16, src="")
    assert rec["prompt_h"] == ""


def test_lookup_prompt_matches_by_hash_not_position(tmp_path):
    """按 hash 精确定位到那一条，而不是按顺序或时间戳猜。

    刻意让目标不是第一条、也不是最后一条 —— 位置无关性正是 hash 方案的价值：
    时间戳就近匹配实测只有 82.8% 能唯一定位。

    变异验证：把 lookup_prompt 改成返回第一条 user message，本用例转红。
    """
    from scripts.analyze_metrics import lookup_prompt
    salt = b"z" * 16
    _write_transcript(tmp_path, "sess-1", ["第一个问题", "中间那个问题", "最后一个问题"])
    target = _metrics.h("中间那个问题", salt)
    assert lookup_prompt(tmp_path, "sess-1", target, salt) == "中间那个问题"


def test_lookup_prompt_returns_empty_when_unresolvable(tmp_path):
    """transcript 缺失 / hash 对不上 / 参数为空，一律返回空串，绝不抛。

    --review 是人工标注入口，任何一条取不回上下文都不该中断整轮标注。
    """
    from scripts.analyze_metrics import lookup_prompt
    salt = b"z" * 16
    _write_transcript(tmp_path, "sess-1", ["只有这一条"])
    assert lookup_prompt(tmp_path, "sess-1", _metrics.h("不存在的", salt), salt) == ""
    assert lookup_prompt(tmp_path, "no-such-session", "abc", salt) == ""
    assert lookup_prompt(tmp_path, "", "abc", salt) == ""
    assert lookup_prompt(tmp_path, "sess-1", "", salt) == ""


def test_sample_admitted_events_finds_context_per_kind(tmp_path):
    """三类标注对象各自能找到它被召回时的事件（用于取回提问原文）。"""
    from scripts.analyze_metrics import sample_admitted_events
    recs = [
        {"_schema": 1, "session": "s1", "prompt_h": "h1",
         "admitted": [{"path": "n/a.md"}], "ft": {"path": "n/f.md"},
         "near_miss_scorelow": [{"path": "n/nm.md", "topical": 3.5}]},
    ]
    assert sample_admitted_events(recs, "n/a.md", "admitted_list") == [("s1", "h1")]
    assert sample_admitted_events(recs, "n/f.md", "admitted_fulltext") == [("s1", "h1")]
    assert sample_admitted_events(recs, "n/nm.md", "near_miss") == [("s1", "h1")]
    assert sample_admitted_events(recs, "n/zzz.md", "admitted_list") == []


def test_sample_admitted_events_skips_records_without_hash(tmp_path):
    """旧记录（无 prompt_h）不该被当成有上下文 —— 否则界面会显示「找不到」而非「无上下文」。"""
    from scripts.analyze_metrics import sample_admitted_events
    recs = [{"_schema": 1, "session": "s1", "admitted": [{"path": "n/a.md"}]}]
    assert sample_admitted_events(recs, "n/a.md", "admitted_list") == []


# ── M7/M8/L2/L3：判据可观测性、版本门、锁不变量、不静默 ─────────────────

def test_supported_schemas_covers_current_writer_version():
    """受支持集合必须包含写端当前版本，否则新写的记录一条都读不出来。

    变异验证：把 SUPPORTED_SCHEMAS 改成 frozenset({999})，本用例转红。
    """
    from scripts.analyze_metrics import SUPPORTED_SCHEMAS
    assert _metrics.SCHEMA in SUPPORTED_SCHEMAS


def test_unsupported_schema_records_are_counted_not_silent(tmp_path, capsys):
    """版本不受支持的记录被丢弃时必须经 stderr 报出条数，不能静默消失。

    此前是 `== SCHEMA` 严格相等 + 无提示，于是 bump 版本的代价被固定成
    「静默丢掉全部历史」—— 这让 SCHEMA 变成单向门，任何语义变更都无路可走。

    变异验证：去掉 dropped_ver 的 stderr 分支，本用例转红。
    """
    _metrics.write_record(tmp_path, "s", {
        "_schema": 999, "ts": 1.0, "session": "s", "prompt_id": "p", "gate": "",
    })
    recs = list(load_records(tmp_path))
    assert recs == [], "未来版本的记录不该被当成当前版本读进来"
    assert "schema 版本不受支持" in capsys.readouterr().err


def test_src_distribution_is_observable(tmp_path):
    """来源分布必须进 summarize —— 黑名单判据默认放行，不统计就完全静默。

    失效场景：harness 日后开始下发一个新的自动化来源值（`agent` 之类），
    它会静默进入 --review 的人类标注池，没有任何信号。上一次白名单判据
    空转 8 天没被发现，正是因为没人看得见这个字段的真实取值。

    变异验证：去掉 _acc_gate_and_admitted 里的 src_dist 累加，本用例转红。
    """
    for i, src in enumerate(["", "sdk", "", "agent"]):
        _metrics.write_record(tmp_path, f"s{i}", {
            "_schema": 1, "ts": float(i), "session": f"s{i}", "prompt_id": "p",
            "gate": "", "src": src,
        })
    s = summarize(load_records(tmp_path))
    assert s["src_dist"] == {"(空)": 2, "sdk": 1, "agent": 1}
    # 报表只在出现非空值时打印这一行（恒空时是噪声）
    assert "来源分布" in render_report(s)


def test_system_prompt_sources_are_subset_of_non_human():
    """不变量：我们拒绝为之注入的来源，不可能算作人类提问。

    两处「非人类来源」词表各自独立维护（prompt_submit_load 的
    `_SYSTEM_PROMPT_SOURCES` 决定是否跳过注入；analyze_metrics 的
    `_NON_HUMAN_SRC` 决定是否排出标注池），成员不同是**有意**的
    ——SDK prompt 该注入、但不该进精度标注池。但没有任何东西把两者联系起来，
    harness 日后真下发 promptSource 时，新增自动化来源的人极可能只改一处。

    因 `_metrics.py` 的零内部依赖不变量禁止共享常量模块，这条断言 + 双向注释
    引用是可行的最强绑定。
    """
    import re
    from scripts.analyze_metrics import _NON_HUMAN_SRC
    psl = (Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py")
    src = psl.read_text(encoding="utf-8")
    m = re.search(r"_SYSTEM_PROMPT_SOURCES\s*=\s*frozenset\(\{([^}]*)\}\)", src)
    assert m, "解析失败：没找到 _SYSTEM_PROMPT_SOURCES 定义（判据本身失效了）"
    system_sources = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert system_sources, "解析到空集合，判据无判别力"
    assert system_sources <= set(_NON_HUMAN_SRC), (
        f"{system_sources - set(_NON_HUMAN_SRC)} 被判为「不为之注入」，"
        f"却仍会进入 --review 的人类标注池")


def test_annotate_speaks_up_on_rejected_key(capsys):
    """越界键与撞键都要出声 —— 静默丢弃与「功能没上线」现象完全一样。

    变异验证：把两个 print 去掉，本用例转红。
    """
    _metrics.stage({"_schema": _metrics.SCHEMA, "kw_h": []})
    try:
        _metrics.annotate(inj_char=1)          # 少个 s
        assert "忽略未登记字段" in capsys.readouterr().err
        _metrics.annotate(inj_chars=10)
        _metrics.annotate(inj_chars=20)        # 撞键
        assert "撞键" in capsys.readouterr().err
    finally:
        _metrics._PENDING = None               # 不把缓冲留给后续用例


def test_reset_counts_takes_the_lock(tmp_path, monkeypatch):
    """reset_counts 必须走 `_bump_locked` 声明的锁协议，拿不到锁时如实报错。

    竞态：某 hook 进程正处在「已 read、未 write」窗口内时，它随后的 write_text
    会把重置前读到的计数原样写回 —— 用户看到「已清空（N 条）」却一条没少，
    且完全无声，这个函数存在的目的被反转。

    变异验证：把 reset_counts 里的取锁去掉，本用例转红。
    """
    _metrics.bump_near_miss_counts(tmp_path, ["a.md"])
    assert _metrics.load_near_miss_counts(tmp_path) == {"a.md": 1}
    monkeypatch.setattr(_metrics, "_acquire_counts_lock", lambda home: None)
    with pytest.raises(RuntimeError, match="正被其他进程写入"):
        _metrics.reset_counts(tmp_path)
    assert _metrics.load_near_miss_counts(tmp_path) == {"a.md": 1}, \
        "拿不到锁就不该删任何东西"


# ── M4：并排的两个榜单必须同源，才当得起「对照」二字 ────────────────────

def test_both_boards_share_the_same_population(tmp_path):
    """「真·擦肩」与「对照·被去重抑制」两榜只统计新格式记录。

    两栏并排渲染、表头写着「对照」，读者会拿计数直接相比。而真·擦肩榜按设计排除
    旧记录（它们的 near_miss 样本在截断阶段已被去重条目挤占），对照榜若跨新旧就是
    apples-to-oranges。真实数据上更极端：新格式记录为 0 时一栏全空、另一栏全满，
    读者的自然结论「我没有擦肩笔记」恰好是错的。

    变异验证：把 summarize 里 suppressed 的 `is_new and` 去掉，本用例转红
    （old/sup.md 会出现在对照榜里）。

    ⚠️ 对照组必须是 **0.9.0 形态**（`near_miss` 条目**带**非空 dedup、但没有
    `near_miss_scorelow`），不能用 `_seed` —— 后者模拟的是更早的形态、near_miss
    条目连 dedup 键都没有，会走 `"dedup" not in nm` 分支计入「未知(旧记录)」，
    **根本到不了 suppressed 那一行**。用它做对照组时，去掉 `is_new` 的变异不转红，
    看起来像守卫无判别力，实际是用例没走到。本机 1000+ 条历史记录正是 0.9.0 形态。
    """
    _metrics.write_record(tmp_path, "old-090", {
        "_schema": 1, "ts": 10.0, "session": "old-090", "prompt_id": "po",
        "gate": "", "n_admitted": 1, "arm_counts": {"topical": 1},
        # 0.9.0 形态：有 dedup，无 near_miss_scorelow
        "near_miss": [{"path": "old/sup.md", "topical": 9.5,
                       "dedup": "fulltext_injected"}],
    })
    _metrics.write_record(tmp_path, "new-s", {
        "_schema": 1, "ts": 20.0, "session": "new-s", "prompt_id": "pn",
        "gate": "", "n_admitted": 1, "arm_counts": {"topical": 1},
        "near_miss": [
            {"path": "new/sup.md", "topical": 9.0, "dedup": "fulltext_injected"},
            {"path": "new/low.md", "topical": 3.5, "dedup": ""},
        ],
        "near_miss_scorelow": [{"path": "new/low.md", "topical": 3.5}],
    })
    s = summarize(load_records(tmp_path))
    sup_paths = {p for p, _ in s["near_miss_suppressed_top"]}
    board_paths = {p for p, _ in s["near_miss_top"]}
    assert sup_paths == {"new/sup.md"}, \
        f"对照榜混入了旧记录条目：{sup_paths}"
    assert board_paths == {"new/low.md"}
    # 成因分布刻意保持**全量**口径（含旧记录），由报表文案说明——这是有意的不对称：
    # 它回答的是「excluded 条目都因为什么落榜」，本就该看全部数据。
    assert s["near_miss_dedup_dist"]["fulltext_injected"] == 2, \
        "成因分布必须仍是全量口径（新旧各一条 fulltext_injected）"


# ── M5：抽样池与报表口径必须对齐渲染层的 max_notes ──────────────────────

def _rec_admitted(n, ft_path="", max_notes=None):
    r = {"_schema": _metrics.SCHEMA, "src": "", "gate": "",
         "ft": {"path": ft_path, "arm": "topical>=6" if ft_path else ""},
         "admitted": [{"path": f"n/{i}.md", "topical": 6.0, "total": 9.0 - i}
                      for i in range(n)],
         "near_miss": []}
    if ft_path:
        r["admitted"].insert(0, {"path": ft_path, "topical": 8.0, "total": 99.0})
    if max_notes is not None:
        r["max_notes"] = max_notes
    return r


def test_sample_admitted_list_side_matches_rendered_count_with_fulltext():
    """有全文时清单侧只渲染 max_notes-1 条 —— 抽样池不得多抓那一条。

    渲染层是 `rest = [...][: max_notes - 1]`（prompt_submit_load.py:341），
    全文那篇**计入** max_notes 之内。此前这里硬编码取 3，于是每个 ft 轮次都会有
    1 条从未进过模型上下文的条目混进标注池，而人工标注是唯一不可再生的数据。

    变异验证：把 `limit = mn - 1 if ft_path else mn` 改回 `limit = 3`，本用例转红。
    """
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted([_rec_admitted(5, ft_path="n/ft.md", max_notes=3)], k=20)
    lists = sorted(x["path"] for x in got if x["kind"] == "admitted_list")
    fts = [x["path"] for x in got if x["kind"] == "admitted_fulltext"]
    assert fts == ["n/ft.md"]
    assert lists == ["n/0.md", "n/1.md"], \
        f"max_notes=3 且有全文 ⇒ 清单侧只该有 2 条，实际 {lists}"


def test_sample_admitted_list_side_without_fulltext():
    """无全文时清单侧渲染满 max_notes 条。"""
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted([_rec_admitted(5, max_notes=3)], k=20)
    lists = sorted(x["path"] for x in got if x["kind"] == "admitted_list")
    assert lists == ["n/0.md", "n/1.md", "n/2.md"]


def test_sample_admitted_honours_non_default_max_notes():
    """max_notes 可配 —— 用户改成 1 时，有全文的轮次清单侧一条都不该抓。"""
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted([_rec_admitted(5, ft_path="n/ft.md", max_notes=1)], k=20)
    assert [x["kind"] for x in got] == ["admitted_fulltext"], \
        f"max_notes=1 且有全文 ⇒ 清单侧渲染 0 条，实际 {got}"


def _recs_fulltext_starved():
    """复刻真实数据形态：清单侧条目又多又高频，全文侧稀疏低频。

    本机实测（0.9.1 后 283 轮）：admitted_list 有 497 个条目、count 最大 74；
    admitted_fulltext 有 209 个条目、count 最大 17；而全局 top20 的门槛是 22。
    """
    recs = []
    for _ in range(30):                       # 25 个清单路径，各出现 30 次
        for g in range(0, 25, 3):
            paths = [f"L{j}.md" for j in range(g, min(g + 3, 25))]
            recs.append({
                "_schema": _metrics.SCHEMA, "src": "", "gate": "",
                "ft": {"path": "", "arm": ""}, "max_notes": 3,
                "admitted": [{"path": p, "topical": 6.0, "total": 9.0}
                             for p in paths],
                "near_miss": [],
            })
    for _ in range(5):                        # 3 个全文路径，各出现 5 次
        for f in range(3):                    # max_notes=1 + 有全文 => 清单侧 0 条
            recs.append({
                "_schema": _metrics.SCHEMA, "src": "", "gate": "",
                "ft": {"path": f"F{f}.md", "arm": "topical>=6"}, "max_notes": 1,
                "admitted": [{"path": f"F{f}.md", "topical": 8.0, "total": 99.0}],
                "near_miss": [],
            })
    return recs


def test_sample_admitted_reserves_quota_for_fulltext():
    """全文侧必须有独立配额，否则被清单侧结构性挤出、精度永远测不到。

    0.9.0 把 kind 分成 admitted_fulltext / admitted_list，理由是「代价差一个
    量级……混成一类之后就分不出是哪边错了」。但排序是全局 top-k，而清单侧每轮
    贡献 max_notes-1 条、全文侧每轮至多 1 条 => 清单侧 count 系统性更高。
    本机实测：全局 top20 门槛 22、全文侧最大 17，**一条都进不去**，于是 283 轮
    数据下 admitted_fulltext 的人工标注恒为 0 —— 而全文正是代价最大的召回形态
    （单篇上限 8192 字节、占 42% 的轮次）。

    变异验证：把返回值改回全局 `sorted(...)[:k]`，本用例转红。
    """
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted(_recs_fulltext_starved(), k=20)
    fts = [x for x in got if x["kind"] == "admitted_fulltext"]
    assert fts, "全文侧被清单侧完全挤出，精度标注通道恒空"


def _recs_list_starved():
    """反向形态：全文侧条目多，清单侧只有两条。"""
    recs = []
    for i in range(15):
        recs.append({
            "_schema": _metrics.SCHEMA, "src": "", "gate": "",
            "ft": {"path": f"F{i}.md", "arm": "topical>=6"}, "max_notes": 1,
            "admitted": [{"path": f"F{i}.md", "topical": 8.0, "total": 99.0}],
            "near_miss": [],
        })
    for i in range(2):
        recs.append({
            "_schema": _metrics.SCHEMA, "src": "", "gate": "",
            "ft": {"path": "", "arm": ""}, "max_notes": 1,
            "admitted": [{"path": f"L{i}.md", "topical": 6.0, "total": 9.0}],
            "near_miss": [],
        })
    return recs


def test_sample_admitted_quota_backfills_when_fulltext_scarce():
    """全文侧不足配额时清单侧补满 —— 池子不因分配额而缩水。

    抽样池本就只有 k 条，而人工标注不可再生；若不回填，全文侧只有 3 条时
    池子会从 20 缩到 13，白白少标 7 条。

    变异验证：把 `n_list = min(len(lists), k - n_ft)` 改成 `min(len(lists), k // 2)`，
    本用例转红（20 -> 13）。
    """
    import collections
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted(_recs_fulltext_starved(), k=20)
    assert len(got) == 20, f"共 28 个条目、k=20，池子不该缩水，实际 {len(got)}"
    kinds = collections.Counter(x["kind"] for x in got)
    assert kinds["admitted_fulltext"] == 3, "3 条全文应全部进池"
    assert kinds["admitted_list"] == 17, "其余槽位由清单侧补满"


def test_sample_admitted_quota_backfills_when_list_scarce():
    """清单侧不足时全文侧回填 —— 配额必须是对称的。

    变异验证：删掉 `n_ft = min(len(fts), k - n_list)` 那行，本用例转红
    （全文侧被钉死在 k//2=10，总数 12）。
    """
    import collections
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted(_recs_list_starved(), k=20)
    kinds = collections.Counter(x["kind"] for x in got)
    assert kinds["admitted_list"] == 2
    assert kinds["admitted_fulltext"] == 15, \
        f"清单侧只有 2 条，全文侧应回填到 15，实际 {kinds['admitted_fulltext']}"
    assert len(got) == 17


def test_sample_admitted_quota_splits_evenly_when_both_ample():
    """两侧都充足时各占 k//2 —— 这是配额的主场景。

    变异验证：把 `n_ft = min(len(fts), k // 2)` 改成 `min(len(fts), k)`，
    本用例转红（全文侧独占 20 条，清单侧 0 条）。
    """
    import collections
    from scripts.analyze_metrics import sample_admitted
    got = sample_admitted(_recs_fulltext_starved() + _recs_list_starved(), k=20)
    kinds = collections.Counter(x["kind"] for x in got)
    assert kinds["admitted_fulltext"] == 10, \
        f"两侧都充足时全文侧应占 k//2=10，实际 {kinds['admitted_fulltext']}"
    assert kinds["admitted_list"] == 10


def test_render_span_uses_persisted_max_notes(tmp_path):
    """报表的「实际渲染 N 篇」按记录真值，不硬编码。

    变异验证：把 `_render_span` 改回返回固定字符串，本用例转红。
    """
    _metrics.write_record(tmp_path, "s", {
        "_schema": 1, "ts": 1.0, "session": "s", "prompt_id": "p", "gate": "",
        "n_admitted": 10, "arm_counts": {"topical": 10}, "max_notes": 7,
        "near_miss": [], "near_miss_scorelow": [],
    })
    out = render_report(summarize(load_records(tmp_path)))
    assert "实际渲染 ≤7 篇" in out
    assert "≤4 篇" not in out, "旧的硬编码口径不得再出现"


def test_render_span_reports_config_drift(tmp_path):
    """样本期内 max_notes 变动过时，报表必须如实说明而不是只报众数。"""
    for i, mn in enumerate([3, 3, 8]):
        _metrics.write_record(tmp_path, f"s{i}", {
            "_schema": 1, "ts": float(i), "session": f"s{i}", "prompt_id": "p",
            "gate": "", "n_admitted": 1, "arm_counts": {"topical": 1},
            "max_notes": mn, "near_miss": [], "near_miss_scorelow": [],
        })
    out = render_report(summarize(load_records(tmp_path)))
    assert "该配置变动过" in out and "实际渲染 ≤3 篇" in out


def test_render_report_on_empty_says_no_data(tmp_path):
    """空库的报表不得暗示「有数据只是太旧」，也不该打印零路径时的隐去提示。"""
    out = render_report(summarize(load_records(tmp_path)))
    assert "无数据" in out and "metrics.enabled" in out
    assert "路径已隐去" not in out
    assert "新口径只统计本版之后落盘的记录" not in out


# ── S2：畸形磁盘字段不得让整个 CLI 归零 ──────────────────────────────────

def test_report_survives_corrupt_numeric_field(tmp_path):
    """一条记录的数值字段被改坏，其余记录必须照常统计。

    修复前实测：把某条的 `inj_chars` 改成字符串，`--report` 直接 `exit=1`、
    **stdout 完全为空** —— 同文件里的合法记录连同上千条历史一起拿不到，
    用户看到的是 Python 堆栈而不是「跳过 N 行损坏记录」。而 `--report`
    恰恰是排障入口，最不该在数据异常时自己先罢工。

    变异验证：去掉 `load_records` 里的 `_drop_bad_numeric_fields(r)` 调用，
    本用例转红（summarize 抛 ValueError）。
    """
    _seed_new(tmp_path, session="ok-sess")
    _metrics.write_record(tmp_path, "bad-sess", {
        "_schema": 1, "ts": 11.0, "session": "bad-sess", "prompt_id": "pb",
        "gate": "", "n_admitted": 3, "arm_counts": {"topical": 3},
        "inj_chars": "tampered",          # ← 手工改坏
        "near_miss": [], "near_miss_scorelow": [],
    })
    s = summarize(load_records(tmp_path))
    assert s["n_events"] == 2, "坏记录不该被整条丢弃，只丢那个字段"
    # 坏字段被删 ⇒ 按「该记录没有 inj_chars」处理，不并入均值
    assert s["inj_chars_n"] == 0
    assert render_report(s), "报表必须仍能渲染"


def test_corrupt_nested_fields_do_not_crash(tmp_path):
    """嵌套字段（arm_counts / admitted[].topical / near_miss[].dedup）各自兜住。

    它们不在 `_NUMERIC_TOP_FIELDS` 的覆盖范围内 —— 顶层 coerce 解决不了嵌套结构，
    这三处必须各写各的守卫。
    """
    from scripts.analyze_metrics import sample_admitted
    _metrics.write_record(tmp_path, "s", {
        "_schema": 1, "ts": 1.0, "session": "s", "prompt_id": "p", "gate": "",
        "n_admitted": 2, "arm_counts": {"topical": "NaN", "keyword_bypass": 2},
        "src": "", "ft": {"path": "", "arm": ""},
        "admitted": [{"path": "n/a.md", "topical": {"bad": 1}, "total": 5.0}],
        "near_miss": [{"path": "n/b.md", "topical": 1.0, "dedup": ["not-a-str"]}],
        "near_miss_scorelow": [],
    })
    recs = list(load_records(tmp_path))
    s = summarize(recs)
    assert s["arm_dist"] == {"keyword_bypass": 2}, "坏值那一臂跳过，好值那一臂保留"
    assert render_report(s)
    assert sample_admitted(recs, k=5), "topical 坏值按 0 记，条目本身仍进池"


def test_configure_context_accepts_legacy_namespace(tmp_home):
    """`analyze_metrics` 对未迁移用户默认传的就是 "legacy"，必须映射到 0.9.x 布局。

    此前 "legacy" 落进 else 分支、又不在 `{claude, codex, unknown}` 白名单里，被改写成
    "unknown" ⇒ `metrics_dir` 指向 `~/.context-vault/metrics/unknown`（空目录）。
    全套用例只传过 "unknown"（conftest 的默认值），而它恰好映射回 legacy，
    所以这条分叉此前一条用例都碰不到。
    """
    _metrics.configure_context("legacy")
    assert _metrics.metrics_dir(tmp_home) == tmp_home / ".claude" / "vault-loader-metrics"

    # 对照组：canonical 存在时显式 runtime 才走新命名空间。缺了它，本用例对
    # 「两个命名空间是否被混淆」没有判别力。
    canonical = tmp_home / ".context-vault" / "config.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("{}", encoding="utf-8")
    _metrics.configure_context("claude")
    assert _metrics.metrics_dir(tmp_home) == tmp_home / ".context-vault" / "metrics" / "claude"

    # 未知取值回落 legacy，而不是凭空造一个没人会看的孤儿命名空间
    _metrics.configure_context("wat")
    assert _metrics.metrics_dir(tmp_home) == tmp_home / ".claude" / "vault-loader-metrics"


def test_purge_actually_deletes_legacy_data_for_unmigrated_user(tmp_home):
    """未迁移用户跑 `--purge` 必须真的删掉数据。

    这是文档里唯一的用户侧隐私删除控件，删的是加盐关键词 hash、**明文笔记路径**、
    **明文 session id** 与不可再生的人工标注。此前它打印「已清空 0 个数据文件」
    + rc=0，而数据原样留在盘上——与「本来就没有数据」完全不可区分。

    走真实 CLI 子进程，不走内部函数：命名空间是在 `main()` 里选定的，
    直接调 `purge()` 会绕过那一层，正是此前漏检的原因。
    """
    script = Path(__file__).resolve().parents[1] / "scripts" / "analyze_metrics.py"
    _metrics.configure_context("legacy")
    _metrics.write_record(tmp_home, "sessA", {
        "_schema": 1, "ts": 1.0, "session": "sessA", "prompt_id": "p0",
        "cwd_h": "x", "kw_h": ["h1"], "n_kw": 1, "gate": "", "relaxed": False,
        "admitted": [], "near_miss": [], "n_excluded": 0, "ft": {"path": "", "arm": ""},
    })
    legacy_dir = tmp_home / ".claude" / "vault-loader-metrics"
    (legacy_dir / "annotations.jsonl").write_text(
        json.dumps({"path": "n/a.md", "label": "relevant"}) + "\n", encoding="utf-8")
    before = sorted(p.name for p in legacy_dir.rglob("*.jsonl"))
    assert before, "前置条件：legacy 目录里应有数据文件（否则本用例零判别力）"

    env = dict(os.environ)
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home)})
    r = subprocess.run([sys.executable, "-B", str(script), "--purge"],
                       capture_output=True, text=True, env=env, encoding="utf-8",
                       cwd=str(script.parents[1]), timeout=60)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    remaining = sorted(p.name for p in legacy_dir.rglob("*.jsonl"))
    assert not remaining, f"--purge 未删除 legacy 数据：{remaining}；stdout={r.stdout!r}"
    assert "已清空 0 个数据文件" not in r.stdout, \
        f"报告了 0 个文件，但删除前确有 {len(before)} 个：{r.stdout!r}"


def test_purge_all_covers_legacy_namespace(tmp_home):
    """迁移后 `--purge`（auto→all）必须连 legacy 目录一起清。

    迁移是「复制」不是「移动」：legacy 目录在迁移后原样留在盘上，含加盐 hash、
    明文笔记路径、明文 session id 与不可再生的人工标注。只清新命名空间却报告
    「已清空 N 个数据文件」，会让用户以为隐私数据已删除。
    """
    script = Path(__file__).resolve().parents[1] / "scripts" / "analyze_metrics.py"
    # 造出「已迁移」的现场：canonical config **加上** migration.json 的 committed 标记。
    # 只写 config 是不够的——命名空间翻转的判据是迁移是否真的提交过（`--set-default`
    # 只写配置不搬数据，不应让命名空间切走），此时 `claude` 仍会落在 legacy 布局。
    canonical = tmp_home / ".context-vault" / "config.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('{"_config_version": 2}', encoding="utf-8")
    (tmp_home / ".context-vault" / "migration.json").write_text(
        '{"schema": 1, "status": "committed"}', encoding="utf-8")
    rec = {"_schema": 1, "ts": 1.0, "session": "s", "prompt_id": "p", "cwd_h": "x",
           "kw_h": ["h"], "n_kw": 1, "gate": "", "relaxed": False, "admitted": [],
           "near_miss": [], "n_excluded": 0, "ft": {"path": "", "arm": ""}}
    _metrics.configure_context("legacy")
    _metrics.write_record(tmp_home, "sessLegacy", rec)
    _metrics.configure_context("claude")
    _metrics.write_record(tmp_home, "sessClaude", rec)
    legacy_dir = tmp_home / ".claude" / "vault-loader-metrics"
    claude_dir = tmp_home / ".context-vault" / "metrics" / "claude"
    assert list(legacy_dir.rglob("*.jsonl")) and list(claude_dir.rglob("*.jsonl")), \
        "前置条件：两个命名空间都要有数据，否则本用例零判别力"

    env = dict(os.environ)
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home)})
    r = subprocess.run([sys.executable, "-B", str(script), "--purge"],
                       capture_output=True, text=True, env=env, encoding="utf-8",
                       cwd=str(script.parents[1]), timeout=60)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not list(legacy_dir.rglob("*.jsonl")), f"legacy 未被清理；stdout={r.stdout!r}"
    assert not list(claude_dir.rglob("*.jsonl")), f"claude 未被清理；stdout={r.stdout!r}"
    # 扫描目录必须打出来——只报总数时「漏扫」与「本来就空」不可区分
    assert "扫描目录" in r.stdout and "vault-loader-metrics" in r.stdout
