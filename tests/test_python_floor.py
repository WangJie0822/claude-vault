"""守卫：本仓库的 .py 不得用到高于最低支持版本（Python 3.9，见 README「跨平台」）的写法。

论域：受跟踪的 `.py`，加上未跟踪、但没被 .gitignore 排除的 `.py`（开发中的新文件）。
只靠标准库 `ast` / `tokenize`，查下列几类，均属 best-effort：

1. `ast.parse(feature_version=FLOOR)`：语法层面的新特性（如 3.10 的 `match`）。官方文档说明
   该参数不保证覆盖每一项语法；实测 3.14 下它放过了下面 4、5 两类，所以单独检查。
2. 没有 `from __future__ import annotations` 的文件里，定义时求值的标注（签名、类体、模块级，
   以及函数体内嵌套的 def / class）不得用 `X | Y`；函数体内的局部变量标注不求值，不算违规。
3. 标注之外的 `X | Y`，只要一侧是 `None` 或内置类型（如 `Opt = int | None`、
   `isinstance(x, int | str)`），有没有 future import 都在运行期求值。
4. 3.10 / 3.11 的放宽：不加括号的 `:=` 用作下标或集合元素；下标里的 `*`；`*args: *Ts`。
5. 3.12 起放宽的 f-string 写法（PEP 701）：表达式里复用外层引号、含反斜杠或 `#`、单引号
   f-string 内换行。依赖 3.12+ 的 tokenizer；更低版本的解释器原生就拒绝这些写法，落到第 1 类。

不查：运行期 API（如 3.10 的 `zip(strict=)`）；自定义类之间的 `A | B`（与按位或无法区分）。
偏保守、会多报的写法：`if TYPE_CHECKING:` 或版本判断分支里的标注、`Literal[1 | 2]` 这类
标注里的整数按位或。遇到时改成字符串标注或挪出分支，不要放宽守卫。

由来：`scan_sessions.py` 签名里的 `X | Y` 自 1.1.0 起让它在 3.9 上无法导入，到 2.0.0 的
full-review 才发现（P4A-E2 / P4A-L3；定位：
`git log -S"str | None = None" -- skills/summarize-session/scripts/scan_sessions.py`）。
此前代码里只有一条注释提到过解释器版本，列举了 3.9 与 3.8，并不是声明的下限。
"""
from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FLOOR = (3, 9)
_TYPE_NAMES = frozenset({"bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset",
                         "int", "list", "object", "set", "str", "tuple", "type"})
_COMPOUND = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)

SYNTAX = "语法高于"
ANNOTATION = "定义时求值的标注"
RUNTIME_UNION = "标注之外的 X | Y"
WALRUS = "不加括号的 :="
STAR = "用了 *"
FSTRING = "f-string"


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _signature_annotations(node: ast.AST):
    a = node.args
    for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
        if arg is not None and arg.annotation is not None:
            yield arg.annotation
    if node.returns is not None:
        yield node.returns


def _evaluated_annotations(body: list[ast.stmt], in_function: bool = False):
    """定义时会被求值的标注：签名、类体与模块级 AnnAssign。

    函数体内的局部变量标注不求值，跳过；但函数体内嵌套的 def / class 在外层函数
    每次执行到时定义，它们的签名与类体标注照样求值，要继续检查。"""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _signature_annotations(node)
            yield from _evaluated_annotations(node.body, in_function=True)
        elif isinstance(node, ast.ClassDef):
            yield from _evaluated_annotations(node.body, in_function=False)
        elif isinstance(node, ast.AnnAssign):
            if not in_function:
                yield node.annotation
        elif isinstance(node, _COMPOUND):
            for field in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(node, field, []):
                    yield from _evaluated_annotations(
                        child.body if isinstance(child, ast.ExceptHandler) else [child],
                        in_function)


def _is_union(n: ast.AST) -> bool:
    return isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)


