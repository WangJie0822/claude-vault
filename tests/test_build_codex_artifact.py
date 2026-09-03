from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_codex_artifact", ROOT / "scripts/build_codex_artifact.py"
)
builder = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(builder)


def test_dirty_development_build_has_standard_layout(tmp_path):
    plugin = builder.build(tmp_path / "market", allow_dirty=True)
    assert (plugin / ".codex-plugin/plugin.json").is_file()
    market = json.loads((tmp_path / "market/.agents/plugins/marketplace.json")
                        .read_text(encoding="utf-8"))
    assert market["plugins"][0]["source"]["path"] == "./plugins/context-vault"


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    """把扫描论域搬到 tmp_path，并隔离掉发布闸门的软依赖。

    不隔离 `_release_gate_patterns` 的话，本机（有 `packaging/`）与分发副本（没有）
    会得到不同结果——用例会在两处给出不同结论，那就不是守卫了。
    """
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "_release_gate_patterns", lambda: ())
    monkeypatch.setattr(builder, "SCAN_EXEMPT", ())

    def write(rel: str, text: str) -> Path:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    return write


# 字面量刻意拆开拼接：本文件自己也在 `git ls-files` 论域里，写成完整形态会让
# 脱敏闸门（本模块的 SENSITIVE 与 build_plugin 的 SECRET_PATTERNS 都算）命中，
# 逼得把整个测试文件加进豁免清单——豁免面越大盲区越大，而拼接是零成本的替代。
PRIVATE_KEY = "-----BEGIN " + "PRIVATE KEY-----"


def test_artifact_scan_rejects_private_key(scan_env):
    leak = scan_env("plugins/leak.txt", PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="sensitive content"):
        builder._scan([leak])


def test_artifact_scan_covers_every_sensitive_pattern(scan_env):
    """三条 pattern 各要有一条用例——只验私钥那条，另两条删掉也全绿。"""
    samples = {
        "key.txt": PRIVATE_KEY,
        "token.txt": "api" + '_key = "abcdef0123456789abcdef0123456789"',
        "winpath.txt": "C:" + chr(92) + "Users" + chr(92) + "somebody" + chr(92) + "notes",
    }
    for name, text in samples.items():
        path = scan_env(name, text)
        with pytest.raises(RuntimeError, match="sensitive content"):
            builder._scan([path])


def test_scan_exemption_must_still_be_needed(scan_env, monkeypatch):
    """豁免项若已不再命中任何 pattern，必须报错要求移除。

    否则它就退化成一条无人知晓的免检通道：将来该文件被塞进真正的敏感内容，
    闸门会一声不响地放行。
    """
    clean = scan_env("fixture.py", "nothing sensitive here")
    monkeypatch.setattr(builder, "SCAN_EXEMPT", ("fixture.py",))
    with pytest.raises(RuntimeError, match="no longer needed"):
        builder._scan([clean])


def test_scan_exemption_must_exist_in_artifact(scan_env, monkeypatch):
    """豁免清单指向论域外的文件（如已删除）时必须报错，防止死条目常驻。"""
    other = scan_env("real.txt", "fine")
    monkeypatch.setattr(builder, "SCAN_EXEMPT", ("gone/removed.py",))
    with pytest.raises(RuntimeError, match="outside the artifact"):
        builder._scan([other])


def test_exempted_file_is_skipped_when_it_still_hits(scan_env, monkeypatch):
    """正常路径：豁免项确实命中 pattern 时被跳过，其余文件照常受检。"""
    fixture = scan_env("fixture.py", PRIVATE_KEY)
    monkeypatch.setattr(builder, "SCAN_EXEMPT", ("fixture.py",))
    builder._scan([fixture])  # 不抛
    leak = scan_env("other.txt", PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="sensitive content"):
        builder._scan([fixture, leak])


def test_real_scan_exempt_entries_are_live(tmp_path):
    """生产 SCAN_EXEMPT 的每一项都必须仍在跟踪集内、且确实会被命中。

    这条钉的是真实清单（不走 scan_env 隔离），所以清单腐烂时它会直接转红。
    """
    tracked = {p.relative_to(builder.ROOT).as_posix() for p in builder.inventory(allow_dirty=True)}
    for rel in builder.SCAN_EXEMPT:
        assert rel in tracked, f"豁免项已不在分发论域: {rel}"
        data = (builder.ROOT / rel).read_bytes()
        assert any(p.search(data) for p in builder.SENSITIVE), \
            f"豁免项已不再命中任何 pattern，应移除: {rel}"


def test_scan_failure_leaves_no_half_baked_output(tmp_path, monkeypatch):
    """扫描命中时不得留下半成品产物目录。

    扫产物（而非扫源）的旧实现会先把文件拷完再报错，于是磁盘上留着一份含敏感
    内容的目录，且重跑因 FileExistsError 被拒，逼用户手工清理。
    """
    output = tmp_path / "market"
    monkeypatch.setattr(builder, "_scan", lambda sources: (_ for _ in ()).throw(
        RuntimeError("sensitive content found in artifact: x")))
    with pytest.raises(RuntimeError, match="sensitive content"):
        builder.build(output, allow_dirty=True)
    assert not output.exists(), "扫描失败后不应留下产物目录"
