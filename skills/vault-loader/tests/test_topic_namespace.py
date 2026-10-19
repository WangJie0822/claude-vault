# -*- coding: utf-8 -*-
"""提炼子进程的**写入落点**必须与 hook 父进程的**读取落点**是同一个 runtime 命名空间。

**为什么这些用例必须真起子进程**：`_state._RUNTIME` 是模块级全局。同进程内直接调
`run_extraction_child` 会继承父进程 `configure_context` 的结果，写读天然落在同一个
命名空间——生产上的分裂（父进程 `canonical/claude`、子进程 `legacy`）在同进程用例里
**结构性不可见**。既有 7 条 wiring 用例全是同进程形态（monkeypatch 掉 spawn，或直接
调 `run_extraction_child`），因此这个缺陷带着全绿跑了一周。

实证（2026-09-09，本机 1.1.0）：18/18 次真实会话提炼全部成功、平均 6.8 词，全部落在
`~/.claude/projects/<hash>/vault-loader-state.json`（legacy）；而 hook 从
`~/.context-vault/state/claude/<hash>.json`（canonical）读，只拿到父进程自己落的
in-flight 空占位 ⇒ `session_topic_hit` 一次都没加过分，LLM 额度照付、召回零收益。
两个命名空间下同一个 cwd hash、同一批 session_id 并存，是这条结论的直接证据。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import _fine_state, _topic
from scripts._config_loader import DEFAULT_CONFIG
from scripts._frontmatter_reader import CACHE_VERSION
from scripts._state import (adopt_runtime, configure_context, current_runtime,
                            state_path_for_cwd)
from scripts._topic import load_session_topic, spawn_fine

ROOT = Path(__file__).resolve().parents[1]
STUB_WORDS = ["召回", "打分", "闸门"]
STUB_RAW = "PINNED: NONE\nWORDS: " + ", ".join(STUB_WORDS)
GEN = "0123456789abcdef"


def _payload_text(home: Path) -> str:
    """真实形状的 v1 payload：子进程要读 cache，所以在临时 HOME 里放一个最小 vault。"""
    vault = home / "Vault"
    (vault / ".meta").mkdir(parents=True, exist_ok=True)
    (vault / ".meta" / "frontmatter-cache.json").write_text(json.dumps(
        {"_version": CACHE_VERSION,
         "entries": {"笔记/召回.md": {"tags": ["召回"], "summary": "召回与打分的闸门", "mtime": 1,
                                     "keywords": []}}}, ensure_ascii=False), encoding="utf-8")
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["vault_path"] = str(vault)
    return json.dumps(_topic.build_fine_payload(
        "pin", prompt="召回 打分", config=cfg, gen_id=GEN, src_seq=1, slot=None, transcript_path="",
        injected=[], excluded=[], prev_words=[], extra_paths=[]), ensure_ascii=False)


def _run_child(cwd: Path, sid: str, runtime: str, home: Path):
    """真起一个新进程跑 `run_extraction_child`，`_call_model` 打桩不触网，payload 经 stdin 给。

    桩只替换模型调用，**不替换写盘路径解析**——被测的正是后者。
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts import _topic\n"
        f"_topic._call_model = lambda _p: {STUB_RAW!r}\n"
        f"sys.exit(_topic.run_extraction_child({[str(cwd), sid, runtime]!r}))\n"
    )
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return subprocess.run([sys.executable, "-c", code], input=_payload_text(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(ROOT), timeout=60)


def test_child_writes_where_parent_reads(tmp_home: Path, tmp_path: Path) -> None:
    """父进程 runtime=claude 时，子进程写的东西父进程必须读得到。

    这是本文件的承重断言。修复前它必然红：子进程不调 `configure_context`，
    `_RUNTIME` 停在默认 `"legacy"`，结果写进 legacy 路径，而父进程读 canonical。
    """
    cwd = tmp_path / "proj"
    cwd.mkdir()
    sid = "sess-cross-proc"
    configure_context("claude", sid)
    # 把隐含前提提成显式断言：本用例的判别力**只在** `has_legacy_data(tmp_home)` 为假时
    # 成立——否则父子都落 legacy、天然一致，「子进程不配 runtime」这个变异不再转红，
    # 承重断言变空网且完全无声。该前提由 conftest 的 tmp_home 恰好满足，而**兄弟文件**
    # `test_topic_observability.py::_setup` 正在往那里写 `.claude/skills/vault-loader/
    # config.json`；两文件 setup 一旦合并（一次很自然的 DRY 重构），守卫就整个失效而
    # CI 只会更绿。前提破掉时要红在这一行，不要静默通过。
    assert current_runtime() == "claude", (
        "前提被破坏：tmp_home 里出现了 legacy 数据，此时父子都落 legacy，"
        "本用例对『落点分裂』失去判别力")

    r = _run_child(cwd, sid, "claude", tmp_home)
    assert r.returncode == 0, f"子进程异常退出：{r.stderr}"

    assert load_session_topic(cwd, sid, 24) == STUB_WORDS, (
        "父进程读不到子进程写的主题词 —— 两者落在不同的 runtime 命名空间。"
        f"父进程读的是 {state_path_for_cwd(cwd)}")


def test_child_honours_legacy_runtime(tmp_home: Path, tmp_path: Path) -> None:
    """阳性对照：runtime=legacy 时同样要读写一致。

    没有这条，上面那条红了分不清是「落点分裂」还是「helper 自己坏了 / 桩没生效 /
    子进程根本没跑起来」——三者的表征都是「父进程读到空」。
    """
    cwd = tmp_path / "proj-legacy"
    cwd.mkdir()
    sid = "sess-legacy"
    configure_context("legacy", sid)

    r = _run_child(cwd, sid, "legacy", tmp_home)
    assert r.returncode == 0, f"子进程异常退出：{r.stderr}"

    assert load_session_topic(cwd, sid, 24) == STUB_WORDS


@pytest.mark.parametrize("runtime", ["claude", "legacy"])
def test_runtime_travels_through_argv_not_stdin(tmp_home: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch,
                                                runtime: str) -> None:
    """runtime 必须经 argv 传给子进程，不能塞进 stdin payload。

    payload 由子进程自己解析，读坏时按空 payload 记一次失败；把决定写盘落点的参数放进 payload，
    它会跟着一起丢，失败记录与后续写回静默回落 legacy —— 即本文件要守的那个缺陷换个成因复发。
    （2.4.0 full-review P-H1 之前 stdin 还是 best-effort 的管道：写入异常被吞、不影响 spawn 判定，
    那是当时的主要理由；现在 payload 先写进临时文件，写失败即 spawn 失败，上面这条理由仍然成立。）

    两档参数化同时钉住第二件事：**spawn 取的必须是父进程实际生效的那个 runtime**
    （`current_runtime()`），不是自己按 payload/config 重新推导一次。重新推导等于
    留着两个各自算落点的地方，正是本缺陷的形态；实现若改成推导，`legacy` 档必失配。
    """
    captured: dict = {}

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()

    def _fake_popen(argv, **_kw):
        captured["argv"] = list(argv)
        return _FakeProc()

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", _fake_popen)
    configure_context(runtime, "sess-X")

    spawn_fine(tmp_path, "sess-X", {"v": 1})

    argv = captured["argv"]
    assert argv[3:] == [str(tmp_path), "sess-X", runtime], (
        f"argv 尾部应为 [cwd, session_id, runtime]，实际 {argv[3:]}")


def _seed_legacy(home: Path) -> None:
    """把 tmp_home 变成「未迁移存量用户」：让 has_legacy_data() 为真。"""
    p = home / ".claude" / "skills" / "vault-loader" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")


def test_adopt_runtime_does_not_re_derive(tmp_home: Path) -> None:
    """`adopt_runtime` 采信调用方**已归一化**的结果，不再判一次环境。

    这是「单点」得以成立的关键。`configure_context` 对 `"claude"` 会再查一次
    `use_canonical_namespace()`——`claude` 是这个往返里唯一的**非不动点**。父子之间
    该判定一旦翻转（`has_legacy_data` 的三个探针里，两个是**其它组件**可能创建的
    路径），落点就分裂，即本次要消灭的缺陷换个成因复发。
    """
    _seed_legacy(tmp_home)

    configure_context("claude", "s")
    assert current_runtime() == "legacy", "前提：未迁移环境下 configure_context 会归一化成 legacy"

    adopt_runtime("claude")
    assert current_runtime() == "claude", "adopt_runtime 必须采信入参，不得重判环境"


@pytest.mark.parametrize("hostile", [
    "../../PWNED", r"..\..\PWNED", "", "unknown", "CLAUDE", "claude/../x", None, 3,
    "codex",
])
def test_adopt_runtime_keeps_the_whitelist(hostile) -> None:
    """白名单不可省：`_RUNTIME` 会被拼进 state 路径。

    放行任意字符串 = 把它变成路径组件 sink（`state_path_for_cwd` 用它拼目录），
    等价于一个任意路径 JSON 写原语。`adopt_runtime` 去掉的只能是
    `use_canonical_namespace()` 的二次推导，**不是**这道清洗。
    """
    adopt_runtime(hostile)
    assert current_runtime() == "legacy", f"{hostile!r} 不在白名单内，必须落回 legacy"


def test_child_survives_environment_flip_between_parent_and_child(
        tmp_home: Path, tmp_path: Path) -> None:
    """父进程判定之后、子进程启动之前环境翻转，落点仍须一致。

    这是 `adopt_runtime` 相对 `configure_context` 的唯一行为差别，也是它存在的理由。
    用真子进程复现那个竞态窗：父在干净 home 上归一化出 `claude`，随后 legacy 数据
    出现（其它组件写了 `~/.claude/skills/vault-loader/config.json`），子进程才启动。
    若子进程重判环境就会落 legacy 而父在 canonical ⇒ 分裂且全程无声。
    """
    cwd = tmp_path / "proj-flip"
    cwd.mkdir()
    sid = "sess-flip"

    configure_context("claude", sid)
    assert current_runtime() == "claude", "前提：干净 home 上应归一化为 claude"
    runtime_arg = current_runtime()

    _seed_legacy(tmp_home)          # ← 竞态：父判完、子未起，环境翻转

    r = _run_child(cwd, sid, runtime_arg, tmp_home)
    assert r.returncode == 0, f"子进程异常退出：{r.stderr}"

    assert load_session_topic(cwd, sid, 24) == STUB_WORDS, (
        "环境在父子之间翻转后落点分裂 —— 子进程重新推导了命名空间，"
        f"父进程读的是 {state_path_for_cwd(cwd)}")


# ---- conftest 必须复位 _state._RUNTIME（下面两条按定义顺序成对生效）----
#
# 2026-09-10 实证：`_RUNTIME` 是模块级全局，而 conftest 的 autouse fixture 当时只复位
# `_metrics`、不碰 `_state`。本文件的用例把它留在 `"claude"` 之后，那些用 `tmp_path`
# （而非 `tmp_home`）的 state 用例里 `Path.home()` 解析到**真实主目录**，一次全套跑
# 在 `~/.context-vault/state/claude/` 下留下 **252 个**测试文件（210 个带测试 sid +
# 42 个用例故意造的畸形数据）。危害有限（那些 cwd hash 永不被真实项目命中），但它是
# 「测试逃出沙箱写用户真实数据目录」这一类，不能留。

def test_runtime_pollution_is_set_up_here(tmp_home: Path) -> None:
    """故意把 `_RUNTIME` 留成 `claude`，供下一条验证复位。

    不是多余的用例：没有它，下一条的断言在「从未有人污染过」时也恒真，
    对「conftest 到底复不复位」零判别力。
    """
    configure_context("claude", "sess-pollute")
    assert current_runtime() == "claude", "前提：干净 tmp_home 上应归一化为 claude"


def test_runtime_is_reset_before_each_test() -> None:
    """上一条把 `_RUNTIME` 设成了 `claude`；conftest 不复位的话这条必红。

    **刻意不要 `tmp_home`**：这条要模拟的正是「没有 HOME 隔离的用例」——真实污染
    就发生在这种用例上。
    """
    assert current_runtime() == "legacy", (
        "_state._RUNTIME 从上一条用例泄漏了。后续用 tmp_path 的 state 用例会把文件"
        "写进真实主目录 ~/.context-vault/state/<runtime>/")


def test_payload_handoff_failure_is_spawn_error(tmp_home: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """payload 交不出去（临时文件建不了、写不进）时判 `spawn_error`、不拉起子进程，句柄照样关掉。

    子进程是 `DETACHED_PROCESS` + stdout/stderr 全 `DEVNULL`，而 `run_extraction_child`
    做的第一件事就是 `sys.stdin.buffer.read()`。2.4.0 full-review P-H1 之前 stdin 是管道：
    原实现把 `write` 与 `close` 放在同一个 `try` 里、`except` 直接 `pass`，write 一抛异常 close
    就被跳过，子进程永久阻塞在读一个不会 EOF 的管道上（2026-09-10 实证过同机制的进程链挂了 13 小时）；
    修好之后的契约是「写失败也算 spawned、但 close 必须执行」。

    现在 payload 先整份写进临时文件再交给子进程作 stdin，子进程读的是普通文件、读到 EOF 就结束，
    不存在那种阻塞；写失败时干脆不拉起子进程、判 `spawn_error`，调用方据此回滚在飞标记与槽位
    （`_maybe_spawn`）。本用例因此改钉新契约：失败、不 Popen、临时文件句柄已关闭。

    **`tmp_home` 不是摆设**：2.4.0 之前的提炼发起入口（已删除）末尾会落 in-flight 标记，
    没有它这条用例自己就会往真实主目录写 state；`spawn_fine` 不再写，tmp_home 留着防同类回归。
    """
    import tempfile

    popen_calls: list = []
    handles: list = []
    real_tmp = tempfile.TemporaryFile

    class _FullDisk:
        """写不进去的临时文件：其余行为委托给真实的临时文件，关闭时记一笔。"""

        def __init__(self) -> None:
            self.f = real_tmp()
            self.closed = False
            handles.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            self.f.close()
            self.closed = True

        def write(self, _b):
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.f, name)

    def _no_temp(*_a, **_k):
        raise OSError(13, "TEMP not writable")

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen",
                        lambda *a, **_k: popen_calls.append(a) or object())
    monkeypatch.setattr("scripts._topic.tempfile.TemporaryFile", _no_temp)
    assert spawn_fine(tmp_path, "sess-notemp", {"v": 1}) == "spawn_error"
    assert _topic.take_spawn_outcome() == "spawn_error"

    monkeypatch.setattr("scripts._topic.tempfile.TemporaryFile", _FullDisk)
    assert spawn_fine(tmp_path, "sess-badwrite", {"v": 1}) == "spawn_error"
    assert _topic.take_spawn_outcome() == "spawn_error"

    assert popen_calls == [], "payload 没交出去就不得拉起子进程"
    assert len(handles) == 1 and handles[0].closed, "写失败后临时文件句柄没被关闭"


def test_child_fine_state_lands_where_parent_reads(tmp_home: Path, tmp_path: Path) -> None:
    """fine 状态与 topics 走同一条落点链：子进程 adopt_runtime 后写，父进程按同一 runtime 读得到。
    同进程用例对「落点分裂」结构性不可见，必须真起子进程（见本文件模块 docstring）。"""
    cwd = tmp_path / "proj-fine"
    cwd.mkdir()
    sid = "sess-fine"
    configure_context("claude", sid)
    assert current_runtime() == "claude", "前提：干净 tmp_home 上应归一化为 claude"
    r = _run_child(cwd, sid, "claude", tmp_home)
    assert r.returncode == 0, f"子进程异常退出：{r.stderr}"
    p = _fine_state.fine_path(sid)
    assert p is not None and p.exists(), f"父进程读不到子进程写的 fine 状态：{p}"
    assert p.parent == tmp_home / ".context-vault" / "state" / "claude" / "fine"
    assert _fine_state.load_fine(sid).stats_q[-1]["ok"] is True
