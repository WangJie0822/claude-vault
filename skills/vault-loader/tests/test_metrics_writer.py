# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path

from scripts import _metrics


def test_salt_is_stable_and_private(tmp_path):
    s1 = _metrics.get_salt(tmp_path)
    s2 = _metrics.get_salt(tmp_path)
    assert s1 == s2 and len(s1) >= 16
    p = _metrics.metrics_dir(tmp_path) / ".salt"
    assert p.exists()
    if os.name != "nt":
        assert oct(p.stat().st_mode)[-3:] == "600"


def test_hash_is_salted_and_stable(tmp_path):
    salt = _metrics.get_salt(tmp_path)
    a = _metrics.h("内存", salt)
    assert a == _metrics.h("内存", salt)
    assert a != _metrics.h("内存", b"different-salt-value")
    assert len(a) == 16 and "内存" not in a


def test_write_record_one_file_per_session(tmp_path):
    for i in range(3):
        _metrics.write_record(tmp_path, "sess-A", {"_schema": _metrics.SCHEMA, "i": i})
    _metrics.write_record(tmp_path, "sess-B", {"_schema": _metrics.SCHEMA, "i": 9})
    files = sorted(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert len(files) == 2
    a = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(a) == 3


def _concurrent_worker(home, sid):
    """**必须定义在模块级，不能是测试函数内的局部函数。**

    Windows 没有 fork，`multiprocessing` 只能 spawn；spawn 要求 target 能在子进程里
    按「模块名 + 限定名」重新 import，局部函数没有可解析的限定名，于是
    `Process(target=<局部函数>)` 在 Windows 上**必然**抛
    `_pickle.PicklingError: Can't pickle local object ...`——与被测逻辑无关，
    纯粹是测试代码自身跑不起来。实证：仅把本函数挪到模块级、逻辑一字不改，
    同一断言 total==160 即通过。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import _metrics as m
    for i in range(40):
        m.write_record(Path(home), sid, {"_schema": m.SCHEMA, "i": i})


def test_concurrent_writes_preserve_all_records(tmp_path):
    """每会话独立文件 ⇒ **跨 session** 并发下记录守恒。混写同一文件会丢 5%~38%（实测）。

    射程仅限「不同 session 各写各的文件」。**同一 session 被多进程并发写同一文件**
    仍会丢失（实测 31%~34%）并产生撕裂行——见 `write_record` docstring，
    那是已知且刻意接受的限制。
    """
    from multiprocessing import Process

    procs = [Process(target=_concurrent_worker, args=(str(tmp_path), f"sess-{w}"))
             for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    total = sum(len([l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()])
                for f in _metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert total == 160


def test_salt_created_atomically_survives_existing_file(tmp_path):
    """.salt 已被别的进程抢先建好时，必须读它那份而非覆盖。

    覆盖会让抢先者已落盘的 hash 与后续记录永久对不齐，且完全静默。
    对照值**必须含 0x0A**：用 b"\x01"*32 这类不含换行的值会连带绕开
    O_BINARY 缺失的触发条件，让本用例对那个缺陷完全无感（实测教训）。
    """
    import os
    d = _metrics.metrics_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    other = bytes([0x0A, 0x01] * 16)      # 含 LF，故意踩 O_BINARY 触发条件
    fd = os.open(d / ".salt", os.O_CREAT | os.O_EXCL | os.O_WRONLY
                 | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, other)
    finally:
        os.close(fd)
    assert _metrics.get_salt(tmp_path) == other, "不得覆盖抢先者的 salt"


def test_salt_roundtrips_newline_bytes(tmp_path, monkeypatch):
    """落盘 salt 必须与内存值逐字节一致 —— 钉死 O_BINARY 缺失。

    Windows 上 os.open 不带 O_BINARY 会把 0x0A 写成 0x0D 0x0A：首次调用返回的是
    内存值（正确），第二次起从盘读到的是被撑长的损坏值，hash 永久对不上且零告警。
    本用例用全 0x0A 的极端 salt 让该缺陷必现（实测：32 字节落盘变 64 字节，
    `len(raw) >= 16` 的畸形检测放行）。真实随机盐的触发概率约 11.8%。
    """
    payload = bytes([0x0A] * 32)
    monkeypatch.setattr(_metrics.secrets, "token_bytes", lambda k: payload)
    first = _metrics.get_salt(tmp_path)                 # 创建，返回内存值
    on_disk = (_metrics.metrics_dir(tmp_path) / ".salt").read_bytes()
    second = _metrics.get_salt(tmp_path)                # 从盘读
    assert first == payload
    assert len(on_disk) == 32, f"落盘被 CRT 改写成 {len(on_disk)} 字节"
    assert second == payload, "第二次调用必须拿到与首次相同的 salt"


def test_session_id_is_sanitized_into_filename(tmp_path):
    _metrics.write_record(tmp_path, "../../evil/../id", {"_schema": _metrics.SCHEMA})
    files = list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert len(files) == 1
    assert ".." not in str(files[0].relative_to(_metrics.metrics_dir(tmp_path)))


def test_build_record_hashes_all_keywords_but_keeps_hit_words(tmp_path):
    from scripts._decision import Decision, EntryDecision
    salt = _metrics.get_salt(tmp_path)
    d = Decision(
        admitted=[EntryDecision(path="n/a.md", topical=7.0, total=9.0, hits=["内存"],
                                admitted=True, admit_arm="topical", dedup="")],
        excluded=[EntryDecision(path="n/b.md", topical=5.8, total=0.0, hits=[],
                                admitted=False, admit_arm="", dedup="")],
        fulltext_path="n/a.md", fulltext_arm="topical>=6+strong_evidence",
        any_relevant=True, relaxed=False, gate_reason="",
    )
    r = _metrics.build_record(d, {"内存", "泄露", "重启"}, Path("D:/secret/proj"),
                              session_id="sess-A", prompt_id="pid-1", salt=salt,
                              src="typed")
    assert r["_schema"] == _metrics.SCHEMA
    assert r["n_kw"] == 3
    blob = json.dumps(r, ensure_ascii=False)
    # 未命中的关键词绝不以明文出现
    assert "泄露" not in blob and "重启" not in blob
    # 命中词保留明文
    assert r["admitted"][0]["hits"] == ["内存"]
    # cwd 不以明文出现
    assert "secret" not in blob and "proj" not in blob
    # excluded 只留计数 + near_miss 的 topical
    assert r["n_excluded"] == 1
    assert r["near_miss"][0]["topical"] == 5.8
    assert "total" not in r["near_miss"][0]
    # near_miss_k 必须落盘：K 是 Top-K 截断参数，不记录就无法区分未来
    # "excluded 本来就少" 与 "当时 K 设得小" 两种情形（Task 6 评审提出）。
    assert r["near_miss_k"] == 10


def test_build_record_truncates_admitted_to_k_but_keeps_full_aggregates(tmp_path):
    """P1 修复：`admitted` 落盘按 `total` 降序截断到 `admitted_k` 条展示样本，
    但 `n_admitted`/`arm_counts` 必须是截断**前**的真实聚合——真实 Vault 实测
    单轮 `admitted` 可达 58~156 条，未截断落盘体积（11795~29962 字节）超出
    README 声称上界 3197 字节 3.7~9.4 倍。
    """
    from scripts._decision import Decision, EntryDecision
    salt = _metrics.get_salt(tmp_path)
    admitted = [
        EntryDecision(path=f"n/{i}.md", topical=5.0, total=float(30 - i), hits=[],
                     admitted=True,
                     admit_arm="topical" if i % 2 == 0 else "keyword_bypass", dedup="")
        for i in range(25)
    ]
    d = Decision(admitted=admitted, excluded=[], fulltext_path=None, fulltext_arm="",
                any_relevant=True, relaxed=False, gate_reason="")
    r = _metrics.build_record(d, {"内存"}, Path("D:/proj"),
                              session_id="s", prompt_id="p", salt=salt,
                              src="typed", admitted_k=10)
    assert len(r["admitted"]) == 10
    # 截断后仍是按 total 降序的前 10 条（total 30..21 → path n/0.md..n/9.md）
    assert [a["path"] for a in r["admitted"]] == [f"n/{i}.md" for i in range(10)]
    assert r["n_admitted"] == 25, "截断不得影响真实条数统计"
    assert sum(r["arm_counts"].values()) == 25, "arm_counts 总和必须等于截断前真实条数"
    assert r["arm_counts"] == {"topical": 13, "keyword_bypass": 12}
    assert r["admitted_k"] == 10


def test_summarize_uses_full_aggregates_not_truncated_sample(tmp_path):
    """`summarize()` 在截断记录上给出的 `n_admitted`/`arm_dist` 必须与未截断时
    一致——不能因为落盘的 `admitted` 数组只剩 K 条就跟着低报。"""
    from scripts._decision import Decision, EntryDecision
    from scripts.analyze_metrics import load_records, summarize
    salt = _metrics.get_salt(tmp_path)
    admitted = [
        EntryDecision(path=f"n/{i}.md", topical=5.0, total=float(30 - i), hits=[],
                     admitted=True, admit_arm="topical", dedup="")
        for i in range(25)
    ]
    d = Decision(admitted=admitted, excluded=[], fulltext_path=None, fulltext_arm="",
                any_relevant=True, relaxed=False, gate_reason="")
    r = _metrics.build_record(d, {"内存"}, Path("D:/proj"),
                              session_id="s", prompt_id="p", salt=salt,
                              src="typed", admitted_k=5)
    assert len(r["admitted"]) == 5, "前提：确实发生了截断"
    _metrics.write_record(tmp_path, "s", r)
    s = summarize(load_records(tmp_path))
    assert s["n_admitted"] == 25
    assert s["arm_dist"] == {"topical": 25}


def test_summarize_falls_back_to_iterating_admitted_for_legacy_records(tmp_path):
    """旧记录（本轮截断改动前落盘、没有 `n_admitted`/`arm_counts` 字段）必须
    回退到遍历 `admitted` 数组，不崩溃、数字仍正确——彼时 `admitted` 未截断，
    遍历口径等价全量。"""
    from scripts.analyze_metrics import load_records, summarize
    _metrics.write_record(tmp_path, "old-sess", {
        "_schema": 1, "ts": 1.0, "session": "old-sess", "prompt_id": "p0",
        "cwd_h": "x", "kw_h": [], "n_kw": 0, "gate": "", "relaxed": False,
        "admitted": [
            {"path": "n/a.md", "topical": 7.0, "total": 9.0, "arm": "topical",
             "dedup": "", "hits": ["内存"]},
            {"path": "n/b.md", "topical": 6.0, "total": 8.0, "arm": "keyword_bypass",
             "dedup": "", "hits": []},
        ],
        # 刻意不带 n_admitted / arm_counts / admitted_k —— 模拟本轮改动前落盘的旧记录
        "near_miss": [], "n_excluded": 0, "ft": {"path": "", "arm": ""},
    })
    s = summarize(load_records(tmp_path))
    assert s["n_admitted"] == 2
    assert s["arm_dist"] == {"topical": 1, "keyword_bypass": 1}


def test_build_record_rejects_positional_session_and_prompt_id(tmp_path):
    """M3 修复：`session_id`/`prompt_id` 是相邻的同类型位置参数，位置对调会
    静默错位——`flush()` 用 `session` 字段决定落盘文件名，对调后即架空「每会话
    独立 `.jsonl`」的整个设计。已实证：对调后 84 个既有测试全绿，类型系统与
    既有断言都拦不住，只能靠强制 keyword-only 让调用形态本身报 TypeError。"""
    import pytest
    from scripts._decision import Decision
    salt = _metrics.get_salt(tmp_path)
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                any_relevant=False, relaxed=False, gate_reason="")
    with pytest.raises(TypeError):
        # ⚠️ 本用例现在会因「缺必填参数 src」而通过——抛错原因已从「session_id 是
        # keyword-only」变成「缺参数」，它**已不足以单独守护** keyword-only 语义。
        # 真正的守护在 test_build_record_src_is_keyword_only_and_required（用
        # inspect.signature 钉形态）。保留本用例是因为它仍能挡住「把 session_id/
        # prompt_id 改回位置参数」这一具体回归。
        _metrics.build_record(d, set(), Path("D:/proj"), "sess-A", "pid-1", salt)


def test_build_record_session_field_matches_session_id_kwarg(tmp_path):
    """落盘记录的 `session` 字段必须确实等于传入的 `session_id`（而非被误传
    成 `prompt_id`）——配合上一条 TypeError 守卫，双重钉死 M3。"""
    from scripts._decision import Decision
    salt = _metrics.get_salt(tmp_path)
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                any_relevant=False, relaxed=False, gate_reason="")
    r = _metrics.build_record(d, set(), Path("D:/proj"),
                              session_id="sess-A", prompt_id="pid-1", salt=salt,
                              src="typed")
    assert r["session"] == "sess-A"
    assert r["prompt_id"] == "pid-1"


def test_prune_removes_expired_months_and_reports(tmp_path, capsys):
    import time
    old = time.strftime("%Y-%m", time.localtime(time.time() - 200 * 86400))
    d = _metrics.metrics_dir(tmp_path) / old
    d.mkdir(parents=True)
    (d / "sess-old.jsonl").write_text("{}\n", encoding="utf-8")
    _metrics.write_record(tmp_path, "sess-new", {"_schema": _metrics.SCHEMA})
    n = _metrics.prune_expired(tmp_path, retention_days=90)
    assert n == 1
    assert "vault-loader" in capsys.readouterr().err        # 不得静默
    assert list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))


def test_purge_counts_and_clears_annotations(tmp_path):
    """人工标注不可重新生成——被清掉可以，但绝不能不计数、不告知。

    早期实现只在 is_dir 分支累加，annotations.jsonl 落进 elif 被删却计 0，
    CLI 会打印「已清空 0 个」，用户毫无察觉标注已没。
    """
    _metrics.get_salt(tmp_path)
    _metrics.write_record(tmp_path, "sess-A", {"_schema": _metrics.SCHEMA})
    ann = _metrics.metrics_dir(tmp_path) / "annotations.jsonl"
    ann.write_text('{"k":"a","v":"relevant"}\n{"k":"b","v":"irrelevant"}\n',
                   encoding="utf-8")
    assert _metrics.count_annotations(tmp_path) == 2
    n = _metrics.purge(tmp_path)
    # 精确值：1 个会话文件（sess-A.jsonl）+ 1 个 annotations.jsonl（内含 2 行标注，
    # 但按「文件数」计，不是「行数」）= 2。当前场景不存在过度计数路径，改精确能
    # 拦住未来的回归（如 annotations.jsonl 被误按行数累加进 n）。
    assert n == 2, "会话文件 + annotations.jsonl 都要计入"
    assert not ann.exists()
    assert _metrics.count_annotations(tmp_path) == 0


def test_purge_clears_all_but_keeps_salt(tmp_path):
    _metrics.get_salt(tmp_path)
    _metrics.write_record(tmp_path, "sess-A", {"_schema": _metrics.SCHEMA})
    assert _metrics.purge(tmp_path) >= 1
    assert not list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert (_metrics.metrics_dir(tmp_path) / ".salt").exists()


def test_count_annotations_tolerates_invalid_utf8(tmp_path):
    """annotations.jsonl 若因写入中断留下非 UTF-8 字节（同类风险见 write_record
    docstring 讨论的并发撕裂行），count_annotations 必须容错读、不抛异常。

    UnicodeDecodeError 不是 OSError 的子类（继承关系为 ValueError），旧实现的
    `except OSError` 拦不住它——往 annotations.jsonl 写入非法 UTF-8 字节后调用
    会直接崩给调用方（Task 11 会把它接进 --purge 前置读取，此时无优雅降级）。
    """
    d = _metrics.metrics_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "annotations.jsonl"
    p.write_bytes(b'{"k":"a"}\n\xff\xfe invalid utf8 bytes\n')
    assert _metrics.count_annotations(tmp_path) == 2


def test_prune_keeps_month_exactly_at_retention_boundary(tmp_path, capsys):
    """恰好等于保留期截止月份的目录必须保留（`prune_expired` 用 `d.name < keep_from`
    严格小于，见其 docstring「边界」说明），不得连该月一起删掉。"""
    import time
    retention_days = 90
    boundary = time.strftime("%Y-%m", time.localtime(time.time() - retention_days * 86400))
    d = _metrics.metrics_dir(tmp_path) / boundary
    d.mkdir(parents=True)
    kept = d / "sess-boundary.jsonl"
    kept.write_text("{}\n", encoding="utf-8")
    n = _metrics.prune_expired(tmp_path, retention_days=retention_days)
    assert n == 0
    assert kept.exists()
    assert capsys.readouterr().err == ""     # 未删除任何东西，不应打印告警


def test_flush_prunes_on_first_call_and_marks_timestamp(tmp_path):
    """H2 修复：`retention_days` 非 None 时，首次 `flush()` 必须触发一次清理
    （`_prune_due` 对不存在的 `prune_ts.json` 视为「早已到期」）并写下时间戳。"""
    import time
    old = time.strftime("%Y-%m", time.localtime(time.time() - 200 * 86400))
    d = _metrics.metrics_dir(tmp_path) / old
    d.mkdir(parents=True)
    (d / "sess-old.jsonl").write_text("{}\n", encoding="utf-8")

    _metrics.reset()
    _metrics.flush(tmp_path, retention_days=90)

    assert not d.exists()      # 过期月份已被清理
    assert (_metrics.metrics_dir(tmp_path) / "prune_ts.json").exists()


def test_flush_skips_scan_within_24h_of_last_prune(tmp_path, monkeypatch):
    """24 小时内再次调用必须直接跳过——`prune_expired` 不应被再次触发
    （即「不做任何目录扫描」的可观测判据：真正做扫描的函数零调用）。"""
    _metrics.mark_pruned(tmp_path)      # 模拟「刚清理过」

    called = []
    monkeypatch.setattr(_metrics, "prune_expired",
                        lambda *a, **k: called.append(1) or 0)
    _metrics.reset()
    _metrics.flush(tmp_path, retention_days=90)

    assert called == []


def test_flush_prunes_again_after_24h(tmp_path, monkeypatch):
    """超过 24 小时后必须再次执行清理。"""
    import time
    ts_dir = _metrics.metrics_dir(tmp_path)
    ts_dir.mkdir(parents=True, exist_ok=True)
    (ts_dir / "prune_ts.json").write_text(
        json.dumps({"ts": time.time() - 25 * 3600}), encoding="utf-8")

    called = []
    monkeypatch.setattr(_metrics, "prune_expired",
                        lambda *a, **k: called.append(1) or 0)
    _metrics.reset()
    _metrics.flush(tmp_path, retention_days=90)

    assert called == [1]


def test_flush_prune_failure_does_not_propagate(tmp_path, monkeypatch, capsys):
    """④ 清理抛异常时 `flush()` 仍须正常返回、不影响主流程——本轮已 stage 的
    记录必须仍正常落盘，异常只留 stderr 痕迹。"""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(_metrics, "prune_expired", boom)

    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "session": "sess-A", "x": 1})
    _metrics.flush(tmp_path, retention_days=90)      # 不得抛出

    files = list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert len(files) == 1                           # 本轮记录仍正常落盘
    assert "metrics 清理失败" in capsys.readouterr().err


def test_flush_without_retention_days_skips_prune_entirely(tmp_path):
    """`retention_days=None`（对应 metrics 未开启时的调用形态）必须保持旧行为：
    不做任何 prune 相关 IO——连 `prune_ts.json` 都不创建。"""
    _metrics.reset()
    _metrics.flush(tmp_path)      # 默认 retention_days=None

    assert not (_metrics.metrics_dir(tmp_path) / "prune_ts.json").exists()


def test_near_miss_carries_dedup(tmp_path):
    """near_miss 必须落 dedup——用于区分「被去重抑制」与「打分不够」。

    实测 2590 条 near_miss 里 173 条（6.7%，下界）是本会话早前已 admitted 的
    同一篇；它们 topical 高、稳定排在榜首，会让 --report 与 nudge 误报
    「这篇老是擦肩而过」，而它其实已经成功召回过。
    """
    from scripts._decision import Decision, EntryDecision
    salt = _metrics.get_salt(tmp_path)
    d = Decision(
        admitted=[],
        excluded=[
            EntryDecision(path="n/dedup.md", topical=9.0, total=0.0, hits=[],
                          admitted=False, admit_arm="", dedup="fulltext_injected"),
            EntryDecision(path="n/plain.md", topical=8.0, total=0.0, hits=[],
                          admitted=False, admit_arm="", dedup=""),
        ],
        fulltext_path=None, fulltext_arm="",
        any_relevant=False, relaxed=False, gate_reason="",
    )
    r = _metrics.build_record(d, {"内存"}, Path("D:/proj"),
                              session_id="s", prompt_id="p", salt=salt, src="typed")
    by_path = {nm["path"]: nm for nm in r["near_miss"]}
    assert by_path["n/dedup.md"]["dedup"] == "fulltext_injected"
    assert by_path["n/plain.md"]["dedup"] == ""
    # 既有性能护栏不变：near_miss 仍不落 total（补算实测 +150~200ms，会顶穿 UPS 预算）
    assert "total" not in by_path["n/plain.md"]


def test_build_record_records_src(tmp_path):
    """记录必须落 src —— 没有它就无法把召回按来源拆开看。

    事件级实测（259 条）：typed 113 / sdk 93 / 无字段 37 /
    suggestion_accepted 12 / queued 1。sdk 占 35.9%、且占全文注入 51.8%，
    其输入是 `Branch:/Range:` 提交元信息模板，与人类提问必须能分开统计。
    """
    from scripts._decision import Decision, EntryDecision
    salt = _metrics.get_salt(tmp_path)
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=False, relaxed=False, gate_reason="")
    r = _metrics.build_record(d, {"内存"}, Path("D:/proj"),
                              session_id="s", prompt_id="p", salt=salt, src="typed")
    assert r["src"] == "typed"


def test_build_record_src_is_keyword_only_and_required(tmp_path):
    """src 与 session_id/prompt_id 同为相邻同型字符串，必须 keyword-only。

    用 inspect.signature 钉形态而非 pytest.raises(TypeError)：后者是弱断言，
    再加一个位置参数时仍会因「参数过多」抛 TypeError、照样绿，
    但它宣称守护的那件事已经变了。
    """
    import inspect
    params = inspect.signature(_metrics.build_record).parameters
    positional = [n for n, p in params.items()
                  if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert positional == ["decision", "prompt_keywords", "cwd"]
    for name in ("session_id", "prompt_id", "salt", "src"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    # src 无默认值——漏传必须报错，不能静默落成 ""（"" 是合法取值，见
    # tests/integration/test_prompt_submit.py 的空串按用户输入处理用例）
    assert params["src"].default is inspect.Parameter.empty
