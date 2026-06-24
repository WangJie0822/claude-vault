import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obsidian_cli import ObsidianCLI, Degraded


def test_no_hardcoded_zsh():
    txt = Path(__file__).resolve().parent.parent.joinpath("scripts/obsidian_cli.py").read_text(encoding="utf-8")
    assert '"zsh"' not in txt and "'zsh'" not in txt


def _mk_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_degraded_bump():
    d = Degraded()
    d.bump("cli-timeout")
    d.bump("cli-timeout")
    d.bump("cli-nonzero")
    assert d.counts == {"cli-timeout": 2, "cli-nonzero": 1}


def test_obsidian_cli_init_expands_vault(tmp_vault: Path):
    cli = ObsidianCLI(str(tmp_vault))
    assert cli.vault_path == tmp_vault
    assert cli.timeout == 5.0
    assert cli.degraded_counts == {}


@patch("obsidian_cli.subprocess.run")
@patch("obsidian_cli.shutil.which")
def test_probe_ok(mock_which, mock_run, tmp_vault):
    mock_which.return_value = "/usr/bin/pgrep"
    mock_run.side_effect = [
        _mk_completed(returncode=0, stdout="12345\n"),
        _mk_completed(returncode=0, stdout="/Applications/Obsidian.app/Contents/MacOS/obsidian\n"),
    ]
    cli = ObsidianCLI(str(tmp_vault))
    result = cli.probe()
    assert result == {"ok": True}


@patch("obsidian_cli.subprocess.run")
@patch("obsidian_cli.shutil.which")
def test_probe_obsidian_not_running(mock_which, mock_run, tmp_vault):
    mock_which.return_value = "/usr/bin/pgrep"
    mock_run.return_value = _mk_completed(returncode=1, stdout="")
    cli = ObsidianCLI(str(tmp_vault))
    result = cli.probe()
    assert result == {"ok": False, "reason": "obsidian-not-running"}


@patch("obsidian_cli.subprocess.run")
@patch("obsidian_cli.shutil.which")
def test_probe_cli_not_registered(mock_which, mock_run, tmp_vault):
    mock_which.return_value = "/usr/bin/pgrep"
    mock_run.side_effect = [
        _mk_completed(returncode=0, stdout="12345\n"),
        _mk_completed(returncode=1, stdout="", stderr=""),
    ]
    cli = ObsidianCLI(str(tmp_vault))
    result = cli.probe()
    assert result == {"ok": False, "reason": "cli-not-registered"}


@patch("obsidian_cli.shutil.which")
def test_probe_pgrep_missing(mock_which, tmp_vault):
    mock_which.return_value = None
    cli = ObsidianCLI(str(tmp_vault))
    result = cli.probe()
    assert result == {"ok": False, "reason": "pgrep-missing"}


@patch("obsidian_cli.subprocess.run")
def test_cli_ok(mock_run, tmp_vault):
    mock_run.return_value = _mk_completed(returncode=0, stdout='{"x":1}\n', stderr="")
    cli = ObsidianCLI(str(tmp_vault))
    result = cli._cli(["read", 'path="a.md"'])
    assert result == {"ok": True, "stdout": '{"x":1}\n', "stderr": ""}
    args, _ = mock_run.call_args
    assert args[0][0] == "zsh"
    assert args[0][1] == "-lc"
    assert args[0][2].startswith("obsidian ")


@patch("obsidian_cli.subprocess.run")
def test_cli_timeout(mock_run, tmp_vault):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="obsidian read", timeout=5.0)
    cli = ObsidianCLI(str(tmp_vault))
    result = cli._cli(["read", 'path="a.md"'])
    assert result == {"ok": False, "reason": "cli-timeout"}


@patch("obsidian_cli.subprocess.run")
def test_cli_nonzero(mock_run, tmp_vault):
    mock_run.return_value = _mk_completed(returncode=1, stdout="", stderr="boom")
    cli = ObsidianCLI(str(tmp_vault))
    result = cli._cli(["read", 'path="missing.md"'])
    assert result == {"ok": False, "reason": "cli-nonzero", "exit_code": 1, "stderr": "boom"}


