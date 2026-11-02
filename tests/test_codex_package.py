"""按真实跟踪清单构包；安装材料与所有入口资源必须同时存在。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_tracked_package_contains_both_hosts_and_resolves_codex_resources(tmp_path):
    tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"], timeout=30).decode("utf-8").split("\0")
    tracked = [name for name in tracked if name]
    required = {".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".agents/plugins/marketplace.json",
                "hooks/hooks.json", "hooks/codex-hooks.json", "hooks/vault-mod.js", "scripts/vault_control.py",
                "context_vault/host_defaults.py", "context_vault/panel_server.py", "context_vault/panel_launcher.py",
                "context_vault/panel_bootstrap.py", "context_vault/panel_view.py", "context_vault/panel/index.html", "context_vault/panel/panel.js",
                "context_vault/panel/panel.css", "skills/vault-injected/SKILL.md"}
    required.update("skills/" + name + "/SKILL.md" for name in ("vault", "vault-loader", "config", "summarize-session"))
    assert required <= set(tracked)
    package = tmp_path / "context-vault.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as out:
        for name in tracked:
            out.write(ROOT / name, name)
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert names == set(tracked)
        assert not any(name.startswith((".tmp/", ".superpowers/", ".full-review/", "docs/superpowers/", "packaging/")) for name in names)
        manifest = json.loads(archive.read(".codex-plugin/plugin.json"))
        market = json.loads(archive.read(".agents/plugins/marketplace.json"))
        assert market["name"] == "context-vault-marketplace"
        assert len(market["plugins"]) == 1
        assert market["plugins"][0]["name"] == manifest["name"]
        assert market["plugins"][0]["source"] == {"source": "local", "path": "."}
        hooks = manifest["hooks"].removeprefix("./")
        assert hooks in names
        assert all(manifest["skills"].removeprefix("./") + skill + "/SKILL.md" in names
                   for skill in ("vault", "vault-loader", "config", "summarize-session", "vault-injected"))
        extracted = tmp_path / "installed"
        archive.extractall(extracted)
    probe = "\n".join([
        "from pathlib import Path",
        "import sys",
        "root = Path(sys.argv[1]).resolve()",
        "sys.path.insert(0, str(root))",
        "from context_vault import host_config, host_defaults",
        "assert Path(host_defaults.__file__).resolve().is_relative_to(root)",
        "assert host_config.CODEX_RELEVANCE_DEFAULTS is host_defaults.CODEX_RELEVANCE_DEFAULTS",
        "sys.path.insert(0, str(root / 'skills/vault-loader'))",
        "from scripts._config_loader import DEFAULT_CONFIG",
        "assert DEFAULT_CONFIG['runtimes']['codex']['relevance']['fine_exclude_tags'] == ['private']",
        "assert host_config.resolve_model_selection({}, host='codex').model is None",
        "print('installed defaults/model compatibility: ok')",
    ])
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(extracted)], env=env,
                            cwd=extracted, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "installed defaults/model compatibility: ok" in result.stdout
