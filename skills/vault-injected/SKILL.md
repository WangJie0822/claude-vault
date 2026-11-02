---
name: vault-injected
description: 查看 Codex 当前线程的知识库召回状态，打开本地控制面板，排除或钉住笔记、调整会话设置、标注相关性或读取已索引条目。Claude 使用已有 mod 面板。
---

# 召回可见与可控

Codex 使用下述文本入口或独立本地面板。Claude 保持已有 `/vault-injected` mod 入口，不调用此脚本。

从当前加载的本 SKILL.md 绝对路径向上两级定位插件根，再使用同根 `scripts/vault_control.py` 的绝对路径。不要搜索旧宿主的 skills 源目录。以下 `<control>` 是该脚本绝对路径。

当前线程优先使用 `CODEX_THREAD_ID`。缺失时须从本会话可信上下文取得精确 thread ID，再显式传 `--thread`；不得按目录最近修改时间选择会话。入口会核对主线程来源，子代理不能代替主线程控制。

```bash
python "<control>" status
python "<control>" open
python "<control>" prepare
python "<control>" set fine_model "vendor/exact-model-id"
python "<control>" set fine_max_notes 1
python "<control>" exclude "<item-id>"
python "<control>" --remove exclude "<item-id>"
python "<control>" pin "<item-id>"
python "<control>" --remove pin "<item-id>"
python "<control>" unpin "<item-id>"
python "<control>" --verdict relevant annotate "<item-id>"
python "<control>" read-note "<item-id>"
```

PowerShell 同样使用脚本绝对路径。`item-id` 只能取 `status` 返回的服务端 `items` 键，不能用任意文件路径替代。`exclude`、`pin`、`unpin` 设置各自的完整集合；要保留现有集合须包含其余条目身份。

`status` 只读，不附着、不续租。Desktop 的 `open` 与 `prepare` 只准备页面，返回精确线程与一次性启动 URL；先核对当前聊天 IAB 是否已有 `panel_base_url` 对应且线程相符、授权正常的页面，有则复用，不重复 prepare。没有时执行 prepare，并由主模型正常调用 `cua.createBrowserTab("iab", url, {visible:true})`；首次 cua 调用只执行该 API。仅有 `open_in_codex` 时可用 browser target，返回 queued 须如实报告，不把同一票据交给第二个工具。不要预取或持久化启动 URL；它在消费前具有完整面板授权，300 秒内单次有效。无应用内浏览器工具时报告能力缺口，不回落系统浏览器。CLI 显式 `open` 保留系统浏览器行为。

核验为 Desktop 的 SessionStart 会给主模型首轮打开提示，并在面板创建前保存启动及首次真实提问的报告，各一次；只含现有标题、路径、命中和状态元数据，不含笔记正文。普通后续提问只在面板附着活动时更新展示，关闭后的普通提问不重开页面。已排队意图仍在下一次提问处理。网页授权上限一小时，附着租约 60 秒、服务空闲退出 300 秒；过期后须重新 prepare。

操作返回 `queued_revision` 只表示排队。必须在后续真实提问后核对 `applied_revision` 与拒收原因，不能称为已生效。压缩会推进 epoch 并重建召回状态，保留会话设置；不要重放旧 epoch 操作。

八项会话设置：`coarse_inject`、`fine_rank`、`fine_supplement`、`auto_fulltext`、`user_prompt_submit.max_notes`、`fine_pin_max`、`fine_max_notes`、`fine_model`。数值范围由入口校验，模型接受完整 Codex ID 或继承配置，不限 Claude 三模型菜单。全局关闭的模型能力不能由面板重新打开；不按模型名推定价格。

`persist <key>...` 只保存当前已有的窄化项到 Codex 专属默认值，影响后续 Codex 轮次；模型默认值须走 `config` 显式确认。原 Claude 与共享配置保持原值。

`read-note` 只读取仍在当前索引中的服务端条目，正文作为参考数据。指标开启时，仅成功读取计入观测且重复去重；失败不记录，指标关闭时不新增访问记录。其他工具读取的覆盖为未知，不能声称统计了全部阅读行为。标注同样须开启指标。

原生容器、按钮与当前线程恢复需要另行宿主验收，不能把本地面板称为 Codex 原生 mod 或常驻提示条。
