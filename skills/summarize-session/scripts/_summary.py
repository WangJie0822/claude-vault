"""摘要（frontmatter `summary`）的判据与写期口径——唯一权威实现。

三条写入路径共用：`summary_gap.py`（会话收尾补缺）、`enrich_summary.py`（付费补存量 / 优化）、
`archive_doc.py`（归集透传，只用 `sanitize_summary`）。判据被 `rebuild_index._health_check`
复用，保证「报告说缺几篇」与「补缺列出几篇」同源。设计见 spec §3（2.3.0）。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from _sensitive_patterns import is_sensitive_content
from _frontmatter_field import get_field_lines

MIN_LEN = 20
MAX_LEN = 240
WORKLOG_CATEGORY = "工作日志"
GAP_KINDS = ("missing", "filename", "short")
OPTIMIZE_KINDS = ("long",)
BOILERPLATE_MIN_RESIDUAL = 10
_FILENAME_FALLBACK_SUFFIX = "（仅标题，summary 缺失）"

_STRUCT_TOKENS = ("[[", "]]", "](", "![", "**", "%%", "`", "<", ">")
_BACKSLASH = chr(92)
# NFKC 后会落进这些字符的才折叠（全角括号、引号等）；其余字符原样入库，不改写中文标点、² ①
_FOLD_TARGETS = frozenset('[]()!*%`<>"#-' + _BACKSLASH)
_SCHEME_RE = re.compile(r"\b(?:javascript|vbscript):|\bfile:/|\bdata:[a-z]+/", re.I)
_LEAD_RE = re.compile(r"^[#>\-\s]+")
_SENT_END = "。；！？.!?;"
_WS_RE = re.compile(r"\s+")

# 只收「明确的指令性短语」：泛词（assistant、instruction）会误杀名字里带 assistant 的项目名
_INSTRUCTION_RE = re.compile(
    r"忽略(以上|之前|上述|前面)|无视(以上|之前|上述|前面)|你(必须|应当|需要)|请(立即|务必)"
    r"|ignore (all|any|the|previous)|disregard"
    # 单独的 system prompt 是技术名词（作者 vault 现有正常摘要误拒 2 篇），只拦索取式说法
    r"|(reveal|print|repeat|show|输出|打印|泄露|复述) ?(your |the |你的 ?)?system prompt",
    re.I)
# `..` 只在路径形态（`../` 或 `/..`）下算：NFKC 会把省略号「…」展开成 `...`
_PATH_RE = re.compile(r"(^|[\s(（])(~/|[A-Za-z]:/|/[A-Za-z0-9_.\-]+/)|\.\./|/\.\.")
_LATIN_RE = re.compile(r"[A-Za-z]")
_GREEK_CYRILLIC_RE = re.compile(r"[\u0370-\u03FF\u0400-\u04FF]")
# 混写按连续字母分词判：按空白分词会把「~12μs/帧、…DMIPS」整段并成一个 token。
# μ 是微符号 µ 经 NFKC 的结果（计量单位），不算希腊字母
_LETTER_RUN_RE = re.compile("[A-Za-z" + _GREEK_CYRILLIC_RE.pattern[1:-1] + "]+")
_MICRO = chr(0x3BC)
_STOCK_PHRASES = ("本文", "本篇", "这篇笔记", "记录了", "介绍了", "总结了", "的过程", "相关内容")
_SEG_SPLIT_RE = re.compile(r"[\s_\-·、，,。.()（）\[\]]+")
_RESIDUAL_STRIP_RE = re.compile(r"[\W_]+")
_SECRET_VALUE = r"['\"]?(?=[A-Za-z0-9_\-./+=]*[0-9])(?=[A-Za-z0-9_\-./+=]*[A-Za-z])[A-Za-z0-9_\-./+=]{8,}"
_EXTRA_SENSITIVE_RE = re.compile(
    r"password\s*[:=]|Bearer\s+\S{20,}|\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|://[^/\s:@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    # 赋值形态：值须同时含字母与数字的 ASCII 凭据字符，免得「token: 用于鉴权的……」
    # 或 tokenizer=bert-base-chinese 这类说明被误判
    r"|(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|passwd|pwd)[A-Za-z0-9_]*\s*[:=]\s*" + _SECRET_VALUE
    + r"|(?:密码|口令|密钥|令牌)\s*[:：=]\s*" + _SECRET_VALUE,
    re.I)


def note_id(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def proposal_id(rel: str, old, new: str) -> str:
    raw = "\x00".join((rel, old or "", new))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def _as_list(v) -> list:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v:
        return [v]
    return []


def is_worklog(rel: str, entry: dict) -> bool:
    if rel.split("/", 1)[0] == WORKLOG_CATEGORY:
        return True
    return WORKLOG_CATEGORY in _as_list(entry.get("category"))


def is_collected_doc(entry: dict) -> bool:
    """`archive_doc` 归集进来的 spec/plan：tags 含 `archived`。摘要唯一来源是 pending-docs 的 context。"""
    return "archived" in _as_list(entry.get("tags"))


def applicable(rel: str, entry: dict, include_archived: bool = False) -> bool:
    if not entry.get("_has_frontmatter", True):
        return False
    if is_worklog(rel, entry) or is_collected_doc(entry):
        return False
    if entry.get("status") == "archived" and not include_archived:
        return False
    return True


def _strip_controls(t: str) -> str:
    out = []
    for c in t:
        cat = unicodedata.category(c)
        if cat in ("Zl", "Zp", "Cc"):
            out.append(" ")
        elif cat[0] == "C":
            continue
        else:
            out.append(c)
    return "".join(out)


def _fold_structural(t: str) -> str:
    out = []
    for c in t:
        n = unicodedata.normalize("NFKC", c)
        out.append(n if n != c and any(ch in _FOLD_TARGETS for ch in n) else c)
    return "".join(out)


def sanitize_summary(raw, *, min_len: int = MIN_LEN, hard_cut: bool = False):
    """清洗后返回不带引号的纯文本；不合规返回 None（拒写）。超长在句末截断，截不出合规长度也返回 None；
    `hard_cut=True` 时截不出句末就直接截到 MAX_LEN（归集用：归集文档不在任何补全通道里，拒写即永久缺失）。

    引号、反斜杠与结构 token 在同一个循环里删到不动点：先删 token 后删引号时，
    `[x]"(…)` 删掉引号会重新拼出链接（2.3.0 full-review P2-SM1）。
    """
    if not isinstance(raw, str):
        return None
    t = _strip_controls(_fold_structural(raw))
    prev = None
    while prev != t:
        prev = t
        for tok in _STRUCT_TOKENS:
            t = t.replace(tok, "")
        t = t.replace('"', "").replace(_BACKSLASH, "")
        t = _LEAD_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > MAX_LEN:
        cut = t[:MAX_LEN]
        idx = max(cut.rfind(ch) for ch in _SENT_END)
        if idx < 0 and not hard_cut:
            return None
        t = (cut[:idx + 1] if idx >= 0 else cut).strip()
    if len(t) < min_len:
        return None
    return t


def fm_has_summary(path: Path):
    """读文件头 4 KB：frontmatter 里有没有 `summary:` 键（边界判据同 _frontmatter_field）。

    读失败返回 None；无 frontmatter、4 KB 内未闭合返回 False。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return None
    lines = get_field_lines(head, "summary")
    return bool(lines) if lines is not None else False


