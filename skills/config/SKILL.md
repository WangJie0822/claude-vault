---
name: config
description: 查看与修改 context-vault 插件自身的配置项（~/.context-vault/config.json）——列出全部可配项及其当前值、默认值与来源，解释某一项的作用与影响，确认后安全改写。仅限本插件的配置，不处理其他工具或项目的配置文件。当用户问「vault-loader 有哪些配置」「我改过哪些配置」「怎么关掉某个召回行为」「这个阈值是干什么的」时使用。
argument-hint: "[键名或关键词] [--diff] [--set <键> <值>]"
allowed-tools: Read, Bash(python3 *), Bash(ls *)
---

# context-vault 配置管理

把本插件的全部可配项连同「当前值 / 默认值 / 是否你改过 / 这一项是干什么的」一并列出，并在你确认后安全改写。

## 定位脚本

```bash
CFG=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/config/scripts 2>/dev/null | sort -V | tail -1)
```

优先用当前 SKILL.md 同级的 `scripts/`；取不到才用上面的 cache 搜索。

## 列清单

```bash
python3 "$CFG/config_inventory.py"              # 全部可配项
python3 "$CFG/config_inventory.py" --diff       # 只看「我显式改过的」
python3 "$CFG/config_inventory.py" 召回          # 按键名或说明里的词过滤
python3 "$CFG/config_inventory.py" --json       # 需要精确取值时用
```

需要按字段精确取值时**用 `--json`**，不要去解析人读输出——那是两种渲染，解析人读文本是会静默漂移的耦合。

输出里每项带这些标记，照实转述给用户，别省略：

| 标记 | 含义 |
|---|---|
| `已显式设置` | 用户自己写进 config.json 的；其余都在吃默认值 |
| `⚠️ 已废弃` | 运行时**不读**这个键，改它不会有任何效果 |
| `🔒 隐私旋钮（控制外发篇数）` | 改大等于每会话把更多笔记发往外部模型端点 |
| `外部键·只读` / `只读` | 本工具不写它，原因在说明里 |

## 解释某一项

用户问「这个是干什么的」时，先跑一次带关键词的清单拿到该项的说明、当前值与默认值，再回答。**不要凭记忆解释配置项**——说明文本随插件版本变化，清单里的才是当前这一版的。

## 修改

```bash
python3 "$CFG/config_set.py" <键> <值> --yes
```

流程（**顺序不能省**）：

1. 先列出该项当前值、默认值与说明，把「改完会发生什么」说清楚；
2. 拿到用户明确同意后才执行；
3. 回报写入结果与**备份路径**。

几类会被脚本直接拒绝，拒绝理由照实转述、不要绕过：

- **废弃键**——改了不生效，脚本会指出该用哪个键替代；
- **只读键**（`work_log_dir`、`obsidian_cli.*`）——它们不在 `DEFAULT_CONFIG` 里，没有默认值与类型判据可依；且 `obsidian_cli` 只认 canonical 配置文件，与本工具的路径解析可能分叉，写了会出现「读端显示已改、运行时不生效」；
- **隐私旋钮**（`relevance.session_topic_top_n` / `session_topic_project_top_k` / `session_topic_pool_max`）——控制每会话有多少篇笔记被发往外部模型端点。要改必须显式加 `--i-understand-privacy`，**并且先把这个含义告诉用户**；
- **类型不符**——按同键默认值的类型校验；注意布尔与整数不互通。

## 改错了怎么退回

每次覆盖写之前都会备份，写入结果里带路径：

```bash
python3 "$CFG/config_set.py" --restore <备份路径>
```

还原本身也会先备份当前值。

## 已知边界

- **只管 `config.json`。** 另外三处开关不在这里改：环境变量 `VAULT_LOADER_DISABLE=1`、文件 `~/.context-vault/.disabled`、项目 CLAUDE.md 里的 `<!-- vault-loader: dis​able -->` 注释。它们是**逃生阀**——用户打开它们往往正是因为插件出了问题，这时再让插件自己去改这些开关，失败方向不对称。要动请手工操作。
- **不提供「一键恢复默认」。** 它要么删键（用户的显式配置静默消失），要么写入当前默认值（此后默认值随版本演进对该用户永久失效）。要恢复某项，删掉 config.json 里对应那一行更安全。
- **列清单是只读的**，配置文件不存在时会如实报「未配置」而**不会创建**它。
