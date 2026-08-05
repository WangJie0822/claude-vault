"""frontmatter-cache `_version` 双端契约守卫（OBS-5）。

`<vault>/.meta/frontmatter-cache.json` 是写端（summarize-session 的 `rebuild_index.py`）
与读端（vault-loader 的 `_frontmatter_reader.py`）之间的**唯一**接口。读端 `load_cache`
校验 `_version`，不匹配就静默返回空索引——即召回全灭，且不报错、不告警。

**为什么必须由本文件守**：
  - 两端不共享常量。读端是 `CACHE_VERSION = 1`，写端是 `rebuild_index.load_cache` 里
    **三处硬编码字面量 `1`**。任一端 bump 而另一端漏改，全体用户的召回同时静默归零。
  - 读端已有的 `skills/vault-loader/tests/test_frontmatter_reader.py` 守不住这件事：
    它用**读端自己的常量**插值构造 fixture（`'{"_version": %d}' % CACHE_VERSION`），
    按构造永远自洽，结构上不可能捕获两端分叉。
  - CLAUDE.md 把「改一端的 cache schema 必须同步另一端」写成了核心数据契约，
    但此前只是一句话、无任何自动化守卫。

**为什么用文本解析而非 import**：本仓库有三个 pytest 根，导入约定各不相同
（见 CLAUDE.md「开发与测试」节）——从仓库根跑时 `from scripts._frontmatter_reader import ...`
与 `from _x import ...` 都不成立。直接读源码文本是唯一能同时看到两端的方式，
且顺带避免「import 到某一端后用它去构造另一端期望值」这类自洽陷阱。
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "skills" / "vault-loader" / "scripts" / "_frontmatter_reader.py"
WRITER = ROOT / "skills" / "summarize-session" / "scripts" / "rebuild_index.py"

# 模块级 `NAME = <整数>` 常量（允许行尾注释）
_INT_CONST = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)\s*(?:#.*)?$", re.M)
# 裸整数字面量（排除标识符内部与属性/小数点相邻的数字）
_BARE_INT = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def _int_consts(src: str) -> dict[str, int]:
    return {name: int(val) for name, val in _INT_CONST.findall(src)}


def _strip_prose(src: str) -> str:
    """把注释与三引号 docstring 抹成等长空白（保持行号不变）。

    两端源码都在注释/docstring 里解释 `_version` 语义（如读端 `_frontmatter_reader.py`
    的「旧 cache 无 _version 字段或版本不符 → 丢弃」），这些行不含版本号，
    不抹掉会被判成「无法解析」而误红。只抹三引号字符串——`{"_version": 1}` 这类
    单引号 dict key 必须保留，它正是要扫的目标。
    """
    lines = src.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            is_comment = tok.type == tokenize.COMMENT
            is_docstring = tok.type == tokenize.STRING and tok.string[:3] in ('"""', "'''")
            if not (is_comment or is_docstring):
                continue
            (r0, c0), (r1, c1) = tok.start, tok.end
            for r in range(r0, r1 + 1):
                line = lines[r - 1]
                lo = c0 if r == r0 else 0
                hi = c1 if r == r1 else len(line)
                lines[r - 1] = line[:lo] + " " * (hi - lo) + line[hi:]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 源码暂时不可 tokenize 时不阻断——后续断言仍会基于原文本给出判定
        return src
    return "\n".join(lines)


def _versions_declared(src: str) -> tuple[list[int], list[str]]:
    """扫描所有提到 `_version` 的行，解析出其上的版本号。

    返回 (已解析版本号列表, 无法解析的行)。字面量与「模块内已知整数常量名」都认，
    后者覆盖将来某一端改成引用常量的写法。**无法解析的行必须为空**——否则本守卫
    就在无声地漏掉那一行，退化成空网。
    """
    consts = _int_consts(src)
    found: list[int] = []
    unresolved: list[str] = []
    for lineno, line in enumerate(_strip_prose(src).splitlines(), 1):
        if "_version" not in line:
            continue
        vals = [int(d) for d in _BARE_INT.findall(line)]
        vals += [consts[n] for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line)
                 if n in consts]
        if vals:
            found.extend(vals)
        else:
            unresolved.append(f"{lineno}: {line.strip()}")
    return found, unresolved


def _reader_cache_version(src: str) -> int:
    m = re.search(r"^CACHE_VERSION\s*=\s*(\d+)", src, re.M)
    assert m is not None, (
        f"未能在读端 {READER.name} 中定位 `CACHE_VERSION = <int>`——"
        f"若该常量被改名或改成非字面量，请同步更新本守卫，不要让它静默失效。")
    return int(m.group(1))


def test_cache_version_matches_across_read_and_write_ends():
    """读端 `CACHE_VERSION` 必须等于写端所有 `_version` 字面量。"""
    reader_src = READER.read_text(encoding="utf-8")
    writer_src = WRITER.read_text(encoding="utf-8")

    reader_version = _reader_cache_version(reader_src)
    writer_versions, unresolved = _versions_declared(writer_src)

    assert not unresolved, (
        f"写端 {WRITER.name} 中这些 `_version` 行无法解析出版本号，本守卫会漏掉它们："
        f"{unresolved}")
    assert writer_versions, (
        f"写端 {WRITER.name} 未找到任何 `_version` 声明——守卫已成空网，"
        f"请确认 cache 写入逻辑是否搬走，并同步更新本守卫。")
    assert set(writer_versions) == {reader_version}, (
        f"cache `_version` 双端分叉：读端 {READER.name} 声明 {reader_version}，"
        f"写端 {WRITER.name} 声明 {sorted(set(writer_versions))}。\n"
        f"读端 load_cache 版本不符时静默返回空索引 → 全体用户召回归零且无任何报错。"
        f"改一端必须同步另一端（见 CLAUDE.md「核心数据契约」）。")


def test_reader_version_checks_are_consistent_with_its_constant():
    """读端自身的 `_version` 比较必须一律走 `CACHE_VERSION`，不得再散落字面量。"""
    reader_src = READER.read_text(encoding="utf-8")
    reader_version = _reader_cache_version(reader_src)
    versions, unresolved = _versions_declared(reader_src)
    assert not unresolved, f"读端存在无法解析的 `_version` 行：{unresolved}"
    assert versions, "读端未找到任何 `_version` 使用点——守卫已成空网"
    assert set(versions) == {reader_version}, (
        f"读端内部 `_version` 取值不一致：{sorted(set(versions))} vs "
        f"CACHE_VERSION={reader_version}")


def test_guard_is_not_self_consistent_by_construction():
    """自证区分力：两端版本号取自**各自源码文本**，不共享来源。

    人为把写端文本的版本号改成 2，本守卫必须红——否则它就和
    `test_frontmatter_reader.py` 那条一样是按构造永远自洽的空网断言。
    """
    reader_src = READER.read_text(encoding="utf-8")
    writer_src = WRITER.read_text(encoding="utf-8")
    reader_version = _reader_cache_version(reader_src)

    bumped = re.sub(r'(["\']_version["\']\s*:\s*)\d+', r"\g<1>99", writer_src)
    bumped = re.sub(r"(_version['\"]\s*\)\s*!=\s*)\d+", r"\g<1>99", bumped)
    assert bumped != writer_src, "变异未生效，说明版本号写法已变，请更新本守卫"

    mutant_versions, _ = _versions_declared(bumped)
    assert set(mutant_versions) != {reader_version}, (
        "写端版本号被改成 99 后判据仍与读端相等——守卫无区分力，需重新设计。")
