"""migrate_config.py 单测：dry-run 只读 / apply 清残留 / EXCLUDED 永不删 /
symlink 放弃 / restore 往返 / 历史默认表登记守卫（spec §8.2）。

fixtures 复用 conftest.py 的 tmp_home（隔离 HOME/USERPROFILE，Windows Path.home()
取 USERPROFILE）——脚本内 backup_dir_path()/default_config_path() 都经 Path.home()
派生，隔离后不会触碰真实机器上的 ~/.claude/skills/vault-loader/config.json。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import migrate_config as mc
from scripts._config_history import HISTORICAL_DEFAULTS
from scripts._config_loader import DEFAULT_CONFIG


def _config_path(home: Path) -> Path:
    return home / ".claude" / "skills" / "vault-loader" / "config.json"


def _write_config(home: Path, data: dict) -> Path:
    p = _config_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _backups(home: Path) -> list[Path]:
    bd = mc.backup_dir_path()
    if not bd.exists():
        return []
    return sorted(bd.glob("config-*.json"))


# ── test_dry_run_reports_residue_and_does_not_write ─────────────────────────

def test_dry_run_reports_residue_and_does_not_write(tmp_home: Path, capsys) -> None:
    p = _write_config(tmp_home, {
        # 5 是 session_start.max_notes 唯一历史默认值 → 应判残留
        "session_start": {"max_notes": 5},
        # 3 是 scoring.prompt_keyword_hit 的旧历史默认（e4a7462 引入，665cf63 改 5）→ 应判残留
        "scoring": {"prompt_keyword_hit": 3},
        # 0.7 不在 relevance.tag_idf_floor 任何历史默认集合中 → 不应判残留
        "relevance": {"tag_idf_floor": 0.7},
    })
    before = p.read_text(encoding="utf-8")

    rc = mc.main(["--path", str(p)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "session_start.max_notes" in out
    assert "scoring.prompt_keyword_hit" in out
    assert "relevance.tag_idf_floor" not in out

    # dry-run 严格只读：磁盘文件必须逐字节不变
    assert p.read_text(encoding="utf-8") == before
    # 无 --apply 时不得产生任何备份
    assert _backups(tmp_home) == []


def test_dry_run_no_residue_prints_clean_message(tmp_home: Path, capsys) -> None:
    p = _write_config(tmp_home, {"relevance": {"tag_idf_floor": 0.7}})

    rc = mc.main(["--path", str(p)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "relevance.tag_idf_floor" not in out
    assert _backups(tmp_home) == []


# ── test_apply_removes_residue_keeps_user_keys ──────────────────────────────

def test_apply_removes_residue_keeps_user_keys(tmp_home: Path) -> None:
    p = _write_config(tmp_home, {
        "session_start": {"max_notes": 5},    # 历史默认值，应判残留删除
        "relevance": {"tag_idf_floor": 0.7},  # 用户显式改过，应留
    })

    rc = mc.main(["--apply", "--path", str(p)])

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "max_notes" not in data.get("session_start", {}), "物化残留应被删除"
    assert data["relevance"]["tag_idf_floor"] == 0.7, "用户显式改动必须保留"

    # 备份已写出，且备份内容是删除前的原始盘上值
    backups = _backups(tmp_home)
    assert len(backups) == 1
    backed_up = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backed_up["session_start"]["max_notes"] == 5
    assert backed_up["relevance"]["tag_idf_floor"] == 0.7


def test_apply_no_residue_writes_no_backup(tmp_home: Path) -> None:
    p = _write_config(tmp_home, {"relevance": {"tag_idf_floor": 0.7}})
    before = p.read_text(encoding="utf-8")

    rc = mc.main(["--apply", "--path", str(p)])

    assert rc == 0
    assert p.read_text(encoding="utf-8") == before
    assert _backups(tmp_home) == []


# ── test_excluded_keys_never_removed ────────────────────────────────────────

def test_excluded_keys_never_removed(tmp_home: Path) -> None:
    p = _write_config(tmp_home, {
        # 逐字等于默认，但 vault_path 在 EXCLUDED_KEYS，任何情况下不删
        "vault_path": DEFAULT_CONFIG["vault_path"],
        "dry_run": DEFAULT_CONFIG["dry_run"],
        "opt_out_paths": DEFAULT_CONFIG["opt_out_paths"],
        "session_start": {
            "enabled": True,     # EXCLUDED（leaf 名命中），且本就非数值
            "max_notes": 5,      # 非豁免键，应正常判残留删除
        },
    })

    rc = mc.main(["--apply", "--path", str(p)])

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["vault_path"] == DEFAULT_CONFIG["vault_path"]
    assert data["dry_run"] == DEFAULT_CONFIG["dry_run"]
    assert data["opt_out_paths"] == DEFAULT_CONFIG["opt_out_paths"]
    assert data["session_start"]["enabled"] is True
    assert "max_notes" not in data["session_start"]


def test_is_allowlisted_excludes_leaf_name_at_any_depth() -> None:
    """直接单测 is_allowlisted：EXCLUDED_KEYS 必须按 leaf 名在任意路径深度排除，
    而不只是依赖"这些键在当前 DEFAULT_CONFIG 里恰好是非数值类型"这一巧合——
    当前 schema 里 session_start.enabled/user_prompt_submit.enabled 都是 bool，
    数值类型过滤本身就会跳过它们，若不单独验证 EXCLUDED_KEYS 分支，
    该分支被删掉也不会被现有 apply 类用例发现（假设未来加了同名数值键）。"""
    assert mc.is_allowlisted("scoring.prompt_keyword_hit") is True
    assert mc.is_allowlisted("relevance.tag_idf_floor") is True
    assert mc.is_allowlisted("session_start.enabled") is False
    # 假设场景（当前 schema 不存在，但防未来误加同名数值键回归）：
    assert mc.is_allowlisted("user_prompt_submit.dry_run") is False
    assert mc.is_allowlisted("scoring.vault_path") is False
    # 前缀本身不在 allowlist 内
    assert mc.is_allowlisted("vault_path") is False
    assert mc.is_allowlisted("display.verbosity") is False
    assert mc.is_allowlisted("telemetry.something") is False


def test_excluded_leaf_name_never_removed_even_if_numeric(tmp_home: Path) -> None:
    """belt-and-suspenders：构造一个当前 schema 不存在、但假设未来误加的数值型
    EXCLUDED_KEYS 同名键（scoring.enabled=1，与其历史默认值集合不相干），验证即使
    数值恰好等于某种"看似默认"的值，也绝不会被 find_residue 判为残留——这是对
    is_allowlisted 分支在完整 apply 流程中的端到端验证（而非只测该函数本身）。"""
    p = _write_config(tmp_home, {
        "scoring": {"enabled": 1, "prompt_keyword_hit": 3},
    })

    rc = mc.main(["--apply", "--path", str(p)])

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["scoring"]["enabled"] == 1, "EXCLUDED_KEYS 命中必须原样保留"
    assert "prompt_keyword_hit" not in data["scoring"], "非豁免键仍应正常判残留删除"


# ── test_symlink_aborts ──────────────────────────────────────────────────────

def test_symlink_aborts(tmp_home: Path) -> None:
    real = _write_config(tmp_home, {"session_start": {"max_notes": 5}})
    link = real.parent / "config-link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接（需管理员权限/开发者模式）")

    rc = mc.main(["--apply", "--path", str(link)])

    assert rc != 0, "symlink 目标必须整体放弃（非 0 退出码）"
    # 真实文件必须完全未被触碰
    data = json.loads(real.read_text(encoding="utf-8"))
    assert data["session_start"]["max_notes"] == 5
    assert _backups(tmp_home) == [], "symlink 放弃时不应产生备份"


# ── test_restore_roundtrip ───────────────────────────────────────────────────

def test_restore_roundtrip(tmp_home: Path) -> None:
    original = {
        "session_start": {"max_notes": 5},
        "relevance": {"tag_idf_floor": 0.9},
    }
    p = _write_config(tmp_home, original)

    rc = mc.main(["--apply", "--path", str(p)])
    assert rc == 0
    cleaned = json.loads(p.read_text(encoding="utf-8"))
    assert "max_notes" not in cleaned.get("session_start", {})

    backups = _backups(tmp_home)
    assert len(backups) == 1

    rc2 = mc.main(["--restore", str(backups[0]), "--path", str(p)])

    assert rc2 == 0
    restored = json.loads(p.read_text(encoding="utf-8"))
    assert restored == original


def test_restore_backs_up_current_config_before_overwriting(tmp_home: Path) -> None:
    """`--restore` 覆盖目标前必须先备份当前盘上内容——否则 `--apply` 之后用户手工做的
    调参会被静默抹掉且无任何恢复路径（`--apply` 有 write_backup，`--restore` 没有，
    与工具"每次写入都先备份"的安全叙事矛盾）。"""
    p = _write_config(tmp_home, {
        "session_start": {"max_notes": 5},
        "relevance": {"tag_idf_floor": 0.9},
    })

    rc = mc.main(["--apply", "--path", str(p)])
    assert rc == 0
    backups = _backups(tmp_home)
    assert len(backups) == 1
    backup = backups[0]

    # --apply 之后用户手工调参：改了已有值，并新增了一段此前没有的配置
    hand_edited = {
        "relevance": {"tag_idf_floor": 0.3},
        "user_prompt_submit": {"max_notes": 7},
    }
    p.write_text(json.dumps(hand_edited, ensure_ascii=False, indent=2), encoding="utf-8")

    rc2 = mc.main(["--restore", str(backup), "--path", str(p)])
    assert rc2 == 0

    backups_after = _backups(tmp_home)
    assert len(backups_after) == 2, "--restore 覆盖前必须先把当前 config 备份出去"
    newest = backups_after[-1]
    assert json.loads(newest.read_text(encoding="utf-8")) == hand_edited, (
        "最新备份必须是被覆盖掉的那份手工改动版，否则用户改动不可恢复"
    )


def test_flat_dotted_key_not_falsely_reported_as_deleted(tmp_home: Path, capsys) -> None:
    """扁平点键（`{"scoring.prompt_keyword_hit": 3}`）与嵌套路径
    （`{"scoring": {...}}`）在 iter_numeric_leaves 里产生**同一个**点分字符串，
    remove_path 按 split(".") 下钻只会删到嵌套那份、`pop(..., None)` 把"没删到"
    吞成静默成功 → 打印"已删除"但盘上原封不动。谎报是本用例要杀死的行为。"""
    p = _write_config(tmp_home, {
        "scoring.prompt_keyword_hit": 3,        # 扁平点键（用户笔误 / 旧工具产物）
        "scoring": {"prompt_summary_hit": 2},   # 真正的嵌套物化残留
    })

    rc = mc.main(["--apply", "--path", str(p)])

    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(p.read_text(encoding="utf-8"))

    reported_deleted = "已删除 scoring.prompt_keyword_hit" in out
    still_on_disk = "scoring.prompt_keyword_hit" in data
    assert not (reported_deleted and still_on_disk), (
        f"谎报：打印了已删除但盘上仍存在。输出={out!r} 盘上={data!r}"
    )
    # 防"整体跳过"式伪修复：真正的嵌套残留仍必须被正常清理
    assert "prompt_summary_hit" not in data.get("scoring", {}), (
        "嵌套残留仍应正常删除，不能靠整体跳过来消除谎报"
    )


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction 仅 Windows 存在")
def test_windows_junction_in_path_aborts(tmp_home: Path) -> None:
    """`Path.is_symlink()` ①只查末段 ②不识别 NTFS junction（reparse point），
    故"路径前缀里放一个 junction / 目录 symlink"可完全绕过越权写守卫。
    守卫必须走全路径解析，而不是只补一个 junction 特判。"""
    real_dir = tmp_home / "real-cfg-dir"
    real_dir.mkdir()
    real = real_dir / "config.json"
    real.write_text(
        json.dumps({"session_start": {"max_notes": 5}}, ensure_ascii=False),
        encoding="utf-8",
    )

    link_dir = tmp_home / "link-cfg-dir"
    # cmd.exe 内置命令输出是 GBK，不能加 text=True（解码会炸）
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_dir), str(real_dir)],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0 or not link_dir.exists():
        pytest.skip("当前环境无法创建 NTFS junction")

    via_junction = link_dir / "config.json"
    rc = mc.main(["--apply", "--path", str(via_junction)])

    assert rc != 0, "路径前缀含 junction 时必须整体放弃（非 0 退出码）"
    data = json.loads(real.read_text(encoding="utf-8"))
    assert data["session_start"]["max_notes"] == 5, "junction 背后的真实文件不得被写入"
    assert _backups(tmp_home) == [], "放弃时不应产生备份"


def test_restore_missing_backup_fails(tmp_home: Path) -> None:
    p = _write_config(tmp_home, {"session_start": {"max_notes": 5}})
    missing = tmp_home / "nope.json"

    rc = mc.main(["--restore", str(missing), "--path", str(p)])

    assert rc != 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["session_start"]["max_notes"] == 5, "还原失败不得触碰目标文件"


def test_bom_config_is_scanned_not_treated_as_corrupt(tmp_home: Path, capsys) -> None:
    """带 BOM 的 config（PowerShell 5.1 `Out-File -Encoding utf8` / 多数编辑器默认）
    必须能正常扫描，而不是被 `utf-8` 解码判成损坏、整份跳过（P4-3）。"""
    p = _config_path(tmp_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_start": {"max_notes": 5}}, ensure_ascii=False),
                 encoding="utf-8-sig")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf"), "前置条件：文件确实带 BOM"

    rc = mc.main(["--path", str(p)])

    assert rc == 0
    assert "session_start.max_notes" in capsys.readouterr().out, (
        "BOM 不得让整份 config 被判损坏、扫不出残留"
    )


def test_restore_rejects_non_schema_backup(tmp_home: Path) -> None:
    """`--restore` 是"任意 JSON → 任意路径写"：load_raw_config 只校验 root 是 dict，
    atomic_write_json 还会 mkdir(parents=True) 造出整条目录链。加顶层键白名单收口。"""
    p = _write_config(tmp_home, {"relevance": {"tag_idf_floor": 0.9}})
    before = p.read_text(encoding="utf-8")
    bogus = tmp_home / "not-a-config.json"
    bogus.write_text(json.dumps({"totally": "unrelated", "payload": [1, 2]}),
                     encoding="utf-8")

    rc = mc.main(["--restore", str(bogus), "--path", str(p)])

    assert rc != 0, "非本工具 schema 的 JSON 必须被拒绝"
    assert p.read_text(encoding="utf-8") == before, "拒绝时不得触碰目标文件"
    assert _backups(tmp_home) == [], "拒绝时不应产生备份"


def test_restore_force_allows_non_schema_backup(tmp_home: Path) -> None:
    """--force 是刻意覆盖场景的逃生阀：白名单拒绝可被显式放行，且仍会先备份当前值。"""
    p = _write_config(tmp_home, {"relevance": {"tag_idf_floor": 0.9}})
    bogus = tmp_home / "not-a-config.json"
    payload = {"totally": "unrelated"}
    bogus.write_text(json.dumps(payload), encoding="utf-8")

    rc = mc.main(["--restore", str(bogus), "--path", str(p), "--force"])

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8")) == payload
    backups = _backups(tmp_home)
    assert len(backups) == 1, "--force 也必须先备份被覆盖的内容"
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {
        "relevance": {"tag_idf_floor": 0.9}
    }


def test_apply_and_restore_are_mutually_exclusive(tmp_home: Path) -> None:
    """两者语义互斥（一个清残留、一个整份覆盖回滚），此前 --restore 静默胜出、
    同时传两个毫无提示。argparse 互斥组应直接报 usage 错误。"""
    p = _write_config(tmp_home, {"session_start": {"max_notes": 5}})
    before = p.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        mc.main(["--apply", "--restore", str(p), "--path", str(p)])

    assert excinfo.value.code != 0
    assert p.read_text(encoding="utf-8") == before, "usage 错误时不得改动任何文件"
    assert _backups(tmp_home) == []


# ── test_current_defaults_registered（防漂移守卫） ───────────────────────────

def test_current_defaults_registered() -> None:
    """防漂移守卫：DEFAULT_CONFIG 中每个 allowlist 数值键的当前值必须已登记在
    HISTORICAL_DEFAULTS——否则未来改默认忘记追加旧值快照，新默认永远无法被脚本
    识别为可清理的"历史默认"，本守卫在 CI 层面提前拦截该遗漏。"""
    checked = 0
    for path, value in mc.iter_numeric_leaves(DEFAULT_CONFIG):
        if not mc.is_allowlisted(path):
            continue
        checked += 1
        assert path in HISTORICAL_DEFAULTS, (
            f"{path} 是当前 allowlist 数值键，但未登记进 HISTORICAL_DEFAULTS"
        )
        assert value in HISTORICAL_DEFAULTS[path], (
            f"{path} 当前默认值 {value!r} 未出现在其历史默认集合 "
            f"{HISTORICAL_DEFAULTS[path]!r} 中（改默认时忘记追加旧值快照？）"
        )
    # 精确对齐（而非宽松下限）：当前 allowlist 数值键集合必须与历史表 key 集合
    # 完全一致——多于历史表说明有新键漏登记（已被上面逐键断言拦住），少于历史表
    # 则说明表里有过时/多余 key（比如键改名后旧 path 忘删），同样是漂移信号。
    assert checked == len(HISTORICAL_DEFAULTS), (
        f"当前 allowlist 数值键数={checked}，HISTORICAL_DEFAULTS 登记数="
        f"{len(HISTORICAL_DEFAULTS)}，两者必须精确一致（多则漏登记，少则表有过时残留）"
    )


def test_historical_defaults_keys_all_allowlisted() -> None:
    """反向核对：表里登记的每个 path 都必须真的落在 ALLOWLIST_PREFIXES 范围内、
    且不含 EXCLUDED_KEYS 命中的 segment（防手工登记表本身写错路径）。"""
    for path in HISTORICAL_DEFAULTS:
        assert mc.is_allowlisted(path), f"{path} 登记在历史表中但不属于 allowlist 范围"


# ── OBS-9 --doctor ─────────────────────────────────────────────────────────

def _doctor(capsys, path: Path) -> str:
    from scripts.migrate_config import main
    rc = main(["--doctor", "--path", str(path)])
    assert rc == 0
    return capsys.readouterr().out


def test_doctor_never_writes(tmp_path, capsys):
    """--doctor 必须纯只读。

    刻意不能用 _config_loader.load_config：它在文件缺失时 mkdir(parents=True) 并写占位，
    于是 `--doctor --path <任意路径>` 就成了「在任意位置创建目录树」，而 doctor 路径
    又没有重定向守卫。doctor 恰恰是最可能被模型经 Bash 工具代跑的命令。
    """
    missing = tmp_path / "deep" / "nested" / "config.json"
    out = _doctor(capsys, missing)
    assert not missing.exists(), "--doctor 写了 config"
    assert not missing.parent.exists(), "--doctor 创建了目录树"
    assert "不存在" in out


def test_doctor_reports_corrupt_config(tmp_path, capsys):
    bad = tmp_path / "config.json"
    bad.write_text('{"vault_path": "D:/V", }', encoding="utf-8")
    out = _doctor(capsys, bad)
    assert "解析失败" in out
    assert "回退默认值" in out
    assert bad.read_text(encoding="utf-8") == '{"vault_path": "D:/V", }', "损坏文件被改动"


def test_doctor_does_not_dump_user_paths(tmp_path, capsys):
    """输出会被贴进 issue、被模型读进 transcript——不得 dump 用户的项目代号与本机路径。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vault_path": str(tmp_path / "V"),
        "keyword_to_tags": {"绝密项目代号": ["tag-secret"]},
        "opt_out_paths": ["D:/private/client-work"],
    }), encoding="utf-8")
    out = _doctor(capsys, cfg)
    assert "绝密项目代号" not in out
    assert "tag-secret" not in out
    assert "client-work" not in out


