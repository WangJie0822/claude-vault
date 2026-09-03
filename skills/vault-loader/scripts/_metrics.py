# -*- coding: utf-8 -*-
"""决策面指标落盘。只记录 transcript 原理上拿不到的字段。

**每会话独立文件**是正确性前提，不是风格选择：Windows 上多进程追加同一文件
实测丢 5%~38% 记录（`open(a)` 与 `os.open(O_APPEND)` 皆然，后者大载荷还撕裂），
而丢失的事件看起来恰好等于「vault-loader 那次没触发」——指标系统的数据损坏
会被误读成它要检测的失效。按 session 分文件后实测 160/160 零丢失。

本模块所有函数必须可被调用方用 try/except 完整兜住；模块顶层零 IO。

模块顶层从 `context_vault` 导入 `lease_lock` / 路径工具，但**配了 ImportError
façade**（与 `_state.py` / `_config_loader.py` 同款），所以 legacy 独立布局或
3.10 及以下解释器上不会整体 import 失败。此处曾写「刻意不依赖任何内部模块」，
在双运行时改造引入这两个导入后已不成立——注释与实现不符比没有注释更误导。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections import Counter
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# 只有**确实像插件根**才插入 sys.path。legacy 独立布局
# （`~/.claude/skills/<skill>/scripts/`）下 parents[3] 正是 `~/.claude` 本身——
# 把一个多插件共享、可被任意工具写入的目录放到 sys.path[0]，等于让任何能在那里
# 落一个 `context_vault/__init__.py` 的东西在每次 hook 进程内取得代码执行。
# 判据不成立时跳过，交给下面的 ImportError façade 兜底。
_LOOKS_LIKE_PLUGIN_ROOT = (_PLUGIN_ROOT / "context_vault" / "runtime.py").is_file()
if _LOOKS_LIKE_PLUGIN_ROOT and str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:
    from context_vault.atomic import lease_lock
    from context_vault.paths import canonical_config, context_home, use_canonical_namespace
except ImportError:  # compatibility façade for isolated legacy script copies
    # 与 `_state.py` / `_config_loader.py` 同款兜底。缺了它，legacy 独立布局或
    # 3.10 及以下解释器（`context_vault.coexist` 用了 3.11 的 tomllib）会让本模块
    # 整体 import 失败，于是每次 UserPromptSubmit 都多打一行 stderr——而 metrics
    # 默认是关的，用户为一个自己没开的功能持续付噪声。
    from contextlib import contextmanager

    def context_home(home: Path | None = None) -> Path:
        return (home or Path.home()) / ".context-vault"

    def canonical_config(home: Path | None = None) -> Path:
        return context_home(home) / "config.json"

    def use_canonical_namespace(home: Path | None = None) -> bool:
        # façade 降级：拿不到 context_vault 时保守走 legacy 布局，
        # 绝不把既有用户的数据切到一个空命名空间。
        return False

    @contextmanager
    def lease_lock(target: Path, *, timeout: float = 2.0, stale_after: float = 30.0):
        # 降级为无锁：本 façade 只在拿不到 context_vault 时生效，此时同进程内的
        # 顺序写仍然正确，跨进程并发写退化为「后写者胜」。metrics 是 opt-in 的
        # 观测数据，丢一条记录远好过让整条召回链路多一行报错。
        yield

if TYPE_CHECKING:                      # 仅供类型标注，运行期不导入（L-PY1）
    # 本模块被每次 UserPromptSubmit 无条件 import。对 `context_vault` 的依赖走上面
    # 的 façade 兜底，故 import 失败不会波及调用方。放 TYPE_CHECKING 里既补上标注，
    # 又不引入运行期依赖——`from __future__ import annotations` 已让注解全部惰性求值。
    from collections.abc import Collection

    from scripts._decision import Decision      # 与 prompt_submit_load.py 同一写法

# schema 演进约定（2026-08-17 确立）：**只有加性可选字段可以不 bump 本常量**
# ——读端一律 `.get(key, 默认)`，新旧双向兼容。任何删除 / 重命名 / 语义变更
# **必须** bump，并同步给 `analyze_metrics.load_records` 加一条「丢弃了 N 条旧版本
# 记录」的 stderr 提示：它现在用严格相等过滤（analyze_metrics.py:68）、且 skipped
# 计数器只统计 JSON 解析失败，bump 会让既有记录**静默消失**（实测本机已积累 259 条）。
SCHEMA = 1
RUNTIME_SCHEMA = 2
_RUNTIME = "legacy"
_EVENT_ID = ""
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")

# score-low 样本的 topical 下限。`min_topical_score` 默认 4，本值取其 75%。
#
# **它是生成侧判据**——`build_record` 落盘时就施加，不再只作用于 nudge 计数。
# `near_miss_scorelow` 里的条目按定义 topical < 4，但**下界是 0**：真实数据实测该批
# topical 中位数只有 2.0、9.5% 恰为 0。而三个消费者的文案都是「反复接近召回闸门」
# 「调 tags/keywords 可能救回来」，对一篇 topical=0 的笔记这些全是错的指引
# （它不是差一点，是压根不相关）。
#
# 下沉到生成侧而非只在消费侧过滤，三个理由：
#   1. 消费侧过滤靠「三处都记得调同一个函数」维系，而这**已经漏过一次**——上一版把
#      判据写进 `scorelow_paths` 并在四处文档声明「三个消费者一律走单点」，实际只有
#      `flush` 在调，`summarize` 与 `sample_near_miss` 各自内联读裸键、都没施加下限。
#      生成侧施加后磁盘上不存在低于本值的条目，判据无从分叉。
#   2. 落盘量：分层场景下 `near_miss` 与 `near_miss_scorelow` 交集为 0，单条记录暴露的
#      excluded 路径精确翻倍（实测 10 → 20）。施加下限后降到 13。
#   3. 少一个把磁盘数据喂进 `float()` 的消费点（该调用链会抛 ValueError/TypeError）。
#
# **代价（刻意记下，不粉饰）**：低于本值的条目**永久不落盘**，将来想调低 floor、
# 或分析低分条目的分布，都没有历史数据可回溯。缓解手段是把生效值一并落盘为
# `scorelow_floor`（与 `near_miss_k`/`admitted_k` 同族的自描述字段），至少让
# 「这批记录当时用的什么阈值」可查——但它换不回已经没落盘的那些条目。
#
# 定量（真实数据）：下限取 3 时达阈值的 path 由 51 → 12；取 2 无效果（1812 条几乎全留）。
NUDGE_TOPICAL_FLOOR = 3.0


def configure_context(runtime: str, event_id: str = "") -> None:
    """选择本次 hook 进程的 metrics 命名空间。

    ⚠️ **`"legacy"` 必须被显式接受**：`analyze_metrics.py` 在 canonical config 不存在
    时（即升级当天的全部存量用户）默认传的就是这个字符串。此前它落进 else 分支、
    又不在白名单里，于是被映射成 `"unknown"` 并指向 `~/.context-vault/metrics/unknown`
    ——一个空目录。后果是 `--report` 报「无数据」、`--review` 抽不到条目、`--purge`
    打印「已清空 0 个数据文件」而真实数据原样留在盘上，三者都与「本来就没有数据」
    完全不可区分。

    未知取值一律回落 `"legacy"` 而不是 `"unknown"`：后者会凭空造出一个谁也不会去看
    的孤儿命名空间，把数据写进去等于丢掉。
    """
    global _RUNTIME, _EVENT_ID
    # Existing Claude users stay on the 0.9.x metrics layout until they create
    # or migrate canonical config. Codex always needs an explicit namespace.
    if runtime in {"legacy", "unknown"} or (
            runtime == "claude" and not use_canonical_namespace()):
        _RUNTIME = "legacy"
    else:
        _RUNTIME = runtime if runtime in {"claude", "codex"} else "legacy"
    _EVENT_ID = event_id


def current_schema() -> int:
    return RUNTIME_SCHEMA if _RUNTIME != "legacy" else SCHEMA


def runtime_record_fields() -> dict:
    if _RUNTIME == "legacy":
        return {}
    return {"runtime": _RUNTIME, "event_id": _EVENT_ID}


def metrics_dir(home: Path) -> Path:
    if _RUNTIME != "legacy":
        return context_home(home) / "metrics" / _RUNTIME
    return home / ".claude" / "vault-loader-metrics"


def event_month_dirs(home: Path) -> list[Path]:
    """按月分桶的真实事件目录（升序路径名），供 `load_records`/`prune_expired` 共用。

    **判据用目录名形态**（`\\d{4}-\\d{2}`，与 `write_record` 的落盘规则同源），
    **不是**「排除已知顶层文件名」的黑名单。顶层目前已有 `annotations.jsonl`
    （人工标注）、`near_miss_counts.json`、`nudge_ts.json`、`.salt` 四个辅助文件——
    黑名单式实现要求每新增一个就同步改一遍，是下一个同类缺陷（H1：`analyze_metrics.
    load_records` 曾用 `root.rglob("*.jsonl")` 把 `annotations.jsonl` 当成事件文件
    一起统计进报表）的温床。月份目录本身没有更深层级（`write_record` 直接把
    `<session>.jsonl` 放在月份目录下），故调用方只需 `d.glob("*.jsonl")`。
    """
    root = metrics_dir(home)
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and _MONTH_RE.fullmatch(d.name))


def _chmod(p: Path, mode: int) -> None:
    try:
        # ⚠️ Windows 上**这道保护根本不存在**，不只是「无实际效果但无害」——
        # 「无害」只在「不会弄坏别的东西」这一层成立，它掩盖的是：`.salt` 的保密是
        # 加盐 hash 的安全前提，而 NTFS 走 ACL、`os.chmod` 对它基本无效，该文件的
        # 实际可读范围完全取决于 `~/.claude` 继承下来的 ACL。若某个组被授过读权限
        # （沙箱工具、备份服务常这么做），它就能同时拿到 salt 与 kw_h，对有限的
        # 关键词空间做字典攻击、还原出 prompt 关键词 —— 那是设计里唯一声明
        # 「绝不可恢复」的资产。README 已按平台如实标注，不再承诺「该文件须保密」
        # 这种实现兑现不了的话。仍然调用是因为 POSIX 上它是真实有效的。
        os.chmod(p, mode)
    except OSError:
        pass


def get_salt(home: Path) -> bytes:
    """每机随机盐。用于 cwd 与关键词的不可逆化。

    **创建必须原子（O_CREAT|O_EXCL），不能写成 `if exists(): read` 再 `write`。**
    那两步之间毫无保护：首次运行时多个 hook 进程并发到达，各自生成不同 salt，
    最后写入者获胜落盘——而先到者已用它那份（此刻已与磁盘不一致的）salt 算完
    kw_h/cwd_h 并落盘，这些 hash 与后续记录**永久对不齐，且零异常零告警**。
    自然并发窗口极窄（实测 120 次首次调用未复现，解释器启动开销天然错开），
    但人为拉宽窗口 100% 复现；多 session 共享同一 ~/.claude 时并非纯理论场景。

    两条降级路径的已知代价，**刻意接受，不要"顺手修好"**：
    1. `.salt` 存在但内容不合法（<16 字节）时**永不自愈**——两轮 O_EXCL 都会
       FileExistsError，从不真正写入，于是每次调用各自生成不同的临时盐，
       该机器的 hash 从此永久互不一致。自愈需要删除并重建，那等于无条件信任
       一个可能正被别的进程使用的文件，风险更大；正解是让用户跑 `--purge`。
    2. `d.mkdir(...)` 在重试循环**之外**，不在任何 try 保护内——mkdir 失败会直接
       抛出，而非降级到临时盐。这与本函数"绝不抛异常"的自我描述不一致，但不影响
       系统级 fail-open：调用方（Task 7 的 stage 块）本身包在 try/except 里。
    """
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, 0o700)
    p = d / ".salt"

    for _ in range(2):          # 至多重试一次：独占创建失败必然因为已存在
        if p.exists():
            try:
                raw = p.read_bytes()
                if len(raw) >= 16:
                    return raw
            except OSError:
                pass
        try:
            # O_BINARY 不可省：Windows 上 os.open 默认文本模式，CRT 会把写入的
            # 0x0A 静默翻译成 0x0D 0x0A。salt 是均匀随机 32 字节，含至少一个
            # 0x0A 的概率 = 1-(255/256)^32 ≈ 11.8%（本机 200 次实测 12.5%）。
            # 一旦触发：os.write 仍返回 32（返回值不反映改写），但落盘变 33+ 字节；
            # 首次调用 return 的是内存值（正确），之后**所有**调用读到的都是损坏值，
            # hash 与首次永久对不上且零异常零告警；`len(raw) >= 16` 也拦不住
            # （实测 32 个 0x0A 落盘 64 字节）。POSIX 无此常量，用 getattr 兜。
            # 注：旧实现 p.write_bytes() 走 pathlib 二进制模式，没有这个问题。
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                         | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue            # 别的进程抢先建好了，回头读它那份
        except OSError:
            break               # 目录不可写等，落到下面的临时盐
        try:
            raw = secrets.token_bytes(32)
            os.write(fd, raw)
        finally:
            os.close(fd)
        # 不再补 _chmod：os.open 的 mode=0o600 已经够——Windows 上 chmod 本就无效，
        # POSIX 上 umask 只会遮 group/other 位，典型 umask 不碰 owner 位。
        return raw

    # .salt 存在但内容不合法且无法独占重建。宁可本轮用临时盐（与历史对不齐）
    # 也不抛异常——hook 必须 fail-open。
    return secrets.token_bytes(32)


def h(text: str, salt: bytes) -> str:
    """加盐单向摘要，取前 16 位十六进制。

    **不得复用 _state._cwd_hash**：那是无盐 SHA-1，对可枚举的短路径不构成匿名化
    （它只是用来生成文件名，不是隐私控制）。
    """
    return hashlib.sha256(salt + text.encode("utf-8")).hexdigest()[:16]


def write_record(home: Path, session_id: str, record: dict) -> Path:
    """追加一行 JSON 到该会话的独立文件。

    **每会话独立文件缩小了并发损坏面，但没有消除它。** 实测：
      - 跨 session 并发（4 进程各写各的文件，160 条）：零丢失，守恒。
      - **同一 session** 被 6 个进程并发写同一文件（预期 240 条）：三轮实测只落
        166 / 166 / 158 条，丢失 31%~34%，并出现撕裂行（样例 `"_schema": 1, "i": 1}`
        —— 开头的 `{` 没了）。量级与「朴素混写丢 5%~38%」相同。

    刻意不加锁：`msvcrt.locking` 是平台特定的，违反三平台兼容约束；而 metrics 是
    侧信道，写失败已被 `flush()` 的 try/except 兜住，不影响召回与 stdout。
    正常交互下同一 session 的 UPS hook 是串行的（Claude Code 等 hook 返回才继续），
    现实触发路径只有「同一 session 被 resume 到两个终端」这类罕见情形。
    代价由读取端承担：`analyze_metrics.load_records` 计数损坏行并经 stderr 报出，
    不静默——见该函数 docstring。
    """
    safe = _SAFE.sub("_", session_id or "unknown")[:64] or "unknown"
    month = time.strftime("%Y-%m")
    d = metrics_dir(home) / month
    d.mkdir(parents=True, exist_ok=True)
    _chmod(metrics_dir(home), 0o700)
    p = d / f"{safe}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
    _chmod(p, 0o600)
    return p


def _dedup_counts(excluded) -> dict:
    """全部 excluded 条目按 dedup 成因计数。

    **必须遍历全量，不能复用 `near_miss`**：后者是按 topical 降序取 top-k 的截断
    样本（实测采样率 1.38%、100% 的轮次都在截断），而两类条目的 topical 有结构性
    差异——dedup 条目不看 topical 就被排除（可达 11），新篇被排除的条件恰恰是
    `t < min_topical`（必 < 4）。于是按 topical 降序时 dedup 天然占满窗口，窗口内
    占比（实测 45%）与真实占比（量级约 1%）相差一个数量级以上。

    成本：一次 O(len(excluded)) 的计数，不调 score()/_hit_keywords()，
    与 `n_excluded` 已有的 len() 同量级，不触碰 UPS 的耗时预算。
    """
    counts: dict[str, int] = {}
    for ed in excluded:
        k = getattr(ed, "dedup", "") or ""
        counts[k] = counts.get(k, 0) + 1
    return counts


def build_session_start_record(*, session_id: str, inj_chars: int,
                               n_notes: int, n_worklogs: int,
                               n_commits: int) -> dict:
    """SessionStart 通道的极简记录。纯计算、无 IO。

    **只记规模与开销，不记笔记路径**：这条通道要回答的是「它花了多少、值不值」，
    答那个不需要知道注入了哪几篇；不落路径则隐私增量为 0。

    `channel` 是读端的隔离判据。缺该键的旧记录一律按 UPS 处理（向后兼容）。
    隔离是硬要求：SS 记录的 `gate` 为空，一旦被当成 UPS 记录，会被计入 `n_ok`，
    同时稀释「走到打分」占比、全文注入率与候选池均值 —— 修一个缺口却弄坏三个
    已有指标。

    为什么这条通道值得落盘：此前 `session_start_load.py` 对 `_metrics` 的引用数是
    **0**，报表只有一句免责「不含 SessionStart 通道，它不落 metrics」，而免责不是
    数据。从 transcript 侧直接量，557 个会话检出 SessionStart 注入、合计 642,439
    字符（中位 724、p90 2091）—— 这笔开销在「值不值」的账上整个缺席。
    """
    rec = {
        "_schema": current_schema(),
        "ts": round(time.time(), 3),
        "session": session_id or "",
        "channel": "session_start",
        "inj_chars": int(inj_chars),
        "n_notes": int(n_notes),
        "n_worklogs": int(n_worklogs),
        "n_commits": int(n_commits),
    }
    if _RUNTIME != "legacy":
        rec["runtime"] = _RUNTIME
    return rec


def build_record(decision: "Decision", prompt_keywords: "Collection[str] | None",
                 cwd: Path, *, session_id: str, prompt_id: str, salt: bytes,
                 src: str, prompt: str = "",
                 near_miss_k: int = 10, admitted_k: int = 20,
                 max_notes: int = 3,
                 min_topical: float | None = None,
                 ft_topical: float | None = None) -> dict:
    """把一次 UPS 决策压成一条可落盘记录。纯计算、无 IO。

    隐私边界（不可协商，见 task-6-brief.md）：
    - prompt 全量关键词只存加盐 hash（kw_h）——`_signal_collect.py` 的 CJK 分词是
      重叠滑窗，相邻 bigram 首尾共享一字，可沿重叠链还原原文（实测 25/26 字还原）。
    - 仅 admitted 条目的命中词（hits）存明文——这些词已经是"展示给用户的内容"的
      一部分，不构成额外泄露；且是回放归因"为什么这篇被召回"的必需信息。
    - prompt 原文本身绝不出现在任何字段。
    - excluded 条目只记 path + topical（near_miss），不记 total/hits：两者在
      `_decision.py` 里是未计算的占位值（0.0 / []），补算在真实 Vault 上实测
      +150~200ms，会顶穿 UPS 300ms 预算（见 EntryDecision 上方性能护栏注释）。
    - cwd 只存 hash（cwd_h），不存原路径。

    **`session_id`/`prompt_id` 强制 keyword-only（`*` 分隔）**：二者是相邻的
    同类型字符串位置参数，调用点一旦按位置对调会静默错位——`flush()` 用
    `rec["session"]` 决定落盘**文件名**，对调后即变成「按 prompt_id 命名」，
    架空「每会话独立 `.jsonl`」的整个设计（该设计存在的理由见 `write_record`
    docstring：同文件并发写实测丢 5%~38%）。已实证：位置对调后 84 个既有测试
    全绿，说明类型系统与既有断言都拦不住，只能靠调用形态本身报错。

    **`admitted` 落盘按 `total` 降序截断到 `admitted_k` 条**（默认 20，与
    `near_miss_k` 同族）：真实 Vault 实测单条记录 `admitted` 可达 58~156 条，
    而渲染层只展示 `max_notes=5` 条，未截断的落盘体积（11795~29962 字节）
    超出 README 声称上界（3197 字节）3.7~9.4 倍。截断只影响落盘的**展示样本**，
    不影响统计口径：`n_admitted`（截断前真实条数）与 `arm_counts`（截断前对
    全部 admitted 按 `admit_arm` 统计的计数字典）在截断**之前**算出并单独落盘，
    `analyze_metrics.summarize` 优先读这两个聚合字段，缺失（旧记录）才回退到
    遍历 `admitted` 数组——保证聚合口径不因截断而失真，也不让旧记录在新报表里
    算错或崩溃。
    """
    kws = sorted(prompt_keywords or ())
    # 只排一次序，两个样本都从它切 —— excluded 在真实 Vault 上中位数 736 条。
    # 省下的第二次 `sorted(721)+[:10]` 实测 min 45.9us / median 50.6us，而本函数
    # 全程 min 120~132us ⇒ **约 +38%**（占 300ms UPS 预算的 0.017%）。
    # ⚠️ 此处原写「会把本函数的主要开销翻倍」，是未经实证的推算，已按实测订正——
    # 它当时紧挨着下面那批精确实测数字（226/216、188/482），相邻的实证反而给它背了书。
    ranked = sorted(decision.excluded, key=lambda ed: -ed.topical)
    near = ranked[:near_miss_k]
    # 「真·擦肩」样本必须**在截断之前**就把去重抑制的条目排掉，不能留给消费侧过滤。
    # 两类条目的 topical 有结构性差异（不是统计巧合，是 _decision.py 的分支决定的）：
    #   - 新篇落 excluded 的条件是 `t < min_topical`（:206）⇒ topical **严格 < 4**
    #   - fulltext_injected 分支（:171）**不看 topical 就 excluded** ⇒ 可以高到 11
    #   - candidate_injected 分支（:189）条件是 t < ft_topical(6) ⇒ 可以落在 [4, 6)
    # 于是按 topical 降序取 top-k 时，被去重的条目天然占满槽位：真实数据实测 226 个
    # 混合事件里 216 个严格分层，**188/482（39%）的轮次里 score-low 一条都进不了
    # `near_miss`**。在消费侧再过滤，拿到的是「k 减去本轮已注入篇数」的残差而非真相。
    #
    # 新增**加性可选字段**而不改 `near_miss` 的语义，是为了不 bump SCHEMA：
    # `analyze_metrics.load_records` 用严格相等过滤（:68），bump 会让既有记录静默
    # 消失（实测本机已积累 1018 条）。旧键保留原样，供回溯与旧记录兼容。
    # 惰性取前 k：`ranked` 已按 topical 降序，找满 k 个即停，不再遍历剩余条目。
    # **`topical >= NUDGE_TOPICAL_FLOOR` 在这里施加、不留给消费侧**（见该常量的说明）：
    # 消费侧过滤靠三处各自记得调同一个函数，上一版就是这样漏掉的；生成侧施加后磁盘上
    # 不存在低于 floor 的条目，判据无从分叉。代价是这些条目永久不落盘，故把生效阈值
    # 一并落盘为 `scorelow_floor`。
    near_scorelow = list(islice(
        (ed for ed in ranked
         if not ed.dedup and ed.topical >= NUDGE_TOPICAL_FLOOR),
        near_miss_k))
    admitted_all = decision.admitted
    n_admitted = len(admitted_all)
    # "?" 与 summarize() 里 `a.get("arm") or "?"` 的兜底口径保持一致：admit_arm
    # 理论上恒非空（见 _decision.py 的三种赋值），但防御性地兜底、避免与旧遍历口径分叉。
    arm_counts = Counter(ed.admit_arm or "?" for ed in admitted_all)
    # 显式按 total 降序排序再截断——不依赖 decision.admitted 已被上游 (-total, -mtime)
    # 预排序这一未在本函数签名/契约中声明的隐式前提，函数自身对排序负责。
    admitted_top = sorted(admitted_all, key=lambda ed: -ed.total)[:admitted_k]
    record = {
        "_schema": current_schema(),
        "ts": round(time.time(), 3),
        "session": session_id or "",
        "prompt_id": prompt_id or "",
        "cwd_h": h(str(cwd), salt),
        "kw_h": [h(k, salt) for k in kws],
        # prompt 原文的加盐 hash —— **不是**用来还原内容，而是给 `--review` 当**定位键**。
        #
        # 为什么必须有它：标注要回答「这篇笔记该不该被召回」，而这个问题在不知道
        # 「当时问的是什么」时**根本无法回答**。此前 --review 只显示「被召回 63 次、
        # topical<=10.8、路径」，标注者对着这些信息按 r/i/u，等于掷骰子。
        #
        # 为什么是 hash 而不是原文：prompt 原文绝不落盘（本函数 docstring 的隐私边界）。
        # 而 transcript(`~/.claude/projects/*/<session_id>.jsonl`) 本来就在本机、
        # 本来就存着全文 —— 标注时按 session_id 找到它，对每条 user message 算同样的
        # hash 做**精确匹配**，就能把原文取回来展示。全程不新增任何落盘的可读内容。
        #
        # 为什么不落 transcript_path（它确实在 hook payload 里，实测存在）：
        # 那个路径含项目目录名，而 `cwd` 特意只落了 hash —— 落它是隐私回退。
        # session_id 已经够定位（实测 387/388 = 99.7% 能找到对应 transcript）。
        #
        # 为什么不靠时间戳就近匹配：实测只有 82.8% 能唯一定位，17.2% 落在
        # 「同一秒内多条 user 消息」的歧义里，而标注是不可再生的数据，不能建在猜上。
        "prompt_h": h(prompt, salt) if prompt else "",
        "n_kw": len(kws),
        # 来源（hook stdin 的 promptSource）。harness 下发的枚举值，不含用户内容，
        # 明文落盘。**刻意无默认值**："" 是合法取值（空串按用户输入处理），给默认
        # 值会让「调用方漏传」与「来源真的缺失」在数据里无法区分，而按来源拆分
        # 统计正要切这一刀。
        # ⚠️ **实测在 Claude Code 2.1.220 上恒为 ""**：该键不在 UserPromptSubmit 的
        # hook stdin payload 里（本机 1018 条记录无一非空）。字段保留作探针——harness
        # 日后下发即自动有值。消费侧务必按「缺失/空串 = 人类输入」处理，不要写成
        # 白名单枚举：`analyze_metrics._NON_HUMAN_SRC` 上方记着那次教训（白名单让
        # 0.9.0 的精度标注通道从上线起 100% 空转，而守卫用例手工构造 src="typed"，
        # 是一种生产中从未存在过的形态，因此永远发现不了）。
        "src": src,
        "gate": decision.gate_reason,
        "relaxed": bool(decision.relaxed),
        "admitted": [
            {"path": ed.path, "topical": round(ed.topical, 3),
             "total": round(ed.total, 3), "arm": ed.admit_arm,
             "dedup": ed.dedup, "hits": list(ed.hits)}
            for ed in admitted_top
        ],
        "n_admitted": n_admitted,
        "arm_counts": dict(arm_counts),
        "admitted_k": admitted_k,
        "near_miss": [
            # dedup 在 _decision.py:173/184/191 已算好，落盘零额外成本——与本函数
            # 刻意不落 total/hits（需补算、实测 +150~200ms）的性能护栏不冲突。
            # 没有它就分不清「被去重抑制」（其实已成功召回过）与「打分不够」，
            # 而前者混在 near-miss 榜首会让 --report 与 nudge 提示误报。
            {"path": ed.path, "topical": round(ed.topical, 3), "dedup": ed.dedup}
            for ed in near
        ],
        # 只含「过不了精度闸门」（dedup == ""）**且** topical >= scorelow_floor 的条目，
        # 即真正意义上的擦肩而过。三个消费者一律经 `scorelow_entries` 读这个键；
        # `near_miss` 那份留给回溯与旧记录。
        "near_miss_scorelow": [
            {"path": ed.path, "topical": round(ed.topical, 3)}
            for ed in near_scorelow
        ],
        # 生效阈值随记录落盘（与 near_miss_k/admitted_k 同族的自描述字段）。
        # 它换不回没落盘的低分条目，但至少让「这批记录当时按什么阈值筛的」可查——
        # 否则日后调 floor，新旧记录混在一起就再也分不清谁是按哪个口径写的。
        "scorelow_floor": NUDGE_TOPICAL_FLOOR,
        "n_excluded": len(decision.excluded),
        "dedup_counts": _dedup_counts(decision.excluded),
        "ft": {"path": decision.fulltext_path or "", "arm": decision.fulltext_arm},
        "near_miss_k": near_miss_k,
        # 渲染层的 max_notes 也随记录落盘（同 near_miss_k/admitted_k 的自描述模式）。
        # `analyze_metrics` 不读 config，此前把默认值 3 硬编码进报表文案与
        # `sample_admitted` 的取数——用户一旦改这个配置项，报表就开始说假话，
        # 而抽样池会抽到**从未渲染给用户**的条目（标注对象本该是「用户实际看到的」）。
        "max_notes": max_notes,
    }
    # 两个最承重的判据阈值。`analyze_metrics` 不读 config，而
    # `fulltext_topical_threshold` 在样本期内已经由 6 改成 10（实测两期全文注入率
    # 65.4% vs 40.1%），报表却只能给出不描述任何一个时期的合并值。补 `max_notes`
    # 时写下的理由（「用户一改配置报表就开始说假话」）逐字适用于这两个字段。
    # 不传时不落键：让读端能用 `in` 判存在，而不是把「没记录」误读成某个具体值。
    if min_topical is not None:
        record["min_topical"] = min_topical
    if ft_topical is not None:
        record["ft_topical"] = ft_topical
    if _RUNTIME != "legacy":
        record["runtime"] = _RUNTIME
        record["event_id"] = _EVENT_ID or prompt_id or ""
    return record


NEAR_MISS_MAX_ENTRIES = 500     # 总条目上限，仅在超出时按 count 降序截断


def scorelow_entries(rec: dict,
                     floor: float = NUDGE_TOPICAL_FLOOR) -> list[tuple[str, float]]:
    """从一条记录里取出「够格算真·擦肩」的 `(path, topical)` —— 三个消费者共用出口。

    **返回 (path, topical) 而不是只返回 path，是这次修复的关键**：上一版只提供
    `scorelow_paths`（`list[str]`），而 `summarize` 的榜单要计数、`sample_near_miss`
    要 `topical_max`，两者都拿不到需要的形态，于是**各自内联读了裸键**并双双漏掉
    下限过滤——「单点判据」四处写进文档却只有 1/3 的消费者在用。抽象没被采用时，
    先怀疑它的形态是不是只贴合了第一个调用者。

    **floor 现在是双重施加**（生成侧 `build_record` + 这里）：
    - 生成侧那道是主判据，让磁盘上根本不存在低于 floor 的条目（见 NUDGE_TOPICAL_FLOOR）；
    - 这里这道是二次判据，专为两种情形保留：① 判据下沉**之前**已落盘的记录仍含低分
      条目；② 将来 floor 调整后，旧记录按旧阈值落盘。两者都会让磁盘数据与当前判据
      不一致，而消费者不该去区分「这条记录是哪个版本写的」。
      幂等的代价只是一次比较，换掉的是一整类版本分支。

    **旧记录（无 `near_miss_scorelow` 键）返回空** —— 不是保守，是它们的 `near_miss`
    样本在截断阶段就已被去重条目挤占（39% 的轮次一条 score-low 都没留下），对旧样本
    做过滤得到的是有系统性偏斜的残差，宁可不计也不要把偏斜的数计进长期累计。

    **`topical` 用 try/except 而非裸 `float()`**：磁盘 jsonl 可能被手工编辑或位翻转，
    实测 `topical="high"` 抛 ValueError、`topical={}` 抛 TypeError。这条路径**今天
    就可达**——`sample_near_miss` / `sample_admitted` 在 `--review` 里直接消费磁盘记录。
    畸形值按「不达标」丢弃该条，不抛：`--report`/`--review` 是排障入口，最不该在数据
    异常时整个罢工（实测未加此守卫时，一条坏记录令 stdout 完全为空）。
    """
    items = rec.get("near_miss_scorelow")
    if not isinstance(items, list):
        return []
    out: list[tuple[str, float]] = []
    for nm in items:
        if not isinstance(nm, dict):
            continue
        p = nm.get("path")
        if not (isinstance(p, str) and p):
            continue
        try:
            t = float(nm.get("topical") or 0)
        except (TypeError, ValueError):
            continue                      # 手工改坏的值当不达标，不让 CLI 崩
        if t >= floor:
            out.append((p, t))
    return out


def scorelow_paths(rec: dict, floor: float = NUDGE_TOPICAL_FLOOR) -> list[str]:
    """`scorelow_entries` 的 path 投影。`flush()` 的 nudge 计数只需要 path。"""
    return [p for p, _ in scorelow_entries(rec, floor)]


def _counts_path(home: Path) -> Path:
    return metrics_dir(home) / "near_miss_counts.json"


def _counts_lock_path(home: Path) -> Path:
    return metrics_dir(home) / "near_miss_counts.lock"


# 锁重试预算。取 250ms 而非更小值的依据：`bump_near_miss_counts` 只在 `flush()` 里
# 调用，而 `flush()` 恒在 `emit()` **之后**执行（见 `_finish_with_metrics` docstring）
# —— 本轮注入早已送达，这里排队不产生任何用户可感知延迟。故预算可以给得宽裕。
# 实测（barrier 严格同步的 4 进程各 +15，且循环内零间隔立即重新竞争——**远比生产
# 苛刻**，生产每轮 hook 只 +1 一次、且分散在不同 prompt 之间）：
#   50ms  预算 -> 落盘 57/60
#   250ms 预算 -> 落盘 57~60/60（三轮实测 57 / 60 / 59）
# 注意这里**不保证零丢失**，且刻意不再往上加预算：继续调大只是在把代码往这个
# 人为最苛刻的测试上凑，而非往真实竞争强度上凑。丢失量已被独立探针确认**恰好等于
# 取锁失败次数**（3=3 / 0=0 / 1=1），即全部来自「超预算主动放弃」这条设计路径，
# 不含任何正确性缺陷。对照原实现：同一场景只落 3/60（丢 95%）。
# 仍然**有界**：超预算即放弃本次 +1 并返回，绝不无限等——hook 卡死不可接受，
# 而丢一次计数无所谓（阈值是 10，多攒一轮即可）。
_LOCK_BUDGET_SEC = 0.25
_LOCK_SLEEP_SEC = 0.002
# 陈旧锁阈值。持有者崩溃会留下永不释放的锁文件，那会让计数从此**永久停摆**——
# 比原来的丢更新缺陷更糟。超过此秒数即视为无主、强夺。取值远大于临界区耗时
# （~1ms）与重试预算（50ms），正常竞争绝不会被误判为陈旧。
_LOCK_STALE_SEC = 10.0


def _acquire_counts_lock(home: Path) -> int | None:
    """获取 near_miss_counts 的写锁；拿不到返回 None（调用方应放弃本次 +1）。

    用 `O_CREAT|O_EXCL` 而非 `fcntl`/`msvcrt`：前者是可移植的原子创建，本仓库
    `get_salt` 防 TOCTOU 已用同一模式；后两者各自只在 POSIX / Windows 可用，
    而本项目必须三平台兼容。
    """
    p = _counts_lock_path(home)
    deadline = time.time() + _LOCK_BUDGET_SEC
    while True:
        try:
            return os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                           | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            try:
                if time.time() - p.stat().st_mtime > _LOCK_STALE_SEC:
                    p.unlink(missing_ok=True)      # 陈旧锁：持有者已崩溃，强夺
                    continue
            except OSError:
                pass                                # stat/unlink 竞争失败即当作没抢到
            if time.time() >= deadline:
                return None
            time.sleep(_LOCK_SLEEP_SEC)
        except OSError:
            return None                             # 目录不可写等，直接放弃


def load_near_miss_counts(home: Path) -> dict[str, int]:
    p = _counts_path(home)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {}
        return {k: int(v) for k, v in d.items()
                if isinstance(k, str) and isinstance(v, (int, float))}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def bump_near_miss_counts(home: Path, paths: list[str]) -> None:
    """累加计数；**只在超容量时**按 count 降序截断到 NEAR_MISS_MAX_ENTRIES。

    **不得加「丢弃 count < N」的无条件裁剪。** hook 是一次性短进程，每轮对同一
    path 只 +1；无条件裁剪会让每次调用都从 0 加到 1 再被裁掉、写回空盘，计数
    永远到不了任何阈值，near-miss 提示在生产中完全失效（PoC 实证 15 轮后仍为 {}）。
    低频条目的淘汰交给容量上限：超限时 count 低者自然被挤掉。
    tiebreak 用 path 名，保证裁剪结果确定、测试可复现。

    不在 hook 内扫描全部 metrics 文件——90 天约 15~26 MB（见下方实测区间），
    会顶穿 UPS 的 81ms 余量。

    **读-改-写整段受锁保护（P2 修复）。** 本文件是**跨 session 全局共享**的，
    碰撞面比 `write_record` 大得多（后者只在同一 session resume 到两个终端时才碰）。
    无保护时并发下会**整条丢更新**且完全静默——4 进程各 +15 实测只落 **3**
    （期望 60，丢 95%）。危害不是数字略偏：计数永远攒不到阈值 ⇒ near-miss 提示
    静默失效，与「无条件裁剪」是同一失效表现的不同成因。
    锁拿不到时**放弃本次 +1 并直接返回**——见 `_acquire_counts_lock` 的取舍说明。
    """
    if not paths:
        return
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, 0o700)
    fd = _acquire_counts_lock(home)
    if fd is None:
        return                      # 竞争激烈，本次放弃（阈值 10，丢一次可接受）
    try:
        _bump_locked(home, paths)
    finally:
        os.close(fd)
        _counts_lock_path(home).unlink(missing_ok=True)


def _bump_locked(home: Path, paths: list[str]) -> None:
    """读-改-写本体。**调用方必须已持有 `_acquire_counts_lock` 的锁。**"""
    c = load_near_miss_counts(home)
    for p in paths:
        if isinstance(p, str) and p:
            c[p] = c.get(p, 0) + 1
    kept = c
    if len(kept) > NEAR_MISS_MAX_ENTRIES:
        kept = dict(sorted(kept.items(),
                           key=lambda kv: (-kv[1], kv[0]))[:NEAR_MISS_MAX_ENTRIES])
    # 目录创建与 chmod 已由调用方在取锁**之前**完成（取锁本身要求目录存在）
    cp = _counts_path(home)
    cp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    _chmod(cp, 0o600)


def reset_counts(home: Path) -> int:
    """清空 near-miss 累计计数（含 nudge 冷却戳），返回被清掉的条目数。

    **为什么需要一个专门的出口**：计数只增不减，且 `bump_near_miss_counts` 的
    500 条容量在本机已经顶满。改判据只影响**此后**的累加，存量污染（实测 211 条
    已达阈值、其中 78.2% 是被去重抑制的）不会自行消退 —— 不给重置手段的话，
    用户看到的那条 nudge 提示在修复上线后一字不变。

    **它是派生数据，不是用户数据**：`purge()` 的 docstring 自己把
    `near_miss_counts.json` 与 `nudge_ts.json` 归为「派生 json」，与「删了不可
    重新生成」的 `annotations.jsonl` 明确分开。故本函数刻意**不碰** annotations
    与事件目录 —— 用户想全清有 `--purge`，这里只做可再生部分。

    连带删 `nudge_ts.json`：计数归零后旧的冷却戳会把下一次提示推迟到一周后，
    而此刻恰恰是最该让新口径提示尽早出现的时候。

    **取锁再删**：`_bump_locked` 的 docstring 声明「该文件的一切读-改-写都必须持锁」，
    而本函数直接 unlink 就绕过了那条不变量。竞态很具体——某个 hook 进程正处在
    「已 read、未 write」的窗口内时，它随后的 `write_text` 会把重置前读到的计数原样
    写回，用户看到「已清空（N 条）」却一条没少，且完全无声。窗口窄、本命令又是人工
    交互触发，概率低；但代价是「这个函数存在的目的被反转」，而取锁的成本只有几行。
    拿不到锁时**如实报错**而不是假装成功——本函数跑在 CLI 里（`main()` 捕获后返回 1），
    不在 hook 路径上，fail-open 约束不适用。
    """
    n = len(load_near_miss_counts(home))
    metrics_dir(home).mkdir(parents=True, exist_ok=True)   # 取锁要求目录存在
    fd = _acquire_counts_lock(home)
    if fd is None:
        raise RuntimeError(
            "near-miss 计数正被其他进程写入，未做任何清理；请稍后重试")
    try:
        _counts_path(home).unlink(missing_ok=True)
        _nudge_ts_path(home).unlink(missing_ok=True)
    finally:
        os.close(fd)
        _counts_lock_path(home).unlink(missing_ok=True)
    return n


def _prune_ts_path(home: Path) -> Path:
    return metrics_dir(home) / "prune_ts.json"


def _prune_due(home: Path, ttl_hours: float = 24) -> bool:
    """距上次 `prune_expired` 执行是否已超过 `ttl_hours`（默认一天一次）。

    仿 `nudge_due` 同一写法：文件不存在或损坏一律视为「早已到期」，立即执行一次
    （首次调用必然清理；`.salt` 那类"损坏后永不自愈"的顾虑在这里不适用——写坏了
    至多导致多扫一次目录，不会像 salt 错位那样污染历史 hash）。
    """
    p = _prune_ts_path(home)
    if not p.exists():
        return True
    try:
        last = float(json.loads(p.read_text(encoding="utf-8")).get("ts", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return True
    return time.time() - last > ttl_hours * 3600


def mark_pruned(home: Path) -> None:
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    p = _prune_ts_path(home)
    p.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    _chmod(p, 0o600)


def _nudge_ts_path(home: Path) -> Path:
    return metrics_dir(home) / "nudge_ts.json"


def nudge_due(home: Path, threshold: int = 10, ttl_hours: int = 168) -> list[str]:
    """达阈值且全局冷却已过的 path 列表。

    **全局**而非 per-cwd：`_state.py:21-25` 的隔离是为项目局部诊断设计的，
    而 near-miss 是全局性质；本机 25 个 cwd 目录，per-cwd 会把周期放大 25 倍。
    """
    last = 0.0
    p = _nudge_ts_path(home)
    if p.exists():
        try:
            last = float(json.loads(p.read_text(encoding="utf-8")).get("ts", 0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            last = 0.0
    if time.time() - last <= ttl_hours * 3600:
        return []
    return sorted(k for k, v in load_near_miss_counts(home).items() if v >= threshold)


def mark_nudged(home: Path) -> None:
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    p = _nudge_ts_path(home)
    p.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    _chmod(p, 0o600)


# ── 进程级缓冲。hook 是一次性短进程，无并发写入。 ──────────────────────────
# 仿 _diagnostics._PENDING：stage 零 IO、只登记；真正写盘由出口处唯一一次 flush 完成。
_PENDING: dict | None = None


def reset() -> None:
    """清空缓冲。生产上一个 hook 进程只跑一次，无需调用；供测试隔离用。"""
    global _PENDING
    _PENDING = None


def stage(record: dict) -> None:
    """登记本轮记录。**零 IO、零副作用**。"""
    global _PENDING
    _PENDING = record


# `annotate` 允许写入的键。白名单而非任意 kwargs：`_PENDING` 持有的是隐私域记录
# （含 kw_h / cwd_h），无约束的 `update()` 等于给它开了一个通用可写入口，未来任何
# 调用点都能覆盖既有键或塞进内容字段，而 `build_record` docstring 里那套「不可协商」
# 的隐私边界只约束 build_record 自己。一行校验把边界从"约定"变成"机制"。
# `shown` 是渲染层算出的**权威**列表（用户实际看到的那几篇，有序）。
# 读端此前只能从落盘的 `admitted` 数组按 `limit = max_notes - (1 if ft else 0)`
# 重建，而那条重建路径是脆的：实测 18822 条 admitted 的 `total - topical` 只有
# 两个取值（0.5 / 1.0），于是 **97.7% 的轮次前 5 条 total 存在并列**，并列时的
# 实际次序由 `-mtime` 决定，而 mtime 根本不落盘。今天重建仍然对，只是因为
# `build_record` 用了稳定排序且上游预排过 —— 而 `build_record` 的注释恰恰声明
# 「不依赖上游已按 (-total, -mtime) 预排序这一隐式前提」。任何人照那句注释去改
# 排序键，全部历史与未来记录的「用户看到了什么」都会静默错位，且没有任何用例会红。
_ANNOTATE_ALLOWED = frozenset({"inj_chars", "shown"})


def annotate(**fields: object) -> None:
    """给已 stage 的记录补充渲染后才知道的字段。**零 IO**。

    `_PENDING` 为 None 时**必须直接返回**，不得写成 `_PENDING = _PENDING or {}`
    ——后者会在 metrics 关闭态凭空造出一条非空记录，随后被 `flush()`（只看
    `_PENDING` 真值）落盘，正好从这个缝里破掉 opt-in 的零足迹边界。

    不覆盖已存在的键：调用方补的是 build_record 之后才产生的信息，撞键即意味着
    语义冲突，静默覆盖会让落盘值与 build_record 的契约不一致。
    """
    global _PENDING
    if _PENDING is None:
        return
    for k, v in fields.items():
        # 越界键与撞键都**出声**。白名单本身是对的，但静默丢弃与本模块「不静默」的
        # 惯例相左（load_records/load_annotations/prune_expired 全都经 stderr 报出）。
        # 具体风险：将来某处写成 `annotate(inj_char=...)`（少个 s），结果是该字段恒缺失、
        # 报表恒显示「无数据」——**与「功能没上线」在现象上完全一样**，正是这批缺陷的
        # 家族特征。
        # ⚠️ 只打 stderr、**不抛异常**：`_finish_with_metrics` 是单 try 块，annotate
        # 抛错会连带跳过同块内的 flush，整条记录丢失。
        if k not in _ANNOTATE_ALLOWED:
            print(f"[vault-loader] annotate 忽略未登记字段 {k!r}"
                  f"（允许集 {sorted(_ANNOTATE_ALLOWED)}）", file=sys.stderr)
            continue
        if k in _PENDING:
            print(f"[vault-loader] annotate 撞键 {k!r}，保留 build_record 的原值",
                  file=sys.stderr)
            continue
        _PENDING[k] = v


def flush(home: Path, retention_days: int | None = None) -> None:
    """把缓冲写盘。调用方必须用 try/except 兜住（见 prompt_submit_load）。

    `retention_days` 非 None 时额外接线 H2 修复——按「每天最多一次」的频率闸门
    触发 `prune_expired`（`prune_ts.json`，写法照抄 `nudge_ts.json`/`mark_nudged`）：
    距上次执行不足 24 小时直接跳过，**不做任何目录扫描**（`_prune_due` 只读一个
    时间戳文件，`event_month_dirs` 意义上的真正扫描只在到期时才发生）。

    选在这里而不是 hook 主流程接线：`flush()` 恒在 `emit()` 之后调用（见
    `_finish_with_metrics` docstring），用户对这次清理零延迟感知；日频闸门保证
    绝大多数轮次连时间戳文件都不用读第二次的 IO 都省下来。

    整段包在同一个 try/except 内、异常只打 stderr——`flush()` 本身处于 emit() 之后，
    清理失败绝不能让本轮已经完成的注入受影响，也不能让异常从 `flush()` 抛出去
    （调用方虽然也兜了一层，但这里独立兜底更贴近"接线只做增强、不改变既有失败面"
    的既定风格，与 near-miss 提示、metrics 构造等其余可选环节一致）。
    """
    global _PENDING
    if _PENDING:
        rec = _PENDING
        _PENDING = None
        write_record(home, str(rec.get("session") or ""), rec)
        # 只计「真·擦肩」：判据与报表榜单、--review 抽样池共用 scorelow_paths 单点。
        # 改动前是无差别计入全部 near_miss，实测后果——达 nudge 阈值的 211 篇里
        # 78.2% 其实已被注入过（43.6% 甚至被全文注入过），提示的语义整个是反的：
        # 它想说「这篇一直召不回，去调 tags/keywords」，报出来的却是「这篇早就给过你了」。
        # `scorelow_paths` 对缺少新键的极简 gate 记录返回 []，`bump_near_miss_counts`
        # 的 `if not paths: return` 在 mkdir/取锁之前，故 gate 轮次连目录都不会碰。
        bump_near_miss_counts(home, scorelow_paths(rec))
    if retention_days is not None:
        try:
            if _prune_due(home):
                prune_expired(home, retention_days)
                mark_pruned(home)
        except Exception as exc:  # noqa: BLE001 — 清理绝不影响主流程
            import sys
            print(f"[vault-loader] metrics 清理失败：{exc}", file=sys.stderr)


def prune_expired(home: Path, retention_days: int) -> int:
    """删除超出保留期的月份目录。返回删除的文件数；**不静默**（对齐 OBS-8）。

    边界：判断用 `d.name < keep_from` 严格小于——**恰好等于**保留期截止月份
    （`keep_from` 当月）的目录会被**保留**，不删除。取舍：按月份粒度裁剪本就是
    近似值（同月内早于/晚于 cutoff 的记录不做日级区分），严格小于让「刚好卡线」
    的月份多留一轮而非提前一天误删，偏保守。
    """
    # shutil 刻意局部 import（L-PY3）：本模块被每次 UserPromptSubmit 无条件
    # import，而 shutil 只在这里和 purge() 用得上。H2 之后 prune_expired 会被
    # flush() 调用，但有「每天至多一次」的频率闸门挡着——绝大多数 UPS 调用根本
    # 走不到这一行，把 shutil 提到模块顶层等于给每次 UPS 都加一份导入成本。
    # （sys 不这样处理：它是内建模块、解释器启动即常驻，局部化零收益，已提到顶层。）
    import shutil
    cutoff = time.time() - retention_days * 86400
    keep_from = time.strftime("%Y-%m", time.localtime(cutoff))
    removed = 0
    for d in event_month_dirs(home):
        if d.name < keep_from:
            removed += len(list(d.glob("*.jsonl")))
            shutil.rmtree(d, ignore_errors=True)
    if removed:
        print(f"[vault-loader] metrics 保留期 {retention_days} 天，"
              f"已清理 {removed} 个过期会话文件", file=sys.stderr)
    return removed


def purge(home: Path) -> int:
    """清空全部指标数据，保留 .salt（否则历史 hash 无法再对齐）。

    实现按「除 .salt 外全清」，**不是**枚举已知文件名：Task 13 会在顶层新增
    near_miss_counts.json / nudge_ts.json，枚举式实现会静默漏清，与 SKILL.md
    和 README 承诺的「一键清空」矛盾，且没有任何测试能拦住。

    **顶层 `annotations.jsonl`（Task 12 的人工标注）也会被清，且必须计入返回值。**
    它与派生数据不同——是用户逐条投入时间标出的 relevant/irrelevant/unsure，
    删了不可重新生成。仍然清，是因为 `--purge` 是隐私兜底、标注里含笔记路径，
    留例外就在「一键清空」上开了洞；但**静默清是缺陷**：CLI 必须在删除前用
    `count_annotations()` 读出条数并明确告知不可恢复（见 Task 11）。

    返回值 = 删除的数据文件（`.jsonl`）总数，含 annotations.jsonl。
    `near_miss_counts.json` / `nudge_ts.json` 这类派生 json 不计入。
    """
    import shutil          # 同 prune_expired，局部导入的理由见那里
    root = metrics_dir(home)
    if not root.exists():
        return 0
    n = 0
    lock_target = root.parent / f".{root.name}-purge"
    with lease_lock(lock_target):
        for d in list(root.iterdir()):
            if d.is_dir():
                count = len(list(d.glob("*.jsonl")))
                shutil.rmtree(d)
                if d.exists():
                    raise OSError(f"metrics directory still exists after purge: {d}")
                n += count
            elif d.name != ".salt":
                is_event = d.suffix == ".jsonl"
                d.unlink()
                if d.exists():
                    raise OSError(f"metrics file still exists after purge: {d}")
                if is_event:
                    n += 1      # annotations.jsonl：必须计数，不能静默消失
    return n


def count_annotations(home: Path) -> int:
    """purge 前供 CLI 读取——人工标注不可重新生成，删除前必须告知条数。

    errors="replace"：与 `prompt_submit_load.py:293` / `_signal_collect.py:88` 同一约定——
    `annotations.jsonl` 若因写入中断（同类风险见 `write_record` docstring 讨论的并发撕裂行）
    留下非 UTF-8 字节，不应让本函数抛 UnicodeDecodeError 崩给调用方。坏字节被替换为 U+FFFD
    而非整行丢弃，该行仍计入返回值——它仍是一条待清理的数据，计入比漏计更安全。
    """
    p = metrics_dir(home) / "annotations.jsonl"
    if not p.exists():
        return 0
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return sum(1 for line in text.splitlines() if line.strip())
    except OSError:
        return 0