@patch("obsidian_cli.subprocess.run")
def test_cli_with_vault_prefix(mock_run, tmp_vault):
    mock_run.return_value = _mk_completed(returncode=0)
    cli = ObsidianCLI(str(tmp_vault))
    cli._vault_name = "MyVault"
    cli._cli(["read", 'path="a.md"'])
    args, _ = mock_run.call_args
    assert '"MyVault"' in args[0][2]


def test_resolve_vault_matches_default(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "_cli") as mcli:
        mcli.return_value = {
            "ok": True,
            "stdout": str(tmp_vault) + "\n",
            "stderr": "",
        }
        result = cli._resolve_vault()
    assert result == {"ok": True}
    assert cli._vault_name is None


def test_resolve_vault_mismatch_finds_name(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    other = str(tmp_vault.parent / "other")
    responses = [
        {"ok": True, "stdout": other + "\n", "stderr": ""},
        {"ok": True, "stdout": f"Target\t{tmp_vault}\nOther\t{other}\n", "stderr": ""},
    ]
    with patch.object(cli, "_cli", side_effect=responses):
        result = cli._resolve_vault()
    assert result == {"ok": True}
    assert cli._vault_name == "Target"


def test_resolve_vault_not_found(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    other = str(tmp_vault.parent / "other")
    responses = [
        {"ok": True, "stdout": other + "\n", "stderr": ""},
        {"ok": True, "stdout": f"Other\t{other}\n", "stderr": ""},
    ]
    with patch.object(cli, "_cli", side_effect=responses):
        result = cli._resolve_vault()
    assert result == {"ok": False, "reason": "vault-not-open-in-obsidian"}


def test_resolve_vault_parse_fail_empty(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "_cli") as mcli:
        mcli.return_value = {"ok": True, "stdout": "", "stderr": ""}
        result = cli._resolve_vault()
    assert result == {"ok": False, "reason": "vault-parse-fail"}


def _write(vault: Path, rel: str, content: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_read_note_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "hello", "stderr": ""}):
        result = cli.read_note("a.md")
    assert result == {"ok": True, "used": "cli", "data": {"content": "hello"}}


def test_read_note_fallback(tmp_vault):
    _write(tmp_vault, "a.md", "hi")
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.read_note("a.md")
    assert result["ok"] is True
    assert result["used"] == "fallback"
    assert result["data"] == {"content": "hi"}
    assert result["reason"] == "obsidian-not-running"


def test_read_note_fallback_missing_file(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.read_note("nope.md")
    assert result == {"ok": False, "used": "fallback", "reason": "file-not-found"}


def test_create_note_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        result = cli.create_note("新/a.md", "# H")
    assert result == {"ok": True, "used": "cli", "data": {"path": "新/a.md"}}


def test_create_note_fallback(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "cli-not-registered"}):
        result = cli.create_note("新/b.md", "# H")
    assert result["ok"] is True
    assert result["used"] == "fallback"
    assert (tmp_vault / "新/b.md").read_text(encoding="utf-8") == "# H"


def test_append_note_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        result = cli.append_note("a.md", "x")
    assert result["ok"] is True
    assert result["used"] == "cli"


def test_append_note_fallback_appends(tmp_vault):
    _write(tmp_vault, "a.md", "head\n")
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.append_note("a.md", "tail")
    assert result["ok"] is True
    assert (tmp_vault / "a.md").read_text(encoding="utf-8") == "head\ntail"


def test_create_note_win_large_content_skips_cli(tmp_vault):
    # Windows + content ≥ 4KB → 主动绕过 CLI，避免触发 Obsidian forward envelope 崩溃
    cli = ObsidianCLI(str(tmp_vault))
    big = "x" * 4096
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "_ensure_ready") as mock_ready:
        result = cli.create_note("大/a.md", big)
    mock_ready.assert_not_called()  # 关键：不走 CLI 通路，连 probe 都不发起
    assert result["used"] == "fallback"
    assert result["reason"] == "win-large-content"
    assert (tmp_vault / "大/a.md").read_text(encoding="utf-8") == big
    assert cli.degraded_counts == {"win-large-content": 1}