def test_doctor_is_mutually_exclusive_with_writes(tmp_path):
    """--doctor 与两个写动作互斥；不进互斥组的话 `--doctor --apply` 会静默取其一。"""
    from scripts.migrate_config import main
    for other in (["--apply"], ["--restore", "x.json"]):
        with pytest.raises(SystemExit) as ei:
            main(["--doctor", "--path", str(tmp_path / "c.json")] + other)
        assert ei.value.code == 2


# ── R-4：--doctor 必须报告旧版残留 ─────────────────────────────────────────

def test_doctor_reports_residue(tmp_home, capsys):
    """存量用户最需要 doctor 回答的问题：我这台机器的 config 压着旧默认值吗？

    此前 doctor 对残留完全沉默，于是「装了新版但修复只生效一半」这个状态没有任何
    可自查的入口——召回变差不会报错，用户也无从知道自己的 prompt_keyword_hit 是 3
    而新装用户是 5。文档却把 --doctor 指定为排障入口。

    用 tmp_home 隔离：doctor 会读 HOME 下的 summarize-session config 做跨 skill
    比对，不隔离就会读到开发者的真实配置（T-7）。
    """
    cfg = tmp_home / "config.json"
    cfg.write_text(json.dumps({
        "vault_path": str(tmp_home / "V"),
        "scoring": {"prompt_keyword_hit": 3},      # 历史默认，属残留
        "relevance": {"tag_idf_floor": 0.5},       # 历史默认，属残留
    }), encoding="utf-8")

    out = _doctor(capsys, cfg)

    assert "旧版默认值残留" in out
    assert "2 项" in out, f"残留计数不对：\n{out}"
    assert "scoring.prompt_keyword_hit" in out
    assert "relevance.tag_idf_floor" in out
    assert "--apply" in out, "应指向清理方式"
    # 只报键名不报值：doctor 输出会被贴进 issue / 读进 transcript
    assert cfg.read_text(encoding="utf-8"), "config 不得被改动"


def test_doctor_reports_no_residue_when_clean(tmp_home, capsys):
    """反向：没有残留时必须明确说「无」，不能与「没检查」难以区分。"""
    cfg = tmp_home / "config.json"
    cfg.write_text(json.dumps({
        "vault_path": str(tmp_home / "V"),
        "scoring": {"prompt_keyword_hit": 999},   # 用户自定值，非历史默认
    }), encoding="utf-8")

    out = _doctor(capsys, cfg)
    assert "旧版默认值残留   : ✅ 无" in out, f"未给出明确的无残留结论：\n{out}"


def test_doctor_silent_on_residue_for_fresh_install(tmp_home, capsys):
    """零配置新装（config 不存在）不该出现残留那一行——没有 config 就没有残留，
    对新用户显示「✅ 无」也是噪声。"""
    out = _doctor(capsys, tmp_home / "nope" / "config.json")
    assert "旧版默认值残留" not in out
