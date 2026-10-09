"""守卫：受跟踪文件中不得出现私钥、令牌赋值与真实 Windows 用户目录。

这三条通用 pattern 原本只在 `scripts/build_codex_artifact.py` 的 `SENSITIVE` 里，
经它的测试间接在每次 `pytest tests/` 时扫描整棵跟踪树；作者私有的
`packaging/build_plugin.py` 不覆盖这三类。2.0.0 删除 Codex 构建器之前，
先把这道扫描搬成独立守卫，扫描不出现空窗。
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_PREFIXES = ("packaging/", "docs/superpowers/")
SENSITIVE = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{16,}"),
    re.compile(rb"(?i)[A-Z]:\\Users\\[^\\\s]+\\"),
)
# 按完整相对路径精确豁免，不用目录前缀：目录级豁免会让该目录下将来新增的任何
# 文件自动免检，那正是把闸门变成盲区的方式。
SCAN_EXEMPT = (
    "skills/summarize-session/tests/test_sensitive_patterns.py",
)
# 每条 pattern 一个阳性样本，字面量拆开拼接（本文件自己在扫描论域内）
SAMPLES = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "api" + '_key = "abcdef0123456789abcdef0123456789"',
    "C:" + chr(92) + "Users" + chr(92) + "somebody" + chr(92) + "notes",
)


def _tracked(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0")
            if p and not p.startswith(SKIP_PREFIXES)]


def _release_gate_patterns(root: Path = ROOT) -> tuple:
    """软依赖作者私有发布闸门的完整清单；分发副本与 worktree 里没有 `packaging/`，只跑
    通用集（由 `test_release_gate_patterns_are_loaded_or_visibly_absent` 以 skip 留痕）。

    按路径加载、用专属模块名：不动 `sys.path`，也不会拿到同进程里缓存的别的
    `build_plugin`。闸门在却加载失败时让异常冒出来——吞掉它与「闸门不在」不可区分。
    """
    gate = root / "packaging" / "build_plugin.py"
    if not gate.is_file():
        return ()
    spec = importlib.util.spec_from_file_location("context_vault_release_gate", gate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.SECRET_PATTERNS)


def _hits(data: bytes, extra: tuple) -> bool:
    if b"\x00" in data[:4096]:          # 二进制按内容探测跳过，不用后缀白名单
        return False
    return (any(p.search(data) for p in SENSITIVE)
            or any(p.search(data.decode("utf-8", "replace")) for p in extra))


def scan(root: Path, rels: list[str], exempt: tuple, extra: tuple
         ) -> tuple[list[str], list[str], list[str]]:
    """返回 (命中文件, 已不需要的豁免, 指向论域外的豁免)。

    豁免清单两道防腐：清单项不在论域内（死条目）、清单项已不再命中（无人知晓的
    免检通道），两者都必须报出来，让「豁免」无法悄悄变成「盲区」。
    """
    stale = [r for r in exempt if r not in rels]
    findings: list[str] = []
    unused: list[str] = []
    for rel in rels:
        path = root / rel
        if not path.is_file():          # 已跟踪、但工作树里刚删掉且未提交
            continue
        hit = _hits(path.read_bytes(), extra)
        if rel in exempt:
            if not hit:
                unused.append(rel)
        elif hit:
            findings.append(rel)
    return findings, unused, stale


def test_tracked_tree_has_no_secrets():
    findings, unused, stale = scan(ROOT, _tracked(), SCAN_EXEMPT, _release_gate_patterns())
    assert not stale, f"豁免项不在跟踪论域内（死条目）：{stale}"
    assert not unused, f"豁免项已不再命中任何 pattern，应移除：{unused}"
    assert not findings, f"受跟踪文件含敏感内容：{findings}"


def test_each_pattern_has_its_own_sample():
    """样本与 pattern 一一对应：新增 pattern 时必须同步补样本，否则这条转红。"""
    assert len(SAMPLES) == len(SENSITIVE)
    for pattern, sample in zip(SENSITIVE, SAMPLES):
        assert pattern.search(sample.encode("utf-8")), sample


@pytest.mark.parametrize("index", range(len(SAMPLES)))
def test_scan_flags_every_sensitive_pattern(tmp_path, index):
    (tmp_path / "leak.txt").write_text(SAMPLES[index], encoding="utf-8")
    findings, _, _ = scan(tmp_path, ["leak.txt"], (), ())
    assert findings == ["leak.txt"]


def test_scan_skips_binary_content(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00" + SAMPLES[0].encode("utf-8"))
    assert scan(tmp_path, ["blob.bin"], (), ()) == ([], [], [])


def test_exemption_must_still_be_needed(tmp_path):
    (tmp_path / "fixture.py").write_text("nothing sensitive here", encoding="utf-8")
    _, unused, _ = scan(tmp_path, ["fixture.py"], ("fixture.py",), ())
    assert unused == ["fixture.py"]


def test_exemption_must_exist_in_universe(tmp_path):
    (tmp_path / "real.txt").write_text("fine", encoding="utf-8")
    _, _, stale = scan(tmp_path, ["real.txt"], ("gone/removed.py",), ())
    assert stale == ["gone/removed.py"]


def _write_gate(root: Path, body: str) -> None:
    (root / "packaging").mkdir()
    (root / "packaging" / "build_plugin.py").write_text(body, encoding="utf-8")


def test_release_gate_patterns_are_loaded_or_visibly_absent():
    """full-review P3A-M6 / P2A-L5：worktree 与分发副本里没有 packaging/，此时只扫 3 条
    通用 pattern。用 skip 把这一点留在 `pytest -rs` 里，而不是静默降级。"""
    if not (ROOT / "packaging" / "build_plugin.py").is_file():
        pytest.skip(f"packaging/build_plugin.py 不在此检出：本次只扫 {len(SENSITIVE)} 条通用 pattern")
    assert _release_gate_patterns(), "build_plugin.py 存在却拿不到 SECRET_PATTERNS"


def test_broken_release_gate_fails_loudly(tmp_path):
    """闸门在却加载失败时必须报红；吞掉异常与「闸门不在」不可区分（1B-L9）。"""
    _write_gate(tmp_path, "raise RuntimeError('gate broken')\n")
    with pytest.raises(RuntimeError, match="gate broken"):
        _release_gate_patterns(tmp_path)


def test_release_gate_is_loaded_by_path_not_by_cached_module_name(tmp_path, monkeypatch):
    """P4A-I1：同一进程里已缓存别的 `build_plugin` 时（例如 tests 与 packaging 两个根一起
    收集），按模块名导入会拿到缓存那份。"""
    decoy = type(sys)("build_plugin")
    decoy.SECRET_PATTERNS = (re.compile("decoy"),)
    monkeypatch.setitem(sys.modules, "build_plugin", decoy)
    _write_gate(tmp_path, "import re\nSECRET_PATTERNS = (re.compile('from-path'),)\n")
    assert [p.pattern for p in _release_gate_patterns(tmp_path)] == ["from-path"]


def test_scan_applies_extra_patterns(tmp_path):
    """extra（私有闸门的 str pattern）套在解码后的文本上；此前没有用例传过非空 extra。"""
    (tmp_path / "x.md").write_text("内网主机 build.internal.example", encoding="utf-8")
    findings, _, _ = scan(tmp_path, ["x.md"], (), (re.compile(r"\.internal\.example"),))
    assert findings == ["x.md"]


def test_real_scan_exempt_entries_are_live():
    tracked = set(_tracked())
    for rel in SCAN_EXEMPT:
        assert rel in tracked, f"豁免项已不在跟踪论域: {rel}"
        assert any(p.search((ROOT / rel).read_bytes()) for p in SENSITIVE), \
            f"豁免项已不再命中任何 pattern，应移除: {rel}"