def test_create_note_win_small_content_uses_cli(tmp_vault):
    # Windows + content < 4KB → 仍走 CLI 通路（不触发 workaround）
    cli = ObsidianCLI(str(tmp_vault))
    small = "x" * 4095
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        result = cli.create_note("小/a.md", small)
    assert result["used"] == "cli"


def test_create_note_win_chinese_bytes_skips_cli(tmp_vault):
    # 中文 1 字符 ≈ 3 字节：1400 个中文字符 ≈ 4200 byte ≥ 4096 → 应触发 workaround
    # 防回归：v1 用 len(content) 字符数判断时，本场景会漏判（5767 byte 中文笔记实证崩溃）
    cli = ObsidianCLI(str(tmp_vault))
    cn = "中" * 1400
    assert len(cn) < 4096 and len(cn.encode("utf-8")) >= 4096
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "_ensure_ready") as mock_ready:
        result = cli.create_note("中/a.md", cn)
    mock_ready.assert_not_called()
    assert result["used"] == "fallback"
    assert result["reason"] == "win-large-content"
    assert (tmp_vault / "中/a.md").read_text(encoding="utf-8") == cn


def test_create_note_win_chinese_small_bytes_uses_cli(tmp_vault):
    # 中文短笔记（< 4096 byte UTF-8）→ 不触发 workaround，仍走 CLI
    cli = ObsidianCLI(str(tmp_vault))
    cn = "中" * 1000  # ≈ 3000 byte
    assert len(cn.encode("utf-8")) < 4096
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        result = cli.create_note("中/b.md", cn)
    assert result["used"] == "cli"


def test_create_note_non_win_large_content_uses_cli(tmp_vault):
    # 非 Windows 平台 + 大 content → 不触发 workaround，仍走 CLI
    cli = ObsidianCLI(str(tmp_vault))
    big = "x" * 4096
    with patch("obsidian_cli.sys.platform", "darwin"), \
         patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        result = cli.create_note("大/a.md", big)
    assert result["used"] == "cli"


def test_append_note_win_large_content_skips_cli(tmp_vault):
    _write(tmp_vault, "a.md", "head\n")
    cli = ObsidianCLI(str(tmp_vault))
    big = "x" * 4096
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "_ensure_ready") as mock_ready:
        result = cli.append_note("a.md", big)
    mock_ready.assert_not_called()
    assert result["used"] == "fallback"
    assert result["reason"] == "win-large-content"
    assert (tmp_vault / "a.md").read_text(encoding="utf-8") == "head\n" + big


def test_property_set_win_large_value_skips_cli(tmp_vault):
    _write(tmp_vault, "a.md", "---\nx: 1\n---\nbody\n")
    cli = ObsidianCLI(str(tmp_vault))
    big = "y" * 4096
    with patch("obsidian_cli.sys.platform", "win32"), \
         patch.object(cli, "_ensure_ready") as mock_ready:
        result = cli.property_set("a.md", "long_val", big)
    mock_ready.assert_not_called()
    assert result["used"] == "fallback"
    assert result["reason"] == "win-large-content"
    # 验证文件确实被写入（fallback 走 frontmatter 改写路径）
    assert big in (tmp_vault / "a.md").read_text(encoding="utf-8")


def test_cli_failure_falls_back_and_counts(tmp_vault):
    _write(tmp_vault, "a.md", "hi")
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": False, "reason": "cli-timeout"}):
        result = cli.read_note("a.md")
    assert result["used"] == "fallback"
    assert result["reason"] == "cli-timeout"
    assert cli.degraded_counts == {"cli-timeout": 1}


FRONTMATTER = """---
tags: [工作日志]
category: 工作日志
created: 2026-04-20
summary: "示例"
---

# body
"""


def test_properties_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={
             "ok": True,
             "stdout": json.dumps({"tags": ["工作日志"], "category": "工作日志"}),
             "stderr": "",
         }):
        result = cli.properties("a.md")
    assert result["ok"] is True
    assert result["used"] == "cli"
    assert result["data"] == {"tags": ["工作日志"], "category": "工作日志"}


