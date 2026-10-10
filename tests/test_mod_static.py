"""mod 的静态守卫：modules 指向受跟踪的文件、manifest 形态、calls 白名单、JS 测试通过。"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CALLS_WHITELIST = {
    "$.fs.read", "$.fs.write", "$.env.get", "$.clock.now", "$.session.surfaces",
    "$.store.get", "$.store.set", "$.store.delete", "$.store.keys",
    "$.ui.resolve", "$.ui.open", "$.ui.close", "$.ui.toast", "$.ui.invalidate",
    "$.command.register", "$.state.get", "$.state.set",
}


def _claude():
    from context_vault.exe_search import which_no_cwd
    return which_no_cwd("claude")


def test_modules_points_to_tracked_file():
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data["modules"] == ["./vault-mod.js"]
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "hooks/vault-mod.js"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert tracked == "hooks/vault-mod.js"


def test_manifest_declares_types_and_toggle():
    m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert m["types"] == "./hooks/vault-mod.d.ts"
    assert (ROOT / "hooks" / "vault-mod.d.ts").is_file()
    opt = m["userConfig"]["mod_enabled"]
    assert opt["type"] == "boolean" and opt["default"] is True
    assert "required" not in opt


# CLAUDE_CONFIG_DIR 会压过 HOME；凭据与代理都用不上，一并剥掉（节点 2 T-M8 / DO-M6）。
# Windows 上环境变量名大小写不敏感，按大写比较。
_DROP_ENV = {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
             "CLAUDE_CODE_OAUTH_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}


def _isolated_env(home: Path) -> dict:
    """claude 子进程用临时 HOME：测试不得写真实 HOME（CLAUDE.md 测试组）。"""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _DROP_ENV and not k.upper().startswith("GIT_")}
    env.update({"HOME": str(home), "USERPROFILE": str(home)})
    return env


def _assert_used_home(home: Path) -> None:
    # validate 与 test 都会往 HOME 写 .claude.json 等文件（节点 2 3A / 4A 实测）：临时 HOME 里出现
    # 文件，说明子进程用的是它而不是真实 HOME
    assert any(home.iterdir()), "claude 子进程没有使用临时 HOME"


def test_validate_calls_are_whitelisted(tmp_path):
    claude = _claude()
    if not claude:
        pytest.skip("claude 不在 PATH 上")
    r = subprocess.run([claude, "plugin", "validate", str(ROOT)], capture_output=True,
                       text=True, encoding="utf-8", timeout=120, env=_isolated_env(tmp_path))
    _assert_used_home(tmp_path)
    out = r.stdout + r.stderr
    assert "Validation passed" in out, out
    m = re.search(r"vault-mod\.js calls:\s*(.+)", out)
    assert m, out
    # 2.1.293 对经由辅助函数的调用写作 `$.env.get (via homeDir)`，括号里还可能有逗号
    listed = re.sub(r"\s*\(via [^)]*\)", "", m.group(1))
    calls = {c.strip() for c in listed.split(",") if c.strip()}
    assert "$.fs.write" in calls, out          # 阳性对照：解析没把整行吃空
    assert calls <= CALLS_WHITELIST, sorted(calls - CALLS_WHITELIST)


SNAPSHOT_FIXTURE = (ROOT / "skills" / "vault-loader" / "tests" / "fixtures" / "mod_contract"
                    / "snapshot_v1.json")


def _snapshot_fields_read(src: str) -> set:
    return set(re.findall(r"\bsnap\.([A-Za-z_]+)", src))


def test_mod_reads_only_contract_snapshot_fields():
    """mod 读的快照字段都在契约夹具里；Python 侧由 test_snapshot_keys_match_contract_fixture 钉住。

    mod 的 JS 测试内联构造快照（kit 不能 import JSON），不经过这份夹具，两侧字段名漂移只能靠这里发现。
    """
    src = (ROOT / "hooks" / "vault-mod.js").read_text(encoding="utf-8")
    keys = set(json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8")))
    read = _snapshot_fields_read(src)
    assert {"prompt_id", "status", "injected"} <= read, read     # 正则没失效
    assert read <= keys, sorted(read - keys)
    # 阳性对照：同一判据对改了字段名的源码必须报出来
    assert not _snapshot_fields_read(src.replace("snap.prompt_id", "snap.promptId")) <= keys


def test_js_suite_passes(tmp_path):
    claude = _claude()
    if not claude:
        pytest.skip("claude 不在 PATH 上")
    r = subprocess.run([claude, "plugin", "test"], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", timeout=600, env=_isolated_env(tmp_path))
    _assert_used_home(tmp_path)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    # 退出码 0 不代表跑了用例（节点 2 DO-M1）：2.1.293 的汇总形如「 31 pass / 0 fail / Ran 31 tests」
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    assert ran and int(ran.group(1)) > 0, out
    assert re.search(r"^\s*0 fail\b", out, re.M), out
