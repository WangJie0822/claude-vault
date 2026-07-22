# -*- coding: utf-8 -*-
"""笔记路径解析的容器校验（防路径穿越）。"""
import os
import pytest
from pathlib import Path

from scripts._note_paths import resolve_note_path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / "sub").mkdir(parents=True)
    (v / "sub" / "note.md").write_text("正常笔记", encoding="utf-8")
    (tmp_path / "OUTSIDE_SECRET.txt").write_text("AWS_SECRET=leak", encoding="utf-8")
    (tmp_path / "outside.md").write_text("OUTSIDE_MD_MARKER", encoding="utf-8")
    return v


def test_normal_relative_path_resolves(vault: Path) -> None:
    got = resolve_note_path(vault, "sub/note.md")
    assert got is not None
    assert got.read_text(encoding="utf-8") == "正常笔记"


def test_absolute_path_rejected(vault: Path) -> None:
    # pathlib 语义：vault / 绝对路径 会整段替换，必须被挡
    outside = vault.parent / "outside.md"
    assert resolve_note_path(vault, str(outside)) is None


def test_dotdot_escape_rejected(vault: Path) -> None:
    assert resolve_note_path(vault, "../outside.md") is None
    assert resolve_note_path(vault, "sub/../../outside.md") is None


def test_non_md_suffix_rejected(vault: Path) -> None:
    assert resolve_note_path(vault, "../OUTSIDE_SECRET.txt") is None
    assert resolve_note_path(vault, "sub/note.txt") is None


def test_missing_file_returns_none(vault: Path) -> None:
    assert resolve_note_path(vault, "sub/nope.md") is None


@pytest.mark.skipif(os.name == "nt" and not os.environ.get("CI"),
                    reason="Windows 建符号链接需要管理员权限或开发者模式")
def test_symlink_escape_rejected(vault: Path) -> None:
    link = vault / "sub" / "link.md"
    try:
        link.symlink_to(vault.parent / "outside.md")
    except (OSError, NotImplementedError):
        pytest.skip("本环境不支持创建符号链接")
    assert resolve_note_path(vault, "sub/link.md") is None


def test_injection_text_excludes_outside_file(vault: Path) -> None:
    """端到端：build_injection_text_ups 拿到带越界 path 的强命中 entry 时，必须走
    prompt_submit_load.py:125-138 的 resolve_note_path 降级分支——注入正文含降级标记
    「（无法读取）」，且绝不含外部文件的内容特征串。

    与 test_dotdot_escape_rejected 等纯单元测试的区别：本测试直接调用生产代码路径
    build_injection_text_ups（该函数内接入 resolve_note_path 并决定注入正文），
    而非只测 resolve_note_path 本身——防止 prompt_submit_load.py 里的接入代码被
    改回裸拼接（`vault_path / ft_entry.path`）后测试仍无感全绿。
    """
    from scripts._frontmatter_reader import Entry
    from scripts.prompt_submit_load import build_injection_text_ups

    # vault fixture 已在 tmp_path 建好越界兄弟文件 outside.md（内容 "OUTSIDE_MD_MARKER"）
    entry = Entry(
        path="../outside.md",
        tags=("alpha", "beta"),  # 两个不同 ASCII 关键词命中 tags → 满足强证据档（dist=2）
        summary="alpha beta 恶意越界条目",
        mtime=1900000000,
    )
    prompt_keywords = {"alpha", "beta"}
    scored = [(8.0, 8.0, entry)]  # topical=8 达全文阈值（默认 fulltext_topical_threshold=6）
    ups_cfg = {"max_notes": 3, "fulltext_max_bytes": 8192}
    rel_cfg = {"confidence_bands": {"high": 6}, "fulltext_topical_threshold": 6,
               "short_summary_chars": 20}

    text, paths, ft = build_injection_text_ups(
        scored, "alpha, beta", prompt_keywords, ups_cfg, rel_cfg, vault_path=vault)

    # 先确认确实走进了全文分支（否则本测试根本没测到目标代码路径）
    assert ft == "../outside.md"
    assert "（无法读取）" in text
    assert "OUTSIDE_MD_MARKER" not in text