def test_properties_fallback_parses_frontmatter(tmp_vault):
    _write(tmp_vault, "a.md", FRONTMATTER)
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.properties("a.md")
    assert result["ok"] is True
    assert result["used"] == "fallback"
    assert result["data"]["category"] == "工作日志"
    assert result["data"]["tags"] == ["工作日志"]
    assert result["data"]["created"] == "2026-04-20"
    assert result["data"]["summary"] == "示例"


def test_property_read_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "工作日志", "stderr": ""}):
        result = cli.property_read("a.md", "category")
    assert result == {"ok": True, "used": "cli", "data": {"value": "工作日志"}}


def test_property_read_fallback(tmp_vault):
    _write(tmp_vault, "a.md", FRONTMATTER)
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.property_read("a.md", "category")
    assert result["data"]["value"] == "工作日志"


def test_property_set_fallback_writes_back(tmp_vault):
    _write(tmp_vault, "a.md", FRONTMATTER)
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.property_set("a.md", "summary", "更新后")
    assert result["ok"] is True
    content = (tmp_vault / "a.md").read_text(encoding="utf-8")
    assert 'summary: "更新后"' in content
    assert "category: 工作日志" in content  # 其他字段不动


# ---------- Task 7: search / files / reload_vault ----------

def test_search_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={
             "ok": True,
             "stdout": json.dumps(["a/x.md", "b/y.md"]),
             "stderr": "",
         }):
        result = cli.search("关键词")
    assert result["ok"] is True
    assert result["used"] == "cli"
    assert result["data"]["paths"] == ["a/x.md", "b/y.md"]


def test_search_fallback_scans_vault(tmp_vault):
    _write(tmp_vault, "a.md", "foo bar 关键词 baz")
    _write(tmp_vault, "b/c.md", "nothing here")
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.search("关键词")
    assert result["used"] == "fallback"
    assert "a.md" in result["data"]["paths"]
    assert "b/c.md" not in result["data"]["paths"]


def test_files_cli(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={
             "ok": True,
             "stdout": "a.md\nb/c.md\n",
             "stderr": "",
         }):
        result = cli.files(ext="md")
    assert result["data"]["paths"] == ["a.md", "b/c.md"]
    assert result["used"] == "cli"


def test_files_cli_skips_blank_lines(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={
             "ok": True,
             "stdout": "\na.md\n\nb.md\n\n",
             "stderr": "",
         }):
        result = cli.files(ext="md")
    assert result["data"]["paths"] == ["a.md", "b.md"]


def test_files_fallback_globs(tmp_vault):
    _write(tmp_vault, "a.md", "x")
    _write(tmp_vault, "sub/b.md", "y")
    _write(tmp_vault, "sub/c.txt", "z")  # 非 md 不计入
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.files(ext="md")
    paths = set(result["data"]["paths"])
    assert paths == {"a.md", "sub/b.md"}