def _typeish(e: ast.AST) -> bool:
    if isinstance(e, ast.Constant) and e.value is None:
        return True
    if isinstance(e, ast.Subscript):
        e = e.value
    return isinstance(e, ast.Name) and e.id in _TYPE_NAMES


def _parenthesized(node: ast.AST, lines: list[str]) -> bool:
    """节点前面紧挨着的非空白字符是不是 `(`（ast 的位置不含括号；col_offset 按 UTF-8 字节计）。"""
    row, prefix = node.lineno - 1, lines[node.lineno - 1].encode("utf-8")[:node.col_offset]
    text = prefix.decode("utf-8", "replace").rstrip()
    while not text and row > 0:
        row -= 1
        text = lines[row].rstrip()
    return text.endswith("(")


def _ast_problems(tree: ast.Module, lines: list[str]) -> list[tuple[int, str]]:
    out = []
    if not _has_future_annotations(tree):
        out += [(ann.lineno, f"{ANNOTATION}用了 X | Y（3.10 起），文件缺 future annotations")
                for ann in _evaluated_annotations(tree.body)
                if any(_is_union(n) for n in ast.walk(ann))]
    in_annotation: set[int] = set()
    for n in ast.walk(tree):
        anns = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns = list(_signature_annotations(n))
            if n.args.vararg is not None and isinstance(n.args.vararg.annotation, ast.Starred):
                out.append((n.lineno, f"*args 的标注{STAR}（3.11 起）"))
        elif isinstance(n, ast.AnnAssign):
            anns = [n.annotation]
        for ann in anns:
            in_annotation.update(id(s) for s in ast.walk(ann))
    for n in ast.walk(tree):
        if _is_union(n) and id(n) not in in_annotation and (_typeish(n.left) or _typeish(n.right)):
            out.append((n.lineno, f"{RUNTIME_UNION}在运行期求值（3.10 起）"))
        walrus_slots = []
        if isinstance(n, ast.Subscript):
            if any(isinstance(s, ast.Starred) for s in ast.walk(n.slice)):
                out.append((n.lineno, f"下标里{STAR}（3.11 起）"))
            walrus_slots = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
        elif isinstance(n, ast.Set):
            walrus_slots = n.elts
        elif isinstance(n, ast.SetComp):
            walrus_slots = [n.elt]
        for s in walrus_slots:
            if isinstance(s, ast.NamedExpr) and not _parenthesized(s, lines):
                out.append((s.lineno, f"{WALRUS}用作下标或集合元素（3.10 起）"))
    return out


def _quote(tok: str) -> str:
    body = tok.lstrip("rRbBuUfF")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def _fstring_problems(src: str) -> list[tuple[int, str]]:
    if not hasattr(tokenize, "FSTRING_START"):
        return []
    out, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_END:
            stack.pop()
            continue
        if stack and tok.type in (tokenize.STRING, tokenize.FSTRING_START):
            q = _quote(tok.string)
            if any(q == enc or (len(enc) == 1 and q[0] == enc) for enc in stack):
                out.append((tok.start[0], f"{FSTRING} 表达式里复用了外层引号（3.12 起）"))
            if tok.type == tokenize.STRING and ("\\" in tok.string or "#" in tok.string):
                out.append((tok.start[0], f"{FSTRING} 表达式里有反斜杠或 #（3.12 起）"))
        elif stack and tok.type == tokenize.COMMENT:
            out.append((tok.start[0], f"{FSTRING} 表达式里有注释（3.12 起）"))
        elif stack and tok.type in (tokenize.NL, tokenize.NEWLINE) and len(stack[-1]) == 1:
            out.append((tok.start[0], f"单引号 {FSTRING} 内换行（3.12 起）"))
        if tok.type == tokenize.FSTRING_START:
            stack.append(_quote(tok.string))
    return out


