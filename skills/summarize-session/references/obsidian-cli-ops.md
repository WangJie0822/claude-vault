# obsidian_cli 封装层使用说明

## 调用方式

所有 Vault 内资源操作通过 scripts/obsidian_cli.py 调用：

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
python3 "$SS/obsidian_cli.py" \
    --vault "$VAULT" <op> [其他参数]
```

> ⚠️ `--vault` 是全局参数，必须放在子命令（op）之前。写成 `<op> --vault "$VAULT"` 会因 argparse 解析失败报 `the following arguments are required: --vault`。

op 列表：`probe` / `read` / `create` / `append` / `properties` / `property-read` / `property-set` / `search` / `files` / `reload`

stdout 统一输出 JSON：

```json
{
  "ok": true,
  "used": "cli" | "fallback",
  "data": {"...": "..."},
  "reason": "obsidian-not-running | cli-timeout | ...",
  "degraded_counts": {"cli-timeout": 1}
}
```

退出码：`ok=true` → 0；`ok=false` → 1。

## 命令映射表

| 封装 op | Obsidian CLI | 降级方案 |
|---|---|---|
| read | `obsidian read path=...` | 文件读取 |
| create | `obsidian create path=... content=...` | `Path.write_text` |
| append | `obsidian append path=... content=...` | 文件以 `a` 模式追加 |
| properties | `obsidian properties path=... format=json` | 内置 frontmatter parser |
| property-read | `obsidian property:read path=... name=...` | parser + dict get |
| property-set | `obsidian property:set path=... name=... value=...` | parser + 行级回写 frontmatter |
| search | `obsidian search query=... format=json` | `rglob("*.md")` + 子串扫描 |
| files | `obsidian files ext=... folder=... format=json` | `rglob` 过滤 |
| reload | `obsidian reload` | 无操作（返回 noop fallback） |

## 降级矩阵

| 触发条件 | reason 取值 |
|---|---|
| `pgrep -x Obsidian` 未命中 | `obsidian-not-running` |
| `command -v obsidian` 未命中 | `cli-not-registered` |
| pgrep 超时（>5s） | `pgrep-timeout` |
| cli 探测超时（>5s） | `cli-probe-timeout` |
| cli 调用超时 | `cli-timeout` |
| cli 退出码非 0 | `cli-nonzero`（stderr 带入 reason payload） |
| CLI stdout JSON 解析失败 | `parse-fail` |
| 多 Vault 未找到目标 | `vault-not-open-in-obsidian` |

## 并发安全

- **CLI 路径**：Obsidian 主进程单线程串行处理，天然安全
- **降级路径**：调用方负责 Read → Edit 模式（参见 SKILL.md「并发安全」段）

## 常见问题

1. **pgrep 在 macOS 无输出**：确认 Obsidian.app 进程名为 `Obsidian`（shell: `pgrep -lx Obsidian`）
2. **`command -v obsidian` 找不到**：检查 `~/.zprofile` 是否含 `export PATH=...Obsidian.app/Contents/MacOS`；GUI 内 Settings → General → Command line interface 需点击 Register CLI
3. **多窗口并发**：CLI 路径天然安全；fallback 路径下工作日志/笔记追加冲突时 Edit 匹配失败，调用方重试