def test_reload_vault_cli_only(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": True}), \
         patch.object(cli, "_resolve_vault", return_value={"ok": True}), \
         patch.object(cli, "_cli", return_value={"ok": True, "stdout": "", "stderr": ""}):
        assert cli.reload_vault() == {"ok": True, "used": "cli", "data": {}}


def test_reload_vault_noop_when_unavailable(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        result = cli.reload_vault()
    assert result == {"ok": True, "used": "fallback", "reason": "obsidian-not-running", "data": {"noop": True}}


# ---------- Task 8: main() CLI 入口 ----------

def test_main_read_outputs_json(tmp_vault, monkeypatch, capsys):
    _write(tmp_vault, "a.md", "hi")
    from obsidian_cli import main
    monkeypatch.setattr("sys.argv", ["obsidian_cli.py", "--vault", str(tmp_vault), "read", "--path", "a.md"])
    with patch("obsidian_cli.ObsidianCLI.probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        with pytest.raises(SystemExit) as exc:
            main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["used"] == "fallback"
    assert payload["data"] == {"content": "hi"}
    assert exc.value.code == 0


def test_main_exit_code_on_failure(tmp_vault, monkeypatch, capsys):
    from obsidian_cli import main
    monkeypatch.setattr("sys.argv", ["obsidian_cli.py", "--vault", str(tmp_vault), "read", "--path", "missing.md"])
    with patch("obsidian_cli.ObsidianCLI.probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        with pytest.raises(SystemExit) as exc:
            main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert exc.value.code == 1


# ---------- Task 9: 集成 smoke test（默认 skip） ----------

@pytest.mark.integration
def test_integration_create_append_read(tmp_path):
    """需要：Obsidian 已运行、obsidian CLI 已注册、tmp_path 被手动添加为 Vault。"""
    vault = tmp_path / "smoke-vault"
    vault.mkdir()
    cli = ObsidianCLI(str(vault))
    probe = cli.probe()
    if not probe["ok"]:
        pytest.skip(f"Obsidian 不可用：{probe['reason']}")

    r_create = cli.create_note("smoke.md", "# Smoke")
    assert r_create["ok"] is True and r_create["used"] == "cli", r_create

    r_append = cli.append_note("smoke.md", "\n- item\n")
    assert r_append["ok"] is True and r_append["used"] == "cli", r_append

    r_read = cli.read_note("smoke.md")
    assert r_read["ok"] is True
    assert "# Smoke" in r_read["data"]["content"]
    assert "- item" in r_read["data"]["content"]


# ---------- Follow-up (reviewer Major #1): shell injection 防护 ----------

def test_shell_quote_blocks_dollar_expansion(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    q = cli._shell_quote("$USER")
    r = subprocess.run(["zsh", "-lc", f"echo {q}"], capture_output=True, text=True)
    assert r.stdout.strip() == "$USER"


def test_shell_quote_blocks_command_substitution(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    q = cli._shell_quote("$(whoami)")
    r = subprocess.run(["zsh", "-lc", f"echo {q}"], capture_output=True, text=True)
    assert r.stdout.strip() == "$(whoami)"


def test_shell_quote_blocks_backticks(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    q = cli._shell_quote("a`whoami`b")
    r = subprocess.run(["zsh", "-lc", f"echo {q}"], capture_output=True, text=True)
    assert r.stdout.strip() == "a`whoami`b"


def test_shell_quote_preserves_single_quote(tmp_vault):
    cli = ObsidianCLI(str(tmp_vault))
    q = cli._shell_quote("it's")
    r = subprocess.run(["zsh", "-lc", f"echo {q}"], capture_output=True, text=True)
    assert r.stdout.strip() == "it's"


# ---------- Follow-up (reviewer Major #3): YAML quote-safe value roundtrip ----------

def test_property_set_fallback_value_with_double_quotes(tmp_vault):
    """value 含双引号时，写入后能通过 property_read roundtrip 读回原值。"""
    _write(tmp_vault, "a.md", FRONTMATTER)
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        cli.property_set("a.md", "summary", 'he said "hi"')
        back = cli.property_read("a.md", "summary")
    assert back["data"]["value"] == 'he said "hi"'


def test_property_set_fallback_new_field_with_quotes(tmp_vault):
    """为已有 frontmatter 新增字段，value 含双引号时可正确 roundtrip。"""
    _write(tmp_vault, "a.md", FRONTMATTER)
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        cli.property_set("a.md", "note", 'say "no"')
        back = cli.property_read("a.md", "note")
    assert back["data"]["value"] == 'say "no"'


def test_property_set_fallback_no_fm_with_quotes(tmp_vault):
    """原文无 frontmatter，新建整个块并写含引号的值，能 roundtrip。"""
    _write(tmp_vault, "a.md", "# body only\n")
    cli = ObsidianCLI(str(tmp_vault))
    with patch.object(cli, "probe", return_value={"ok": False, "reason": "obsidian-not-running"}):
        cli.property_set("a.md", "note", 'say "no"')
        back = cli.property_read("a.md", "note")
    assert back["data"]["value"] == 'say "no"'
