from __future__ import annotations

import json

import scripts.context_vault_doctor as doctor
from scripts.context_vault_doctor import diagnose


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_doctor_reports_migration_without_writing(tmp_path):
    legacy = tmp_path / ".claude/skills/vault-loader/config.json"
    _write(legacy, {"vault_path": str(tmp_path / "vault")})
    report = diagnose(tmp_path, "codex")
    assert report["ok"] is True
    assert report["runtime"] == "codex"
    assert report["migration_needed"] is True
    assert report["coexistence_risk"] is False
    assert not (tmp_path / ".context-vault").exists()


def test_doctor_reports_enabled_legacy_plugin(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    _write(settings, {
        "enabledPlugins": {"claude-vault@claude-vault-marketplace": True}
    })
    assert diagnose(tmp_path, "claude")["coexistence_risk"] is True


def test_doctor_survives_malformed_legacy_config(tmp_path):
    """排障工具在「被诊断对象坏掉」时不得自己先崩。

    `inspect()` 对畸形 legacy config 抛 JSONDecodeError、`VERSION` 缺失抛
    FileNotFoundError，旧实现两处都是裸调 ⇒ 最需要 doctor 的场景下它给的是 traceback。
    """
    legacy = tmp_path / ".claude" / "skills" / "vault-loader"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text("{ broken json", encoding="utf-8")

    report = doctor.diagnose(tmp_path)          # 不得抛
    assert report["ok"] is False
    assert any("无法解析" in e for e in report["errors"])
    # 其余诊断照常给出——降级不等于什么都不报
    assert report["plugin_root"]
    assert "manifests" in report


def test_doctor_main_never_raises(tmp_path, monkeypatch, capsys):
    """最外层兜底：diagnose 若仍抛异常，main 也必须给 JSON 而不是 traceback。"""
    monkeypatch.setattr(doctor, "diagnose",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = doctor.main(["--home", str(tmp_path)])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any("boom" in e for e in payload["errors"])
