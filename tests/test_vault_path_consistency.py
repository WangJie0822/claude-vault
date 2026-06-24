"""Task 3.2: vault 路径字段一致性测试。

验证：vault-loader DEFAULT_CONFIG.vault_path 与 summarize-session SKILL.md 中
文档化的 default_vault_path 默认值均指向 ~/.claude/knowledge-vault（中性路径）。
"""
import re
import sys
from pathlib import Path

VL = Path(__file__).resolve().parent.parent / "skills/vault-loader/scripts"
SS_SKILL_MD = Path(__file__).resolve().parent.parent / "skills/summarize-session/SKILL.md"


def test_both_defaults_same():
    """vault-loader vault_path 与 summarize-session default_vault_path 文档默认值均含 knowledge-vault。"""
    sys.path.insert(0, str(VL))
    from _config_loader import DEFAULT_CONFIG
    vl_default = DEFAULT_CONFIG["vault_path"]

    # vault-loader 侧
    assert "knowledge-vault" in vl_default, (
        f"vault-loader DEFAULT_CONFIG.vault_path={vl_default!r} 未含 'knowledge-vault'"
    )

    # summarize-session 侧：从 SKILL.md 提取文档化的 default_vault_path 默认值
    assert SS_SKILL_MD.exists(), f"summarize-session SKILL.md 不存在：{SS_SKILL_MD}"
    skill_text = SS_SKILL_MD.read_text(encoding="utf-8")
    # 匹配 "default_vault_path": "..." 或 default_vault_path: "..."
    match = re.search(r'"default_vault_path"\s*:\s*"([^"]+)"', skill_text)
    assert match, "SKILL.md 中未找到 default_vault_path 默认值定义"
    ss_default = match.group(1)
    assert "knowledge-vault" in ss_default, (
        f"summarize-session SKILL.md default_vault_path={ss_default!r} 未含 'knowledge-vault'"
    )

    # 二者解析到同一目录名（末尾 basename 一致）
    vl_basename = Path(vl_default.replace("~", str(Path.home()))).parts[-1]
    ss_basename = ss_default.rstrip("/").split("/")[-1]
    assert vl_basename == ss_basename, (
        f"vault-loader basename={vl_basename!r} 与 summarize-session basename={ss_basename!r} 不一致"
    )


def test_vl_default_not_private_hardcode():
    """vault-loader 默认路径不得是旧私人硬编码（含 Vault 但不含 knowledge-vault）。"""
    sys.path.insert(0, str(VL))
    from _config_loader import DEFAULT_CONFIG
    vp = DEFAULT_CONFIG["vault_path"]
    if "Vault" in vp:
        assert "knowledge-vault" in vp, (
            f"vault_path={vp!r} 含 'Vault' 但不含 'knowledge-vault'，疑似旧私人硬编码"
        )


def test_ss_skill_md_default_vault_path_is_neutral():
    """summarize-session SKILL.md 中的 default_vault_path 默认值应指向 ~/.claude/knowledge-vault。"""
    assert SS_SKILL_MD.exists(), f"SKILL.md 不存在：{SS_SKILL_MD}"
    text = SS_SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r'"default_vault_path"\s*:\s*"([^"]+)"', text)
    assert match, "SKILL.md 未定义 default_vault_path 示例值"
    val = match.group(1)
    # 必须以 .claude/knowledge-vault 结尾（含 ~ 展开形式）
    normalized = val.replace("~/", "").replace("~\\", "")
    assert normalized.endswith(".claude/knowledge-vault") or val.endswith(".claude/knowledge-vault"), (
        f"SKILL.md default_vault_path={val!r} 未以 .claude/knowledge-vault 结尾"
    )
