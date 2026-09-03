"""vault 路径字段一致性测试。

契约是「vault-loader 的默认 vault 与 summarize-session SKILL.md 文档化的默认值
**同源**，且都是产品中性路径（非作者私人硬编码）」——**不是**某个具体取值。

1.0 把新装默认从 `~/.claude/knowledge-vault` 迁到 `~/.context-vault/knowledge-vault`，
并把 SKILL.md 里的键从 `default_vault_path` 换成 canonical 配置的 `vault_path`。
旧断言把「当时的取值」和「当时的键名」都钉死了，于是正确的迁移会在此转红——
那是「断言比契约更严」，不是实现有错。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VL = ROOT / "skills/vault-loader/scripts"
SS_SKILL_MD = ROOT / "skills/summarize-session/SKILL.md"

# SKILL.md 里文档化默认 vault 的两种键名：canonical 的 `vault_path`（1.0）与
# 0.9.x 的 `default_vault_path`（兼容段落里仍会提到）。取第一个匹配到的。
_DOC_DEFAULT_RE = re.compile(r'"(?:vault_path|default_vault_path)"\s*:\s*"([^"]+)"')


def _doc_default() -> str:
    assert SS_SKILL_MD.exists(), f"summarize-session SKILL.md 不存在：{SS_SKILL_MD}"
    text = SS_SKILL_MD.read_text(encoding="utf-8")
    match = _DOC_DEFAULT_RE.search(text)
    assert match, "SKILL.md 未文档化默认 vault 路径（vault_path / default_vault_path 均未找到）"
    return match.group(1)


def _vl_default() -> str:
    sys.path.insert(0, str(VL))
    from _config_loader import DEFAULT_CONFIG
    return DEFAULT_CONFIG["vault_path"]


def test_both_defaults_same():
    """两侧默认必须同源：都含 knowledge-vault，且 basename 一致。"""
    vl_default = _vl_default()
    assert "knowledge-vault" in vl_default, (
        f"vault-loader DEFAULT_CONFIG.vault_path={vl_default!r} 未含 'knowledge-vault'"
    )
    ss_default = _doc_default()
    assert "knowledge-vault" in ss_default, (
        f"SKILL.md 文档化默认={ss_default!r} 未含 'knowledge-vault'"
    )
    vl_basename = Path(vl_default.replace("~", str(Path.home()))).parts[-1]
    ss_basename = ss_default.rstrip("/").split("/")[-1]
    assert vl_basename == ss_basename, (
        f"vault-loader basename={vl_basename!r} 与 SKILL.md basename={ss_basename!r} 不一致"
    )


def test_vl_default_not_private_hardcode():
    """vault-loader 默认路径不得是旧私人硬编码（含 Vault 但不含 knowledge-vault）。"""
    vp = _vl_default()
    if "Vault" in vp:
        assert "knowledge-vault" in vp, (
            f"vault_path={vp!r} 含 'Vault' 但不含 'knowledge-vault'，疑似旧私人硬编码"
        )


def test_doc_default_matches_canonical_implementation():
    """SKILL.md 文档化的默认值必须与**实现**给 canonical 配置算出的默认一致。

    这是本文件最有价值的一条：它钉的是「文档与实现同源」，而不是某个字面量。
    改默认路径时若只改了一边，这里会红。
    """
    sys.path.insert(0, str(VL))
    from _config_loader import _default_vault_for
    from context_vault.paths import canonical_config

    impl = _default_vault_for(canonical_config()).replace("\\", "/")
    doc = _doc_default().replace("~/", "").rstrip("/")
    assert impl.endswith(doc), (
        f"SKILL.md 写的是 {doc!r}，而实现给 canonical 的默认是 {impl!r}"
    )


def test_legacy_default_is_documented_as_compat():
    """0.9.x 默认路径必须仍出现在 SKILL.md 的兼容说明里。

    1.0 对既有用户的承诺就是「旧配置存在时保持旧路径」；文档若不提它，
    用户无从判断自己升级后该看哪个目录。
    """
    text = SS_SKILL_MD.read_text(encoding="utf-8")
    assert ".claude/skills/summarize-session/config.json" in text,         "SKILL.md 应说明 0.9.x 旧配置的兼容读取路径"
