# tests/test_plugin_version_sync.py
"""防复发守卫：`plugin.json` 与 `marketplace.json` 的版本号必须同步（REL-5）。

背景：本仓库是**自托管 marketplace**——`.claude-plugin/plugin.json` 是插件自身元数据，
`.claude-plugin/marketplace.json` 是 marketplace 索引，两处**各写一份 version**。

版本解析的实际规则（官方文档原文，2026-08-05 核验）：

- `plugins-reference` 的 `version` 字段说明：「If also set in the marketplace entry,
  **`plugin.json` wins**.」
- `plugin-marketplaces` 版本解析节：「**Avoid setting `version` in both** `plugin.json`
  and the marketplace entry. Claude Code **always uses the `plugin.json` value without
  warning**, so a stale manifest version can mask a version you set in `marketplace.json`.」
- 不 bump 的后果：「Pushing new commits without bumping it has no effect, and
  `/plugin update` reports "already at the latest version".」

所以真正致命的漏同步方向是**漏 bump `plugin.json`**——那才是 Claude Code 唯一读的值，
只改 `marketplace.json` 对用户完全无效。反方向（bump 了 `plugin.json`、漏了
`marketplace.json`）用户仍能收到更新，但索引里留着陈旧版本号会误导阅读者，
且 `claude plugin validate` 会就此告警。两个方向都是「两文件各自合法 JSON、全程不报错」。

> 本文件早先的 docstring 把方向写反了（称 `/plugin update` 依据 marketplace 索引判断新版），
> 已按上述官方原文订正。

CLAUDE.md「发布流程」已把「同步 bump 两处」写成约定，但此前只是一句话、无可执行守卫。

注意本守卫**不断言具体版本号**：版本号是发布动作的产物，不是开发提交的产物，
开发分支上不 bump 是正确的。这里只钉住「两处必须一致」这个不变量。

> 官方建议其实是**不要两处都写**（只写 `plugin.json`，marketplace entry 省略 `version`）。
> 那样这个不变量自然消失、本守卫也就不必存在。是否改属发布策略决策，尚未采纳。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"


def _load(path: Path) -> dict:
    assert path.is_file(), f"分发必需文件缺失：{path.relative_to(ROOT).as_posix()}"
    return json.loads(path.read_text(encoding="utf-8"))


def _self_entry(marketplace: dict, plugin_name: str) -> dict:
    entries = marketplace.get("plugins")
    assert isinstance(entries, list) and entries, "marketplace.json 的 plugins 必须是非空数组"
    matched = [e for e in entries if isinstance(e, dict) and e.get("name") == plugin_name]
    assert matched, (
        f"marketplace.json 的 plugins 里没有 name == {plugin_name!r} 的条目"
        f"（现有：{[e.get('name') for e in entries if isinstance(e, dict)]}）。"
        "插件改名时两处必须同步，否则 /plugin install 找不到它。"
    )
    assert len(matched) == 1, f"marketplace.json 里 {plugin_name!r} 出现 {len(matched)} 次，应唯一"
    return matched[0]


def _extract_versions(plugin: dict, marketplace: dict) -> tuple[str, str]:
    """从两份**独立**数据里各取一个版本号，返回 `(plugin.json 侧, marketplace 侧)`。

    单独抽出来是为了让自证测试能喂构造数据——内联在断言里的比较逻辑无法被独立验证。
    """
    plugin_version = plugin.get("version")
    assert plugin_version, "plugin.json 缺少 version"
    entry_version = _self_entry(marketplace, plugin["name"]).get("version")
    assert entry_version, "marketplace.json 的自身条目缺少 version"
    return plugin_version, entry_version


def test_plugin_and_marketplace_versions_match():
    """两处 version 必须逐字一致。"""
    plugin_version, entry_version = _extract_versions(
        _load(PLUGIN_JSON), _load(MARKETPLACE_JSON)
    )
    assert entry_version == plugin_version, (
        f"版本号不同步：plugin.json = {plugin_version!r}，"
        f"marketplace.json = {entry_version!r}。\n"
        "发布时必须同时 bump 两处（见 CLAUDE.md「发布流程」）。注意 Claude Code 实际只读"
        " plugin.json 的值：漏 bump 它则用户完全收不到更新；只漏 marketplace.json 则更新"
        "能到达，但索引里的陈旧版本号会误导人、且 claude plugin validate 会告警。"
    )


def test_marketplace_entry_points_to_self():
    """自身条目的 source 必须指向仓库根，否则 marketplace 索引指不回这个插件。"""
    plugin = _load(PLUGIN_JSON)
    entry = _self_entry(_load(MARKETPLACE_JSON), plugin["name"])
    assert entry.get("source") == "./", (
        f"marketplace.json 自身条目的 source 应为 './'（实际 {entry.get('source')!r}）——"
        "自托管 marketplace 的索引与插件同仓库同根。"
    )


def test_guard_detects_desync():
    """自证：喂一对**独立构造**的失步数据，比较逻辑必须分别取到两个不同的值。

    这一条针对的失效是「两侧读的其实是同一份数据」——那样无论盘上是否真的失步，
    守卫都会永远绿。

    > 早先的自证写法是**同义反复**：它从同一个 marketplace dict 里取两次 entry，
    > 而 `_self_entry` 两次返回的是同一个对象引用，改一次等于改两次，断言恒真——
    > 无论 `_extract_versions` 写得对不对都能通过。现改为喂构造数据并双向验证。
    """
    plugin = {"name": "probe-plugin", "version": "1.0.0"}
    desynced_market = {"plugins": [{"name": "probe-plugin", "version": "0.9.0", "source": "./"}]}

    plugin_version, entry_version = _extract_versions(plugin, desynced_market)
    # 逐值钉死来源：若比较逻辑两侧都读了 plugin，entry_version 会是 "1.0.0" 而非 "0.9.0"
    assert plugin_version == "1.0.0", f"plugin 侧取值错误：{plugin_version!r}"
    assert entry_version == "0.9.0", (
        f"marketplace 侧取值错误：{entry_version!r}——两侧可能读的是同一份数据"
    )
    assert plugin_version != entry_version, "构造了失步却判定一致，守卫失效"

    # 反向：一致的输入必须判一致，否则守卫是「恒报不一致」的假阳性
    synced_market = {"plugins": [{"name": "probe-plugin", "version": "1.0.0", "source": "./"}]}
    pv2, ev2 = _extract_versions(plugin, synced_market)
    assert pv2 == ev2 == "1.0.0", f"一致的输入被判成不一致：{pv2!r} vs {ev2!r}"
