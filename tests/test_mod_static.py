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
    "$.ui.resolve", "$.ui.open", "$.ui.close", "$.ui.toast", "$.ui.invalidate", "$.ui.focus",
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
    assert "$.ui.focus" in calls, out          # 面板切换展开后把焦点放回该行（spec 2026-10-12 §4.3）


def test_toggle_puts_focus_back_on_the_row():
    """切换展开后先更新展开态、再把焦点放回该行（节点 1 T3）。

    行为测试做不了：claude plugin test 2.1.296 不实现插件发起的 `$.ui.focus`（实测
    `HooksError: no implementation for ui.focus`，测试挂的 ui.focus hook 也收不到），
    所以在这里钉调用形态；真实落位需在交互终端人工验证（尚无运行记录）。

    钉的是语句形态而不是子串：focus 调用必须独占一行、紧跟在 `try {` 之后。只查子串时把它包进
    永假条件（`if (Date.now() < 0) await ...`）照样通过（full-review T-M6 / JM21）。
    """
    src = (ROOT / "hooks" / "vault-mod.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"const toggle = \(id, key\) => async \(\) => \{(.*?)\n    \}\n", src, re.S)
    assert m, "找不到 toggle"
    body = m.group(1)
    upd = re.search(r"^      await update\(\$, UI, ", body, re.M)
    foc = re.search(r"^      try \{\n        await \$\.ui\.focus\(\{ requestId: PANE, key \}\)\s*(//.*)?$",
                    body, re.M)
    assert upd and foc and upd.start() < foc.start(), body
    assert "onPress: toggle(id, key)" in src             # 行按钮确实接到了 toggle


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


_ITEM_PARSERS = {"injectedItems": ("injected",), "items": ("pins.llm", "pins.user", "excluded"),
                 "ssItems": ("session_start",), "suppItems": ("supps",)}


def _fn_body(src: str, name: str) -> str:
    m = re.search(rf"^function {name}\(.*?^\}}$", src, re.M | re.S)
    assert m, f"找不到 function {name}"
    return m.group(0)


def _fixture_item_keys(fixture: dict, dotted: str) -> set:
    node = fixture
    for part in dotted.split("."):
        node = node[part]
    assert node, f"夹具 {dotted} 没有样例条目，字段守卫会变成空网"
    return set(node[0])


def test_mod_reads_only_contract_item_fields():
    """条目级与 llm / thresholds 的字段也受夹具约束（节点 1 T2：上一条只抓 snap.X 顶层）。"""
    src = (ROOT / "hooks" / "vault-mod.js").read_text(encoding="utf-8")
    fixture = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
    for fn, places in _ITEM_PARSERS.items():
        read = set(re.findall(r"\bx\.([a-z_]+)", _fn_body(src, fn)))
        assert "path" in read, fn                                  # 正则没失效
        allowed = set().union(*(_fixture_item_keys(fixture, p) for p in places))
        assert read <= allowed, (fn, sorted(read - allowed))
    for fn, key, var in (("parseLlm", "llm", "l"), ("parseThresholds", "thresholds", "t"),
                         ("parseSettings", "settings", "s")):
        read = set(re.findall(rf"\b{var}\.([a-z_]+)", _fn_body(src, fn)))
        assert read, fn
        assert read <= set(fixture[key]), (fn, sorted(read - set(fixture[key])))
    # 阳性对照：改一个字段名必须被抓到
    bad = _fn_body(src.replace("x.admit_arm", "x.admitArm"), "injectedItems")
    assert not set(re.findall(r"\bx\.([a-z_]+)", bad)) <= _fixture_item_keys(fixture, "injected")


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


def test_read_observer_skips_subagents_and_errors():
    """Read 观测只看结果、不改写 e；子 agent（带 agentId）与被拒的 Read（结果带 isError 或 deny）不计。

    kit 未必能在测试 `$` 上构造带 agentId 的 tool.call（Task 19 Step 2 的探针），这里钉语句形态兜底；
    行为面由 tests/mod/vault-mod.test.ts 的 Read 用例覆盖。
    """
    src = (ROOT / "hooks" / "vault-mod.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"  on\('tool\.call', \{ tool: 'Read' \}, async \(\$, e, next\) => \{\n(.*?)\n  \}\)\n",
                  src, re.S)
    assert m, "找不到 Read 观测 hook"
    body = m.group(1)
    assert re.search(r"^    const r = await next\(e\)$", body, re.M)
    assert re.search(r"^      if \(!e\.agentId && r && r\.isError !== true && typeof r\.deny !== 'string'\) "
                     r"await noteRead\(\$, e\.file_path\)$", body, re.M), body
    assert re.search(r"^    return r$", body, re.M)
    assert "next({" not in body                            # 不改写 e