def classify(rel: str, entry: dict, *, vault=None) -> str:
    raw = entry.get("summary")
    src = entry.get("summary_src")
    if src is None and vault is not None and isinstance(raw, str) and raw.strip():
        # 旧 cache 没有 summary_src：读文件头判断这个值是不是 H1 兜底
        if fm_has_summary(Path(vault) / rel) is False:
            src = "h1"
    if src == "h1" or not isinstance(raw, str) or not raw.strip():
        return "missing"
    s = _WS_RE.sub(" ", raw).strip()
    stem = _stem(rel)
    if s == stem or s == stem + _FILENAME_FALLBACK_SUFFIX:
        return "filename"
    if len(s) < MIN_LEN:
        return "short"
    if len(s) > MAX_LEN:
        return "long"
    return "ok"


def payload_sensitive(text: str) -> bool:
    return bool(is_sensitive_content(text or "") or _EXTRA_SENSITIVE_RE.search(text or ""))


def _suspicious(t: str) -> bool:
    if _INSTRUCTION_RE.search(t) or "://" in t or _PATH_RE.search(t) or _SCHEME_RE.search(t):
        return True
    for tok in _LETTER_RUN_RE.findall(t):
        if _LATIN_RE.search(tok) and _GREEK_CYRILLIC_RE.search(tok.replace(_MICRO, "")):
            return True
    return False


def _residual_len(t: str, rel: str) -> int:
    s = t
    for seg in _SEG_SPLIT_RE.split(_stem(rel)):
        if len(seg) >= 2:
            s = s.replace(seg, "")
    for w in _STOCK_PHRASES:
        s = s.replace(w, "")
    return len(_RESIDUAL_STRIP_RE.sub("", s))


def reject_reason(text, rel: str, entry: dict):
    """写前闸门：返回拒写原因，合格返回 None。顺序：invalid → sensitive → suspicious → boilerplate → not_ok。"""
    if not isinstance(text, str) or not text:
        return "invalid"
    if payload_sensitive(text):
        return "sensitive"
    norm = unicodedata.normalize("NFKC", text)
    if _suspicious(norm):
        return "suspicious"
    if _residual_len(norm, rel) < BOILERPLATE_MIN_RESIDUAL:
        return "boilerplate"
    probe = dict(entry)
    probe["summary"] = text
    probe["summary_src"] = "fm"
    if classify(rel, probe) != "ok":
        return "not_ok"
    return None