def floor_problems(src: str, name: str = "<src>") -> list[str]:
    try:
        tree = ast.parse(src, feature_version=FLOOR)
    except SyntaxError as e:
        return [f"{name}:{e.lineno}: {SYNTAX} {FLOOR[0]}.{FLOOR[1]}：{e.msg}"]
    found = sorted(set(_ast_problems(tree, src.splitlines()) + _fstring_problems(src)))
    return [f"{name}:{line}: {msg}" for line, msg in found]


def _python_files() -> list[str]:
    """沿用 test_claude_md_layout 的策略：非本仓库 checkout 跳过，git 坏了报红。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, env=env,
                              stdin=subprocess.DEVNULL)

    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        if (ROOT / ".git").exists():
            pytest.fail(f"这是 git 检出但 rev-parse 失败：{top.stderr.decode('utf-8', 'replace')}")
        pytest.skip("非 git 仓库（例如插件缓存里的副本）")
    if Path(top.stdout.decode("utf-8", "replace").strip()).resolve() != ROOT.resolve():
        pytest.skip("不是本仓库自己的 checkout（嵌在别的 git 仓库里）")
    proc = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py")
    if proc.returncode != 0:
        pytest.fail(f"git ls-files 失败：{proc.stderr.decode('utf-8', 'replace')}")
    names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({p for p in names if p and (ROOT / p).is_file()})


def read_source(path: Path) -> str:
    """按 BOM 与编码声明解码，与解释器读源文件的方式一致。"""
    data = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


# 名称 -> (源码, 可接受的问题类别；空元组表示必须零命中)。低于 3.12 的解释器原生拒绝
# 4、5 两类写法，报成「语法」，所以那几条也接受 SYNTAX。
_NEW_SYNTAX = (SYNTAX,)
CASES = {
    "match": ("match x:\n    case 1:\n        pass\n", _NEW_SYNTAX),
    "arg": ("def f(x: int | None) -> None: ...\n", (ANNOTATION,)),
    "return": ("def f(x) -> str | None: ...\n", (ANNOTATION,)),
    "vararg_kwarg": ("def f(*a: int | None, **k: str | None): ...\n", (ANNOTATION,)),
    "posonly": ("def f(a: int | None, /): ...\n", (ANNOTATION,)),
    "kwonly": ("def f(*, a: int | None): ...\n", (ANNOTATION,)),
    "nested_generic": ("def f(x: Optional[int | None]): ...\n", (ANNOTATION,)),
    "class": ("class C:\n    a: int | None = None\n", (ANNOTATION,)),
    "module": ("x: int | str = 1\n", (ANNOTATION,)),
    "module_for": ("for i in r:\n    y: int | None = 1\n", (ANNOTATION,)),
    "try_body": ("try:\n    def f(x: int | None): ...\nexcept ImportError:\n    pass\n",
                 (ANNOTATION,)),
    "try_handler": ("try:\n    pass\nexcept E:\n    def i(x: int | None): ...\n", (ANNOTATION,)),
    "try_finally": ("try:\n    pass\nfinally:\n    def i(x: int | None): ...\n", (ANNOTATION,)),
    "while_else": ("while x:\n    pass\nelse:\n    def i(x: int | None): ...\n", (ANNOTATION,)),
    "with_in_func": ("def o():\n    with c:\n        def i(x: int | None): ...\n", (ANNOTATION,)),
    "nested_def": ("def outer():\n    def inner(x: int | None): ...\n", (ANNOTATION,)),
    "async_nested": ("async def o():\n    async def i(x: int | None): ...\n", (ANNOTATION,)),
    "class_in_func": ("def outer():\n    class C:\n        a: int | None = None\n",
                      (ANNOTATION,)),
    "other_future": ("from __future__ import division\ndef f(x: int | None): ...\n",
                     (ANNOTATION,)),
    "other_from_import": ("from typing import Optional\ndef f(x: int | None): ...\n",
                          (ANNOTATION,)),
    "annotations_not_from_future": ("from mod import annotations\ndef f(x: int | None): ...\n",
                                    (ANNOTATION,)),
    "alias": ("Opt = int | None\n", (RUNTIME_UNION,)),
    "alias_custom_none": ("Opt = Foo | None\n", (RUNTIME_UNION,)),
    "alias_subscript": ("Opt = list[int] | Foo\n", (RUNTIME_UNION,)),
    "alias_with_future": ("from __future__ import annotations\nOpt = list[int] | None\n",
                          (RUNTIME_UNION,)),
    "isinstance": ("isinstance(x, int | str)\n", (RUNTIME_UNION,)),
    "default_value": ("def f(x=int | None): ...\n", (RUNTIME_UNION,)),
    "star_index": ("a[*b]\n", (STAR, SYNTAX)),
    "star_vararg": ("def f(*a: *Ts): ...\n", (STAR, SYNTAX)),
    "walrus_index": ("a[x := 1]\n", (WALRUS, SYNTAX)),
    "walrus_set": ("s = {y := 1, 2}\n", (WALRUS, SYNTAX)),
    "walrus_setcomp": ("s = {y := v for v in r}\n", (WALRUS, SYNTAX)),
    "fs_same_quote": ('f"{x["a"]}"\n', (FSTRING, SYNTAX)),
    "fs_nested_same": ('f"{f"{x}"}"\n', (FSTRING, SYNTAX)),
    "fs_backslash": ("f\"{'\\n'.join(x)}\"\n", (FSTRING, SYNTAX)),
    "fs_hash": ("f\"{'#'}\"\n", (FSTRING, SYNTAX)),
    "fs_comment": ('f"{x # c\n}"\n', (FSTRING, SYNTAX)),
    "fs_comment_triple": ('f"""{x # c\n}"""\n', (FSTRING, SYNTAX)),
    "fs_newline": ('f"{x +\ny}"\n', (FSTRING, SYNTAX)),
    "future": ("from __future__ import annotations\ndef f(x: int | None): ...\n", ()),
    "local": ("def f():\n    y: int | None = None\n", ()),
    "local_in_if": ("def f():\n    if x:\n        y: int | None = None\n", ()),
    "generics": ("def f(x: list[int]) -> dict[str, int]: ...\n", ()),
    "flags": ("import os\nm = os.O_CREAT | os.O_EXCL\nn = a | b\n", ()),
    "string_annotation": ('def f(x: "int | None"): ...\n', ()),
    "walrus_parenthesized": ("a[(x := 1)]\ns = {(y := 1), 2}\nif (n := 1):\n    pass\n", ()),
    "star_call": ("f(*b)\nc = [*b]\n", ()),
    "fs_other_quote": ("f\"{x['a']}\"\n", ()),
    "fs_triple_outer": ('f"""{x["a"]}"""\n', ()),
    "fs_escape_in_literal": ('f"a\\nb{x!r:>{w}}"\n', ()),
}


@pytest.mark.parametrize("name", list(CASES))
def test_checker_discriminates(name):
    src, accepted = CASES[name]
    problems = floor_problems(src)
    if not accepted:
        assert problems == []
    else:
        assert problems and all(any(c in p for c in accepted) for p in problems), problems


@pytest.mark.parametrize("encoding, prefix", [
    ("utf-8-sig", ""),
    ("gbk", "# -*- coding: gbk -*-\n"),
], ids=["bom", "gbk"])
def test_read_source_follows_bom_and_coding_cookie(tmp_path, encoding, prefix):
    p = tmp_path / "m.py"
    p.write_bytes((prefix + 'MSG = "中文"\n').encode(encoding))
    assert floor_problems(read_source(p)) == []


def test_repo_python_respects_floor():
    files = _python_files()
    assert "skills/summarize-session/scripts/scan_sessions.py" in files
    problems = []
    for rel in files:
        problems += floor_problems(read_source(ROOT / rel), rel)
    assert not problems, "\n".join(problems)


def test_readme_states_the_same_floor():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "\n## 跨平台\n" in readme
    assert f"Python **{FLOOR[0]}.{FLOOR[1]}**" in readme
