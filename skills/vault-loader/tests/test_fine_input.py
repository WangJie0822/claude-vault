# -*- coding: utf-8 -*-
"""精排子进程输入构造（spec 2026-10-13 §6.3）：transcript 最近 K 轮、本项目最近工作、宽召回词、
排除集、摘要层与目录层、统一编号与净化。"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from scripts import _fine_input as FI
from scripts._fine_input import (build_fine_input, clean_text, exclusion_set,
                                 note_name, project_rels, recent_user_prompts, recent_work_titles,
                                 safe_transcript_path, wide_recall_words, worklog_files)
from scripts._frontmatter_reader import Entry
from scripts._signal_collect import collect_signal_f_recent_worklogs

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(["11111111", "2222", "3333", "4444", "555555555555"])


def E(path, tags=(), summary="", mtime=0, keywords=(), category=""):
    return Entry(path=path, tags=tuple(tags), category=category, summary=summary, mtime=mtime,
                 keywords=tuple(keywords))


def _user(text, source="typed", **extra):
    return {"type": "user", "promptSource": source, "message": {"role": "user", "content": text}, **extra}


def _write_jsonl(p: Path, objs=(), raw_lines=()) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        for o in objs:
            f.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
        for r in raw_lines:
            f.write(r + b"\n")
    return p


def _tx(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "projects" / "proj" / f"{SID}.jsonl"


# ---------------- transcript 路径校验（F12/F13） ----------------

def test_safe_transcript_path_accepts_only_own_session_file(tmp_path):
    cfg = tmp_path / "cfg"
    good = _write_jsonl(_tx(tmp_path), [_user("x")])
    assert safe_transcript_path(str(good), SID, cfg) == good
    other = _write_jsonl(cfg / "projects" / "proj" / "other.jsonl", [_user("x")])
    assert safe_transcript_path(str(other), SID, cfg) is None              # 文件名不是本会话
    outside = _write_jsonl(tmp_path / "elsewhere" / f"{SID}.jsonl", [_user("x")])
    assert safe_transcript_path(str(outside), SID, cfg) is None            # 不在 <配置目录>/projects/ 下
    assert safe_transcript_path("\\\\host\\share\\" + f"{SID}.jsonl", SID, cfg) is None
    assert safe_transcript_path("//host/share/" + f"{SID}.jsonl", SID, cfg) is None
    assert safe_transcript_path(f"projects/proj/{SID}.jsonl", SID, cfg) is None   # 相对路径
    d = cfg / "projects" / "dir" / f"{SID}.jsonl"
    d.mkdir(parents=True)
    assert safe_transcript_path(str(d), SID, cfg) is None                  # 不是普通文件
    for bad in ("", None, 3):
        assert safe_transcript_path(bad, SID, cfg) is None
    assert safe_transcript_path(str(good), "", cfg) is None


def test_safe_transcript_path_rejects_dotdot_traversal(tmp_path):
    """文本上落在 `<配置目录>/projects/` 之下、归一后却跳出去的路径：必须先归一再比对。"""
    cfg = tmp_path / "cfg"
    _write_jsonl(_tx(tmp_path), [_user("x")])                                 # 让 projects/proj 目录存在，`..` 才走得通
    outside = _write_jsonl(tmp_path / "elsewhere" / f"{SID}.jsonl", [_user("x")])
    sneaky = cfg / "projects" / ".." / ".." / "elsewhere" / f"{SID}.jsonl"
    assert ".." in str(sneaky) and sneaky.is_file()                         # 阳性对照：路径含 .. 且确实指向真文件
    assert os.path.samefile(sneaky, outside)                                  # 它就是 projects 之外的那个文件
    assert safe_transcript_path(str(sneaky), SID, cfg) is None
    inner = cfg / "projects" / "proj" / ".." / "proj" / f"{SID}.jsonl"        # 含 .. 但归一后仍在 projects 内：放行
    assert safe_transcript_path(str(inner), SID, cfg) == inner


def test_safe_transcript_path_rejects_symlink(tmp_path):
    """链接不是普通文件：用 `lstat` 不跟随。真链接用例在没有创建链接权限的机器上 skip，故另配一条
    桩住 `os.lstat` 的确定性用例（不依赖权限）。"""
    cfg = tmp_path / "cfg"
    good = _write_jsonl(_tx(tmp_path), [_user("x")])
    link = cfg / "projects" / "other" / f"{SID}.jsonl"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(good, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("本机无创建符号链接的权限；确定性等价用例见 test_safe_transcript_path_uses_lstat_mode")
    assert link.is_symlink() and link.is_file()                               # 阳性对照：跟随链接它就是普通文件
    assert safe_transcript_path(str(link), SID, cfg) is None


def test_safe_transcript_path_uses_lstat_mode(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    good = _write_jsonl(_tx(tmp_path), [_user("x")])
    assert safe_transcript_path(str(good), SID, cfg) == good
    real = os.lstat

    def as_link(path, *a, **k):
        st = real(path, *a, **k)
        if str(path).endswith(f"{SID}.jsonl"):
            return os.stat_result((stat.S_IFLNK | 0o777,) + tuple(st)[1:10])
        return st

    monkeypatch.setattr(os, "lstat", as_link)
    assert safe_transcript_path(str(good), SID, cfg) is None                  # lstat 报链接 ⇒ 拒；改用 stat 的实现会放行


def test_config_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert FI.config_dir() == tmp_path / "cc"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert FI.config_dir() == Path.home() / ".claude"


# ---------------- 最近 K 轮用户输入 ----------------

def test_recent_user_prompts_filters_real_shapes(tmp_path):
    """真实 transcript 形态（2.1.272–2.1.296）：只取用户手输 / 排队 / 点选建议的纯文本条目。"""
    p = _write_jsonl(_tx(tmp_path), [
        _user("第一个问题 召回"),
        _user("排队的问题", source="queued"),
        _user("点选的建议", source="suggestion_accepted"),
        _user("后台通知正文", source="system"),
        {"type": "user", "promptSource": "typed",
         "message": {"content": [{"type": "tool_result", "content": "工具结果"}]}},
        _user("<bash-stdout>ls</bash-stdout>"),
        _user("<task-notification><task-id>x</task-id></task-notification>"),
        _user("元信息", isMeta=True),
        _user("压缩摘要", isCompactSummary=True),
        _user("子代理的提问", isSidechain=True),
        {"type": "assistant", "message": {"content": "助手回复"}},
        {"type": "user", "promptSource": "typed",
         "message": {"content": [{"type": "text", "text": "块形式的问题"}]}},
        _user("本轮提问"),
    ])
    out, deg = recent_user_prompts(p, 10, "本轮提问")
    assert out == ["第一个问题 召回", "排队的问题", "点选的建议", "块形式的问题"]
    assert deg is None


def test_recent_user_prompts_k_and_caps(tmp_path):
    p = _write_jsonl(_tx(tmp_path), [_user(f"问题{i}") for i in range(5)])
    assert recent_user_prompts(p, 3, "")[0] == ["问题2", "问题3", "问题4"]          # 最近 K 条，时间正序
    p = _write_jsonl(_tx(tmp_path), [_user("长" * 800)])
    assert recent_user_prompts(p, 3, "")[0] == ["长" * FI.K_ITEM_CHARS]
    p = _write_jsonl(_tx(tmp_path), [_user(f"{i}" + "字" * 449) for i in range(6)])
    out, _ = recent_user_prompts(p, 10, "")
    assert sum(len(x) for x in out) <= FI.K_TOTAL_CHARS
    assert out[-1].startswith("5")                                           # 截断从最旧的一端丢


def test_tail_partial_first_line_dropped(tmp_path, monkeypatch):
    """尾部窗口的第一行几乎必然是半行。造一个「半行本身恰好是合法用户条目」的极端形态：
    不丢第一行时它会混进输入。"""
    emb = json.dumps(_user("不该出现"), ensure_ascii=False).encode("utf-8")
    assert json.loads(emb)["type"] == "user"                                 # 阳性对照：半行确是合法条目
    rest = (json.dumps(_user("真问题"), ensure_ascii=False) + "\n").encode("utf-8")
    p = _tx(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"X" * 50 + emb + b"\n" + rest)
    monkeypatch.setattr(FI, "TRANSCRIPT_TAIL_BYTES", len(emb) + 1 + len(rest))   # 窗口恰好从 emb 开头起
    assert recent_user_prompts(p, 5, "") == (["真问题"], None)
    monkeypatch.setattr(FI, "TRANSCRIPT_TAIL_BYTES", 10 ** 6)                     # 整份读：首行本就不合法
    assert recent_user_prompts(p, 5, "") == (["真问题"], None)


def test_tail_cut_inside_multibyte_char(tmp_path, monkeypatch):
    head = (json.dumps(_user("中" * 200), ensure_ascii=False) + "\n").encode("utf-8")
    rest = (json.dumps(_user("后面的问题"), ensure_ascii=False) + "\n").encode("utf-8")
    p = _tx(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(head + rest)
    i = head.index("中".encode("utf-8")) + 1                                  # 落在三字节字符中间
    monkeypatch.setattr(FI, "TRANSCRIPT_TAIL_BYTES", len(head) - i + len(rest))
    assert recent_user_prompts(p, 5, "") == (["后面的问题"], None)


def test_tail_bad_lines_skipped(tmp_path):
    deep = b"[" * 100_000
    with pytest.raises(RecursionError):                                      # 阳性对照：这一行确实会让 json.loads 抛 RecursionError
        json.loads(deep.decode("ascii"))
    p = _write_jsonl(_tx(tmp_path), [_user("好问题")],
                     raw_lines=[b"{", b"not json", b"\xff\xfe\x00", b"[1,2]", b'"str"', b"null", deep])
    assert recent_user_prompts(p, 5, "") == (["好问题"], None)
    q = _write_jsonl(_tx(tmp_path), [], raw_lines=[deep])                     # 深层嵌套行排在合法条目之前
    with open(q, "ab") as f:
        f.write((json.dumps(_user("深行之后的问题"), ensure_ascii=False) + "\n").encode("utf-8"))
    assert recent_user_prompts(q, 5, "") == (["深行之后的问题"], None)


def test_tail_overlong_lines(tmp_path, monkeypatch):
    """超长行：窗口内的合法长行只截到 K_ITEM_CHARS、不挤掉后面的行；比窗口还长的行只剩半行，整行丢弃。"""
    big = (json.dumps(_user("长" * 300_000), ensure_ascii=False) + "\n").encode("utf-8")
    rest = (json.dumps(_user("后面的问题"), ensure_ascii=False) + "\n").encode("utf-8")
    p = _tx(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(big + rest)
    assert recent_user_prompts(p, 5, "") == (["长" * FI.K_ITEM_CHARS, "后面的问题"], None)
    monkeypatch.setattr(FI, "TRANSCRIPT_TAIL_BYTES", len(rest) + 1000)          # 窗口小于长行：只读到它的尾巴
    assert recent_user_prompts(p, 5, "") == (["后面的问题"], None)


def test_recent_user_prompts_degraded_codes(tmp_path):
    assert recent_user_prompts(None, 3, "") == ([], "transcript_err")
    d = tmp_path / "dir.jsonl"
    d.mkdir()
    assert recent_user_prompts(d, 3, "") == ([], "transcript_err")
    p = _write_jsonl(_tx(tmp_path), [_user("x", source="system")])
    assert recent_user_prompts(p, 3, "") == ([], "transcript_none")
    assert recent_user_prompts(p, 0, "") == ([], None)


# ---------------- 本项目最近工作 ----------------

LOG = (
    "---\ntags: [工作日志]\ncategory: 工作日志\n---\n\n# 2026-10-14 工作日志\n\n"
    "## 10:00 ~ 11:00 | 精排补充的时效规则\n\n**项目**: Claude-Vault\n\n### 工作内容\n\n- x\n\n"
    "## 12:00 ~ 13:00 | 别的项目的事\n\n**项目**: other\n\n"
    "## 14:00 ~ 15:00 | 第二件\u200b含«括号»\n\n**项目**: claude-vault\n"
)


def _worklog(vault: Path, rel: str, text: str, age_days: float = 0) -> Path:
    p = vault / "工作日志" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_work_titles_case_insensitive_project(tmp_path):
    vault = tmp_path / "v"
    _worklog(vault, "2026年/10月/2026-10-14.md", LOG)
    titles = recent_work_titles(worklog_files(vault, 7), "claude-vault", 3)
    assert titles == ["精排补充的时效规则", "第二件含'括号'"]


def test_work_titles_real_header_takes_text_after_last_pipe(tmp_path):
    """真实日志头行是 `## 时间 | 时长 | 标题`（summarize-session 的格式），标题取最后一个 `|` 之后。"""
    vault = tmp_path / "v"
    _worklog(vault, "2026年/10月/2026-10-14.md",
             "## 14:30 ~ 16:00 | 1.5h | 重构日志文件识别逻辑\n\n**项目**: cv\n")
    assert recent_work_titles(worklog_files(vault, 7), "cv", 3) == ["重构日志文件识别逻辑"]


def test_worklog_files_skip_index_old_and_appledouble(tmp_path):
    vault = tmp_path / "v"
    new = _worklog(vault, "2026年/10月/2026-10-14.md", LOG)
    older = _worklog(vault, "2026年/10月/2026-10-13.md", LOG, age_days=1)
    _worklog(vault, "2026年/09月/2026-09-01.md", LOG, age_days=30)
    _worklog(vault, "INDEX.md", LOG)
    _worklog(vault, "工作日志 索引.md", LOG)
    _worklog(vault, "2026年/10月/._2026-10-14.md", LOG)
    assert worklog_files(vault, 7) == [new, older]
    assert worklog_files(tmp_path / "none", 7) == []


def test_work_titles_caps(tmp_path):
    vault = tmp_path / "v"
    many = "".join(f"## {i:02d}:00 ~ {i:02d}:30 | 标题{i}\n\n**项目**: cv\n\n" for i in range(20))
    _worklog(vault, "2026年/10月/2026-10-14.md", many)
    assert len(recent_work_titles(worklog_files(vault, 7), "cv", 3)) == FI.MAX_WORK_TITLES
    v2 = tmp_path / "v2"
    _worklog(v2, "2026年/10月/2026-10-14.md", "## 10:00 ~ 11:00 | 今天\n\n**项目**: cv\n")
    _worklog(v2, "2026年/10月/2026-10-12.md", "## 08:00 ~ 09:00 | 别处\n\n**项目**: other\n", age_days=2)
    _worklog(v2, "2026年/10月/2026-10-13.md", "## 09:00 ~ 10:00 | 昨天\n\n**项目**: cv\n", age_days=1)
    files = worklog_files(v2, 7)
    assert recent_work_titles(files, "cv", 1) == ["今天"]                    # max_logs 按「有命中小节的篇」计
    assert recent_work_titles(files, "cv", 3) == ["今天", "昨天"]
    assert recent_work_titles(files, "", 3) == []


def test_worklog_source_matches_signal_f(tmp_path):
    """与信号 F 同源（spec §6.3 F50）：信号 F 找得到的日志，本模块的候选文件里必须都有。"""
    vault = tmp_path / "v"
    _worklog(vault, "2026年/10月/2026-10-14.md", LOG.replace("Claude-Vault", "claude-vault"))
    _worklog(vault, "2026年/10月/2026-10-12.md", LOG, age_days=2)
    sig = collect_signal_f_recent_worklogs(Path("claude-vault"), vault, 7)
    ours = {str(p.relative_to(vault)).replace("\\", "/") for p in worklog_files(vault, 7)}
    assert sig.paths and set(sig.paths) <= ours


# ---------------- 排除集与宽召回词 ----------------

def test_exclusion_set_covers_all_rules():
    entries = {
        "笔记/ok.md": E("笔记/ok.md", tags=("x",)),
        "笔记/arch.md": E("笔记/arch.md", tags=("Archived",)),
        "笔记/priv.md": E("笔记/priv.md", tags=("private",)),
        "项目笔记/cv/cv 索引.md": E("项目笔记/cv/cv 索引.md"),
        "工作日志/2026年/10月/2026-10-14.md": E("工作日志/2026年/10月/2026-10-14.md", category="工作日志"),
        "笔记/inj.md": E("笔记/inj.md"),
        "笔记/exc.md": E("笔记/exc.md"),
        "../escape.md": E("../escape.md"),
    }
    ex = exclusion_set(entries, exclude_note_tags=["archived"], fine_exclude_tags=["private"],
                       injected=["笔记/inj.md"], excluded=["笔记/exc.md"])
    assert ex == set(entries) - {"笔记/ok.md"}


def test_wide_recall_words_union_sorted():
    words = wide_recall_words(["召回打分", "fine-rank tuning", "", None], {})
    assert {"召回", "打分", "fine-rank", "tuning"} <= set(words)
    assert words == sorted(set(words))


def test_wide_recall_words_forwards_jcfg():
    """信号 J 的参数原样转发（与 UPS 同参数）：每一项都改成非默认值，结果必须随之变化。"""
    assert wide_recall_words(["召回打分"], {}) == ["召回", "回打", "打分"]
    assert wide_recall_words(["召回打分"], {"split_cjk_bigram": False}) == ["召回打分"]
    assert wide_recall_words(["召回打分 tuning rank"], {"max_prompt_keywords": 1}) == ["召回"]
    assert wide_recall_words(["fine-rank"], {}) == ["fine", "fine-rank", "rank"]
    assert wide_recall_words(["fine-rank"], {"split_english_token": False}) == ["fine-rank"]
    assert wide_recall_words(["fine-rank"], {"en_subtoken_min": 5}) == ["fine-rank"]
    assert wide_recall_words(["/mycmd tuning"], {}) == ["tuning"]
    assert wide_recall_words(["/mycmd tuning"], {"strip_slash_command": False}) == ["mycmd", "tuning"]


# ---------------- 摘要层、目录层与编号 ----------------

def _entries():
    return {
        "笔记/a.md": E("笔记/a.md", tags=("召回",), summary="召回的设计", mtime=100),
        "笔记/b.md": E("笔记/b.md", summary="打分细节", mtime=200),
        "笔记/c.md": E("笔记/c.md", summary="无关内容", mtime=300),
        "项目笔记/cv/d.md": E("项目笔记/cv/d.md", summary="项目笔记", mtime=400),
        "笔记/e.md": E("笔记/e.md", summary="也无关", mtime=50),
    }


def _cfg(**over):
    c = {"top_n": 1, "project_top_k": 1, "pool_max": 5, "catalog": True, "catalog_max_chars": 80000,
         "weights": {"prompt_tag_hit": 4, "prompt_summary_hit": 2, "prompt_keyword_hit": 5},
         "use_keywords": True, "use_tag_idf": True, "tag_idf_floor": 0.5}
    c.update(over)
    return c


def _build(entries=None, words=("召回", "打分"), **over):
    entries = entries if entries is not None else _entries()
    return build_fine_input(entries, words=list(words), exclude=set(), project_rels=["项目笔记/cv/d.md"],
                            cfg=_cfg(**over), injected=[], work_titles=[], k_texts=[], degraded=[])


def test_summary_layer_then_catalog_with_continuous_numbers():
    fin = _build()
    assert [c.rel for c in fin.candidates] == ["笔记/a.md", "项目笔记/cv/d.md",      # 摘要层：topical 前 1 + 项目 1
                                               "笔记/b.md", "笔记/c.md", "笔记/e.md"]  # 目录层：宽召回命中在前，其余按 mtime
    assert [c.n for c in fin.candidates] == [1, 2, 3, 4, 5]
    assert [c.summary is not None for c in fin.candidates] == [True, True, False, False, False]
    assert (fin.n_summary_layer, fin.n_catalog, fin.n_catalog_dropped) == (2, 3, 0)


def test_catalog_prefers_wide_recall_then_mtime():
    """F63：超预算时先保留宽召回 topical>0 的条目，其余再按 mtime；超出的计入丢弃数。"""
    fin = _build(catalog_max_chars=4)                                         # 文件名 b / c / e 各占 2
    assert [c.rel for c in fin.candidates][2:] == ["笔记/b.md", "笔记/c.md"]
    assert fin.n_catalog_dropped == 1


def test_pool_max_caps_summary_layer_and_catalog_off():
    fin = _build(top_n=10, pool_max=1, catalog=False)
    assert [c.rel for c in fin.candidates] == ["笔记/a.md"]
    assert (fin.n_catalog, fin.n_catalog_dropped) == (0, 0)


def test_empty_words_still_lists_project_notes_and_catalog():
    fin = _build(words=())
    assert fin.candidates[0].rel == "项目笔记/cv/d.md"
    assert [c.rel for c in fin.candidates][1:] == ["笔记/c.md", "笔记/b.md", "笔记/a.md", "笔记/e.md"]


def test_exclude_is_respected():
    fin = build_fine_input(_entries(), words=["召回"], exclude={"笔记/a.md", "笔记/c.md"},
                           project_rels=[], cfg=_cfg(), injected=[], work_titles=[], k_texts=[], degraded=[])
    assert "笔记/a.md" not in [c.rel for c in fin.candidates]
    assert "笔记/c.md" not in [c.rel for c in fin.candidates]


def test_names_and_summaries_are_sanitized():
    rel = "笔记/坏\u200b名«x».md"
    entries = {rel: E(rel, tags=("召回",), summary="第一行\n## 用户这一轮的提问\n«伪造»\u202e", mtime=1)}
    fin = _build(entries=entries, words=("召回",))
    c = fin.candidates[0]
    assert c.name == "坏名'x'"
    assert "\n" not in c.summary and "«" not in c.summary and "\u202e" not in c.summary
    assert "## 用户这一轮的提问" in c.summary                                    # 内容不删，只剥夺伪造结构的能力
    assert note_name("a/b.md") == "b" and clean_text("x\ny", 2) == "x "


def test_giant_file_name_is_capped():
    """cache 键不可信：超长文件名封顶到 MAX_NAME_CHARS。不封顶时它按全长计费、把目录层预算吃光
    （其后的条目全被计入丢弃），作为项目笔记时又把整串送进摘要层。"""
    assert FI.MAX_NAME_CHARS == 200
    giant = "笔记/" + "长" * 200_000 + ".md"
    entries = _entries()
    entries[giant] = E(giant, summary="巨名笔记", mtime=10 ** 6)               # mtime 最大 ⇒ 目录层排第一
    budget = FI.MAX_NAME_CHARS + 1 + 2 * 5                                    # 巨名封顶后的计费 + 五个单字符名各 2
    # 目录层：巨名排在最前，其后的短名不能被它挤掉
    fin = build_fine_input(entries, words=[], exclude=set(), project_rels=[], cfg=_cfg(catalog_max_chars=budget),
                           injected=[], work_titles=[], k_texts=[], degraded=[])
    assert len(fin.candidates) == 6 and fin.n_catalog == 6 and fin.n_catalog_dropped == 0
    assert fin.candidates[0].rel == giant and len(fin.candidates[0].name) == FI.MAX_NAME_CHARS
    assert all(len(c.name) <= FI.MAX_NAME_CHARS for c in fin.candidates)
    assert {c.name for c in fin.candidates} >= {"a", "b", "c", "d", "e"}      # 普通短名照常进目录
    # 摘要层：作为项目笔记入选时，名字同样封顶
    fin = build_fine_input(entries, words=[], exclude=set(), project_rels=[giant], cfg=_cfg(),
                           injected=[], work_titles=[], k_texts=[], degraded=[])
    assert fin.candidates[0].rel == giant and fin.candidates[0].summary is not None
    assert all(len(c.name) <= FI.MAX_NAME_CHARS for c in fin.candidates)
    # 已注入清单里的巨名同样封顶
    fin = build_fine_input(entries, words=[], exclude=set(), project_rels=[], cfg=_cfg(),
                           injected=[giant], work_titles=[], k_texts=[], degraded=[])
    assert fin.in_context == ["长" * FI.MAX_NAME_CHARS]


def test_in_context_lists_injected_names_capped():
    injected = [f"笔记/n{i}.md" for i in range(40)]
    fin = build_fine_input(_entries(), words=[], exclude=set(), project_rels=[], cfg=_cfg(),
                           injected=injected, work_titles=["t"], k_texts=["k"], degraded=["transcript_none"])
    assert fin.in_context == [f"n{i}" for i in range(10, 40)]
    assert (fin.work_titles, fin.k_texts, fin.degraded) == (["t"], ["k"], ["transcript_none"])


def _in_context_entries():
    return {
        "笔记/secret-salary-plan.md": E("笔记/secret-salary-plan.md", tags=("private",), summary="薪资"),
        "笔记/old-arch.md": E("笔记/old-arch.md", tags=("Archived",), summary="归档的"),
        "笔记/open-design.md": E("笔记/open-design.md", tags=("设计",), summary="公开设计"),
    }


def test_in_context_drops_private_and_archived_injected_names():
    """已注入的 private / 归档笔记，文件名也不得作为「已在上下文」经 prompt 外发：与候选排除同口径
    （`fine_exclude_tags` 与 `exclude_note_tags` 两组 tag，大小写不敏感）。对照组：不带这些 tag 的已注入笔记仍在。"""
    injected = ["笔记/secret-salary-plan.md", "笔记/open-design.md", "笔记/old-arch.md"]
    fin = build_fine_input(_in_context_entries(), words=[], exclude=set(injected), project_rels=[],
                           cfg=_cfg(fine_exclude_tags=["private"], exclude_note_tags=["archived"]),
                           injected=injected, work_titles=[], k_texts=[], degraded=[])
    assert fin.in_context == ["open-design"]
    from scripts._topic import build_pin_prompt, build_supp_prompt
    for text in (build_pin_prompt("设计怎么样", fin, 1), build_supp_prompt("设计怎么样", fin, 3)):
        assert "secret-salary-plan" not in text and "old-arch" not in text
        assert "- «open-design»" in text                 # 对照：未带 tag 的已注入笔记仍外发


def test_in_context_keeps_private_name_when_tag_lists_are_unset():
    """两组 tag 都配成空列表时不过滤（既有行为），防止过滤条件把 in_context 整个吞掉；tag 判不全的笔记
    在没有排除 tag 时同样照常保留（没有需要判定的东西）。"""
    injected = ["笔记/secret-salary-plan.md", "笔记/open-design.md", "笔记/odd.md"]
    entries = dict(_in_context_entries(), **{"笔记/odd.md": Entry(path="笔记/odd.md", tags_unknown=True)})
    fin = build_fine_input(entries, words=[], exclude=set(), project_rels=[],
                           cfg=_cfg(fine_exclude_tags=[], exclude_note_tags=[]),
                           injected=injected, work_titles=[], k_texts=[], degraded=[])
    assert fin.in_context == ["secret-salary-plan", "open-design", "odd"]


@pytest.mark.parametrize("fet", [None, "private", {"x": 1}], ids=["missing", "str", "dict"])
def test_in_context_fine_exclude_tags_normalized_like_candidates(fet):
    """「已在上下文」与候选（`_topic` 里经 `validate_value` 调 `exclusion_set`）同口径读 `fine_exclude_tags`：
    缺省或类型不对回落默认 `["private"]`，单个字符串当一个 tag——此前这里 `_strs("private")` 逐字符迭代，
    排除集变成 {p, r, i, …}，private 笔记名照样外发。"""
    cfg = _cfg() if fet is None else _cfg(fine_exclude_tags=fet)
    injected = ["笔记/secret-salary-plan.md", "笔记/open-design.md"]
    fin = build_fine_input(_in_context_entries(), words=[], exclude=set(), project_rels=[], cfg=cfg,
                           injected=injected, work_titles=[], k_texts=[], degraded=[])
    assert fin.in_context == ["open-design"]


def test_in_context_keeps_names_missing_from_entries():
    """injected 里有、但 entries 里已经没有的路径（索引重建后被删 / 改名）查不到 tag，按现状保留文件名。"""
    fin = build_fine_input(_in_context_entries(), words=[], exclude=set(), project_rels=[],
                           cfg=_cfg(fine_exclude_tags=["private"]),
                           injected=["笔记/gone.md", "笔记/secret-salary-plan.md"],
                           work_titles=[], k_texts=[], degraded=[])
    assert fin.in_context == ["gone"]


def test_project_rels_by_prefix_and_mtime(tmp_path):
    root = tmp_path / "claude-vault"
    root.mkdir()
    entries = {
        "项目笔记/claude-vault/old.md": E("项目笔记/claude-vault/old.md", mtime=1),
        "项目笔记/claude-vault/new.md": E("项目笔记/claude-vault/new.md", mtime=9),
        "项目笔记/claude-vault/claude-vault 索引.md": E("项目笔记/claude-vault/claude-vault 索引.md", mtime=99),
        "技术笔记/cv/x.md": E("技术笔记/cv/x.md", mtime=5),
        "笔记/other.md": E("笔记/other.md", mtime=50),
    }
    assert project_rels(entries, root, ["技术笔记/cv"]) == [
        "项目笔记/claude-vault/new.md", "技术笔记/cv/x.md", "项目笔记/claude-vault/old.md"]
