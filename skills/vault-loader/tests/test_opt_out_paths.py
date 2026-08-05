"""`_is_opt_out_path` 的拼写变体与边界锚定守卫（BP-1 / C-2）。

`opt_out_paths` 是用户**唯一**的「这个目录别注入」开关。修复前它用裸
`str().startswith()`，于是同一个目录的多种合理写法只有一部分生效，而且会连兄弟目录
一起拦掉——两类错都静默：漏判时用户以为关掉了、Vault 内容照旧进模型上下文；
误判时用户不知道为什么某个项目没有注入。

本文件的用例全部**双向**：既验证「该拦的拦住」，也验证「不该拦的别拦」。
只验前者的话，一个 `return True` 的实现也能全绿——这正是原有集成用例的问题
（它只断言 stdout 为空，而 fixture 的 Vault 本来就没内容）。

两个 hook 入口各有一份实现，本文件对**两份都跑同一套用例**，另有一条文本比对
钉住二者逐字一致。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from scripts.prompt_submit_load import _is_opt_out_path as ups_impl
from scripts.session_start_load import _is_opt_out_path as ss_impl

# 两个入口的实现都要过同一套用例
IMPLS = [pytest.param(ups_impl, id="prompt_submit"), pytest.param(ss_impl, id="session_start")]
pytestmark = pytest.mark.parametrize("is_opt_out", IMPLS)


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """一个真实存在的目录——`resolve()` 对存在与否行为不同，用真目录更接近生产。"""
    d = tmp_path / "Work" / "SecretProject"
    d.mkdir(parents=True)
    return d


# ── 该拦住的：同一目录的各种合理写法 ────────────────────────────────


def test_exact_path(is_opt_out, secret: Path) -> None:
    assert is_opt_out(secret, [str(secret)])


def test_subdirectory(is_opt_out, secret: Path) -> None:
    deep = secret / "sub" / "deeper"
    deep.mkdir(parents=True)
    assert is_opt_out(deep, [str(secret)])


def test_forward_slash_spelling(is_opt_out, secret: Path) -> None:
    """JSON 里写 Windows 路径必须双写反斜杠，用户自然改用正斜杠——必须等价。

    这是修复前最容易踩、也最危险的一种：配置看起来完全正常，开关却没生效。
    """
    assert is_opt_out(secret, [str(secret).replace("\\", "/")])


def test_trailing_separator(is_opt_out, secret: Path) -> None:
    """用户从资源管理器/终端复制路径常带末尾分隔符。"""
    assert is_opt_out(secret, [str(secret) + os.sep])


@pytest.mark.skipif(os.name != "nt", reason="路径大小写不敏感是 Windows 语义")
def test_case_insensitive_on_windows(is_opt_out, secret: Path) -> None:
    assert is_opt_out(secret, [str(secret).lower()])
    assert is_opt_out(secret, [str(secret).upper()])


def test_tilde_expansion(is_opt_out, tmp_path: Path, monkeypatch) -> None:
    """`~/private` 这种写法必须展开。Windows 上 Path.home() 读 USERPROFILE。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    private = tmp_path / "private"
    private.mkdir()
    assert is_opt_out(private, ["~/private"])


def test_matches_when_any_entry_hits(is_opt_out, secret: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert is_opt_out(secret, [str(other), str(secret)])


# ── 不该拦的：边界锚定与过度匹配 ────────────────────────────────────


def test_sibling_with_shared_prefix_not_matched(is_opt_out, tmp_path: Path) -> None:
    """`.../secret` 不得连 `.../secret-public` 一起拦掉——裸前缀比对的典型误伤。"""
    secret = tmp_path / "secret"
    secret.mkdir()
    public = tmp_path / "secret-public"
    public.mkdir()

    assert is_opt_out(secret, [str(secret)]), "目标目录本身必须拦住"
    assert not is_opt_out(public, [str(secret)]), "兄弟目录被误伤"


def test_unrelated_path_not_matched(is_opt_out, tmp_path: Path, secret: Path) -> None:
    other = tmp_path / "public"
    other.mkdir()
    assert not is_opt_out(other, [str(secret)])


def test_parent_not_matched(is_opt_out, secret: Path) -> None:
    """配置的是子目录时，父目录不该被拦（方向不能反）。"""
    assert not is_opt_out(secret.parent, [str(secret)])


def test_empty_list_never_matches(is_opt_out, secret: Path) -> None:
    assert not is_opt_out(secret, [])


# ── 畸形配置项：逐项跳过，不牵连其他项、不抛异常 ──────────────────


@pytest.mark.parametrize(
    "junk", [None, 123, [], {}, "", "   "], ids=["none", "int", "list", "dict", "empty", "blank"]
)
def test_malformed_entry_is_skipped(is_opt_out, secret: Path, junk) -> None:
    assert not is_opt_out(secret, [junk])
    # 关键：畸形项不能让同一列表里的合法项失效
    assert is_opt_out(secret, [junk, str(secret)])


def test_never_raises_on_hostile_input(is_opt_out, secret: Path) -> None:
    """闸门自身不得抛异常——它在 hook 最前端，抛了就等于整条链失效。"""
    hostile = ["\x00bad", "?" * 300, "con", "//?/UNC/nonexistent", str(secret)]
    assert is_opt_out(secret, hostile) is True


# ── 两份实现必须逐字一致 ────────────────────────────────────────────

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_FUNC_RE = re.compile(
    r"^def _is_opt_out_path\(.*?(?=^\S)", re.MULTILINE | re.DOTALL
)


def _extract(name: str) -> str:
    src = (SCRIPTS / name).read_text(encoding="utf-8")
    m = _FUNC_RE.search(src)
    assert m, f"{name} 里找不到 _is_opt_out_path 定义"
    return m.group(0).strip()


def test_both_hooks_share_identical_source(is_opt_out) -> None:
    """两个入口的实现逐字相同——改一处漏另一处，一半的会话就失去这道闸门。

    刻意用文本比对而非 `inspect.getsource` 相等：后者在两个模块各自 import 成功时
    才可用，而这里要守的正是「有人只改了一个文件」。
    """
    ups = _extract("prompt_submit_load.py")
    ss = _extract("session_start_load.py")
    assert ups == ss, (
        "两个 hook 入口的 _is_opt_out_path 已不一致——修改必须同步到两处。\n"
        f"prompt_submit_load.py 长度 {len(ups)}，session_start_load.py 长度 {len(ss)}"
    )
