"""会话主题词按**项目根**隔离的守卫（2026-09-10）。

这批用例守的是本次修复的**唯一目的**——此前一条都没有：既有 roundtrip 用例
（`test_session_topic.py`）全部在同一个 `tmp_path` 内 save→load，没有一条在
save 与 load 之间换过 cwd，因此「把落点改回 `state_path_for_cwd`」这个变异
一条都不会红。

同时守住反方向：跨项目**必须**保持隔离。`_config_loader.py` 的
`session_topic_hit=2` 可叠加把无关笔记推过 `fulltext_topical_threshold`
（`test_session_topic_scoring.py::test_topic_word_alone_can_cross_fulltext_threshold`
已把该行为钉死），所以「让所有项目共享主题词」不是更彻底的修复，而是缺陷。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts import _state
from scripts._state import (MAX_STATE_BYTES, configure_context,
                            session_topics_path, state_path_for_cwd)
from scripts._topic import (MAX_TOPIC_SESSIONS, has_recent_topic_attempt,
                            load_session_topic, save_session_topic)


def _mkrepo(root: Path) -> Path:
    """造一个普通 git 检出（`.git` 是目录）。

    必须写 `HEAD`：`_looks_like_git_dir` 的判据与 git 对齐，**空 `.git/` 目录不算
    仓库**（git 自己也报 not a git repository）。此前这里只 mkdir，那既让 fixture
    偏离真实形态，也正是安全评审 S1 报的那个洞的最小构造。
    """
    g = root / ".git"
    g.mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


# --------------- `.git` 标记的有效性（S1 回归守卫）---------------

@pytest.mark.parametrize("marker", ["empty_dir", "zero_byte_file", "junk_file"])
def test_invalid_git_marker_does_not_merge_projects(tmp_path: Path,
                                                    marker: str) -> None:
    """无效的 `.git` 标记不得被认作项目根。

    安全评审 S1（CWE-653）：隔离键由文件系统内容推导，首版只判 `.exists()`，
    于是在两个项目的**共享父目录**里放一个零字节 `.git` 文件就能把它们合并进
    同一份 topics —— 而 `git rev-parse` 对这三种标记都是
    `rc=128 fatal: invalid gitfile format` / `not a git repository` 明确拒绝的。
    合并之后 A 项目的主题词可经 `session_topic_hit` 叠加把 B 项目的无关笔记
    推过全文阈值。
    """
    # ⚠️ 两个项目**自己不能有 `.git`**：有的话 `_repo_root` 第一层就命中、永远走不到
    # 共享父目录，这条守卫就退化成空网（首版即如此，M9 变异存活才暴露出来）。
    # 真实场景就是这样：两个普通子目录 + 父目录里一个无效的 `.git` 标记。
    shared = tmp_path / "shared"
    a = shared / "projA"
    b = shared / "projB"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    bogus = shared / ".git"
    if marker == "empty_dir":
        bogus.mkdir()
    elif marker == "zero_byte_file":
        bogus.write_bytes(b"")
    else:
        bogus.write_text("这不是 gitfile\n", encoding="utf-8")

    save_session_topic(a, "sess-1", ["A的词"])
    assert load_session_topic(b, "sess-1", 24) == [], (
        f"共享父目录里的无效 .git 标记（{marker}）把两个项目合并了 —— "
        "隔离键被外部内容操纵")
    assert session_topics_path(a) != session_topics_path(b)


# --------------- 修复目标：同项目内跨 cwd 可读 ---------------

def test_topic_saved_in_subdir_is_readable_from_repo_root(tmp_path: Path) -> None:
    """子目录写、项目根读 —— 这正是 2026-09-10 修掉的那个缺陷的最小复现。

    缺陷形态：会话中途 `cd` 进 `skills/vault-loader` 跑测试，那一轮 UPS 的
    `n_topic_words` 掉到 0，并白白多调一次 haiku（落在子目录那份，此后再没被读到）。
    """
    repo = _mkrepo(tmp_path / "proj")
    sub = repo / "skills" / "vault-loader"
    sub.mkdir(parents=True)

    save_session_topic(sub, "sess-1", ["召回", "打分"])

    assert load_session_topic(repo, "sess-1", 24) == ["召回", "打分"], \
        "同一项目内，子目录提炼的主题词必须能从项目根读到"


def test_topic_saved_at_root_is_readable_from_subdir(tmp_path: Path) -> None:
    """反方向同样要成立（真实顺序：会话在根目录起，中途 cd 进子目录）。"""
    repo = _mkrepo(tmp_path / "proj")
    sub = repo / "a" / "b" / "c"
    sub.mkdir(parents=True)

    save_session_topic(repo, "sess-1", ["主题"])

    assert load_session_topic(sub, "sess-1", 24) == ["主题"]


def test_attempt_marker_also_crosses_cwd(tmp_path: Path) -> None:
    """spawn 门禁也必须跨 cwd 生效，否则换目录会重复拉起付费子进程。

    这是缺陷的**成本面**：`has_recent_topic_attempt` 的 docstring 承诺
    「每个 TTL 窗口最多一次」，按 cwd 隔离时实际粒度是「每个 (cwd, session)
    窗口最多一次」，该承诺兑现不了。
    """
    repo = _mkrepo(tmp_path / "proj")
    sub = repo / "sub"
    sub.mkdir()

    save_session_topic(repo, "sess-1", [])          # 落 in-flight 占位

    assert has_recent_topic_attempt(sub, "sess-1", 24) is True, \
        "换到子目录后不得再次 spawn（否则每换一个目录就多付一次 haiku）"


# --------------- 反方向：跨项目必须隔离 ---------------

def test_topic_isolated_across_repos(tmp_path: Path) -> None:
    """同一 session 跨**项目**时不得共享主题词。

    这条挡住「全局单文件」「按 session_id 存一份」这类"更彻底"的修法：
    A 项目的主题词经 session_topic_hit 叠加，足以把 B 项目的无关笔记
    推过全文阈值（数千字注入）。
    """
    a = _mkrepo(tmp_path / "repoA")
    b = _mkrepo(tmp_path / "repoB")

    save_session_topic(a, "sess-1", ["A项目的词"])

    assert load_session_topic(b, "sess-1", 24) == [], \
        "跨项目主题污染：A 项目的主题词不得泄漏到 B 项目"
    assert has_recent_topic_attempt(b, "sess-1", 24) is False, \
        "跨项目时 B 应当允许自己提炼，不受 A 的尝试记录压制"


def test_non_git_dir_falls_back_to_cwd_isolation(tmp_path: Path) -> None:
    """不在任何 git 仓库内时回落 cwd 自身，保持原有隔离粒度。"""
    plain = tmp_path / "plain"
    sub = plain / "sub"
    sub.mkdir(parents=True)

    save_session_topic(sub, "sess-1", ["词"])

    assert load_session_topic(sub, "sess-1", 24) == ["词"]
    assert load_session_topic(plain, "sess-1", 24) == [], \
        "非 git 目录没有项目根可归一，应退回按 cwd 隔离"


# --------------- worktree / submodule：.git 是文件 ---------------

def test_git_as_file_is_treated_as_repo_root(tmp_path: Path) -> None:
    """worktree 与 submodule 的 `.git` 是**文件**不是目录。

    语义要求：归一到 worktree 自己的根，**不**跳到主检出——不同 worktree 是
    不同工作副本，不该共享主题词。本机当前无此类检出，故这条用例是该分支的
    唯一覆盖（PoC 阶段未能实测）。
    """
    main = _mkrepo(tmp_path / "main")
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n",
                             encoding="utf-8")

    save_session_topic(wt / "sub", "sess-1", ["worktree的词"])

    assert load_session_topic(wt, "sess-1", 24) == ["worktree的词"], \
        "`.git` 是文件时也应被认作项目根（子目录归一到 worktree 根）"
    assert load_session_topic(main, "sess-1", 24) == [], \
        "worktree 的主题词不得泄漏到主检出"


# --------------- 路径形态：不得漏接 runtime 命名空间 ---------------

@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_path_carries_runtime_segment(tmp_path: Path, runtime: str) -> None:
    """`session_topics_path` 必须含 `_RUNTIME` 段。

    漏接的话 `adopt_runtime` 的整套保护（白名单 + 父子单点）对主题词全部失效，
    而只断言「父子落点一致」的用例**仍会绿**——那正是 1.2.0 修掉的缺陷形态。
    """
    original = _state._RUNTIME
    try:
        configure_context(runtime)
        p = session_topics_path(tmp_path)
        assert _state.current_runtime() in p.parts, \
            f"落点 {p} 未携带 runtime 段，命名空间隔离对主题词失效"
    finally:
        _state._RUNTIME = original


def test_session_capacity_survives_a_day_of_sessions_in_one_repo(
        tmp_path: Path) -> None:
    """同一仓库一天内的多个会话不得互相挤掉。

    full-review 抓出的 High：`MAX_TOPIC_SESSIONS = 5` 是按 **per-cwd 文件**定的，
    落点改成按项目根后，同一仓库**一天内**（TTL 默认 24h）跑过的全部会话共享这些
    槽位 —— 不是"同时活跃"的数量，是"24h 内出现过"的数量。实测第 6 个会话把最旧的
    挤掉，其 `has_recent_topic_attempt` 随之转 False → 重新 spawn 一次付费 haiku，
    复现的正是本次修复要解决的症状。

    **12 是刻意写死的字面量，不从 MAX_TOPIC_SESSIONS 反算** —— 从常量反算会让
    fixture 随常量同步漂移，那个常量的取值本身就再也测不出来了。12 代表「一天在
    同一个仓库开十来个会话」这个完全正常的用量。
    """
    repo = _mkrepo(tmp_path / "proj")
    for i in range(12):
        sub = repo / f"mod{i}"
        sub.mkdir()
        save_session_topic(sub, f"sess-{i}", [f"词{i}"])
        time.sleep(0.002)          # 拉开 ts，避免同秒歧义

    for i in range(12):
        assert load_session_topic(repo, f"sess-{i}", 24) == [f"词{i}"], \
            f"sess-{i} 被挤掉了 —— 同一仓库一天内的会话数超过容量，会重复付费提炼"
        assert has_recent_topic_attempt(repo, f"sess-{i}", 24) is True


def test_repo_root_agrees_with_git_toplevel(tmp_path: Path) -> None:
    """`_repo_root`（纯 stat）与 `session_start_load._get_git_toplevel`（fork git）
    在常规检出下必须给出同一个项目根。

    full-review 的 F1：系统里现在有**两个互不相识的项目根定义**，而后者决定项目
    CLAUDE.md 的 disable/tags 作用域。两者若分叉，会出现「SessionStart 认这个项目、
    UPS 的主题词认另一个」的错位。已知**允许**分叉的两种情形（不在本用例范围）：
    git 不在 PATH（fork 版返回 None，stat 版照常工作 —— 这正是不 fork 的好处之一）、
    `.git` 指向已删除的 gitdir。
    """
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("git"):
        pytest.skip("git 不在 PATH —— 该分歧本身是已知且可接受的")

    from scripts.session_start_load import _get_git_toplevel
    from scripts._state import _repo_root

    # ⚠️ 必须 `git init` 造**真**仓库：本文件其余用例用的 `_mkrepo` 只 mkdir 一个
    # 空 `.git` 目录，那对纯 stat 的 `_repo_root` 足够，但 `git rev-parse` 不认，
    # 会让本用例恒 skip 变成空网（首版即如此，且 skip 不计失败、没人会发现）。
    repo = tmp_path / "proj"
    repo.mkdir()
    _sp.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, timeout=30)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    checked = 0
    for probe in (repo, sub):
        stat_root = _repo_root(probe)
        git_root = _get_git_toplevel(probe)
        assert git_root is not None, \
            "git init 过的目录 rev-parse 仍失败 —— 本用例的前提不成立，不是可跳过的情形"
        assert stat_root.resolve() == git_root.resolve(), (
            f"两个项目根定义分叉：stat={stat_root} git={git_root}；"
            "SessionStart 的作用域与主题词落点会错位")
        checked += 1
    assert checked == 2, "两个探测点都必须真正比对过，不得静默跳过"


@pytest.mark.parametrize("charset,ch", [
    ("cjk3", "词"),          # 3 字节，本功能提炼的就是中文
    ("nonbmp4", "𝄞"),        # 4 字节非 BMP，字节维度的最坏情况
])
def test_file_stays_under_byte_budget_with_wide_chars(tmp_path: Path,
                                                      charset: str, ch: str) -> None:
    """满尺寸**宽字符** entry 写满后，文件仍须小于 MAX_STATE_BYTES 且不被整份重置。

    安全/性能两维独立命中的同一条：条数上限单独用不住。一条 entry 的字节数跨字符集
    差 3 倍以上（满尺寸实测 ASCII 1041B / 3 字节中文 2625B / 4 字节非 BMP 3417B），
    而 `MAX_TOPIC_SESSIONS=32` 当初是按 **ASCII 口径**（991B）论证的 —— 非 BMP 下
    写到第 31 条即越过 `MAX_STATE_BYTES`，`update_json` 把整份文件当损坏重置，
    实测 entries 31→1，一次抹掉 30 个会话的主题词、每个都要再付一次 haiku。

    **刻意用宽字符而不是 ASCII**：用 ASCII 测这条永远绿，正是原来漏掉它的原因。
    """
    from scripts._topic import MAX_TOPIC_WORD_LEN, TOPIC_TRIM_BYTES
    repo = _mkrepo(tmp_path / "proj")
    word = ch * MAX_TOPIC_WORD_LEN          # 单词顶到长度上限
    for i in range(MAX_TOPIC_SESSIONS + 4):
        save_session_topic(repo, f"sess-{i}", [word] * 8)

    p = session_topics_path(repo)
    size = p.stat().st_size
    assert size <= TOPIC_TRIM_BYTES, \
        f"[{charset}] 文件 {size}B 越过字节预算 {TOPIC_TRIM_BYTES}B"
    assert size < MAX_STATE_BYTES, \
        f"[{charset}] 文件 {size}B 越过 MAX_STATE_BYTES，会触发 update_json 整份重置"

    data = json.loads(p.read_text(encoding="utf-8"))
    # ⚠️ 判据必须能区分「正常裁剪」与「整份重置」。`>= 1` 太弱：越过
    # MAX_STATE_BYTES 后 update_json 把文件当损坏重置，只剩最新那 1 条，
    # 文件反而变小 —— 上面的 size 断言会**通过**，`>= 1` 也通过，于是 4 字节
    # 字符那一档的变异照样存活（M8 第一轮实测：只杀了 cjk3、放过 nonbmp4）。
    # 正常裁剪下应保留 TOPIC_TRIM_BYTES/单条字节 ≈ 24~31 条。
    assert len(data["topics"]) >= 8, (
        f"[{charset}] 只剩 {len(data['topics'])} 条 —— 整份被重置了，"
        "全部会话的主题词一次丢光，每个都要再付一次 haiku")
    # 最后写入的那条必须活着：字节裁剪不得把当前会话自己丢掉
    assert load_session_topic(repo, f"sess-{MAX_TOPIC_SESSIONS + 3}", 24), \
        "字节裁剪把刚写入的会话丢掉了，该会话将永远存不进主题词"


def test_session_capacity_has_absolute_floor() -> None:
    """钉住绝对下界，而不是与别的常量的相对关系。

    CLAUDE.md 的教训：修复必须锁绝对下界 —— 相对关系断言（如「容量 > 某值」）
    在两个值一起回退时仍然成立，挡不住整体 revert。
    """
    assert MAX_TOPIC_SESSIONS >= 32, (
        "容量下界不得低于 32：落点按项目根隔离后，同一仓库 24h 内的全部会话共享"
        "这些槽位；取值依据与实测尺寸见 _topic.py 中该常量的注释")


def test_topics_and_injection_state_are_different_files(tmp_path: Path) -> None:
    """两者必须分属不同文件，且**判据不同**（项目根 vs cwd）。

    注入去重按 cwd 隔离是 `configure_context` 里写明的有意设计，不得为了
    "对齐"把它也改成项目根——那会让同一仓库不同子目录共享去重集。
    """
    repo = _mkrepo(tmp_path / "proj")
    sub = repo / "sub"
    sub.mkdir()

    assert session_topics_path(sub) != state_path_for_cwd(sub)
    # topics：子目录与根归一到同一份
    assert session_topics_path(sub) == session_topics_path(repo)
    # 注入去重：仍按 cwd 分开
    assert state_path_for_cwd(sub) != state_path_for_cwd(repo), \
        "注入去重不得被顺手改成按项目根隔离"


@pytest.mark.parametrize("runtime", ["legacy", "claude", "codex"])
def test_topics_and_state_differ_when_cwd_is_repo_root(tmp_path: Path,
                                                       runtime: str) -> None:
    """cwd **恰好是项目根**时两者也必须分属不同文件 —— 三个 runtime 都要验。

    变异验证补的两个缺口（2026-09-10）：
    ① 上一条用例拿子目录做断言，而 `_cwd_hash(sub) != _cwd_hash(repo)` 让路径
       天然不同，于是「去掉 `topics` 路径段」的变异存活。真正会撞名的恰恰是
       **最常见**的情形——会话就在项目根里跑，此时 `_repo_root(cwd) == cwd`，
       少了 `topics` 段两者就是同一个文件，主题词与注入去重互相覆盖。
    ② `_state._RUNTIME` 的模块级默认值是 `"legacy"`，不显式 `configure_context`
       的用例**全部跑在 legacy 分支**，而真实用户绝大多数在 canonical。只测默认
       值等于把 canonical 分支的路径构成整个漏掉，该变异因此仍能存活。
    """
    original = _state._RUNTIME
    try:
        configure_context(runtime)
        repo = _mkrepo(tmp_path / "proj")

        assert session_topics_path(repo) != state_path_for_cwd(repo), \
            f"[{runtime}] cwd 即项目根时，topics 与注入去重仍须落在不同文件"

        # 行为层面再验一次：写 topics 不得让 paths 消失，反之亦然
        from scripts._state import load_already_injected, save_injected
        save_injected(repo, ["x.md"])
        save_session_topic(repo, "sess-1", ["词"])
        assert load_already_injected(repo, 24) == {"x.md"}
        assert load_session_topic(repo, "sess-1", 24) == ["词"]
    finally:
        _state._RUNTIME = original


# --------------- legacy 命名空间（未迁移的 0.9.x 用户）---------------

def test_legacy_namespace_keeps_repo_scope_and_own_dir(tmp_path: Path) -> None:
    """legacy 分支同样按项目根归一，且落在自己的目录里。

    变异验证补的缺口（2026-09-10）：其余用例全跑在 canonical runtime，legacy
    分支零覆盖，「legacy 去掉目录段」的变异因此存活。少了目录段会把文件直接堆进
    `~/.claude/` 根，与该目录下其它同名产物存在撞名风险。
    """
    original = _state._RUNTIME
    try:
        configure_context("legacy")
        repo = _mkrepo(tmp_path / "proj")
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)

        p = session_topics_path(sub)
        assert "vault-loader-topics" in p.parts, \
            f"legacy 落点 {p} 缺少专属目录段，会直接堆进 ~/.claude/ 根"
        assert p == session_topics_path(repo), \
            "legacy 下同样要按项目根归一"

        save_session_topic(sub, "sess-1", ["词"])
        assert load_session_topic(repo, "sess-1", 24) == ["词"]
    finally:
        _state._RUNTIME = original
