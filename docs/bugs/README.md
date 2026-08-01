# Bug & 改进记录

本目录记录真实使用中的问题、根因，以及整条链路上的修复。  
**不是路线图**：下面「仍可商量」只是观察项，下一步做什么一起定。

上游与已做能力总览见 [README.md](../../README.md)。  
文档索引：[docs/README.md](../README.md) · 评测：[evals.md](../evals.md)

---

## 索引

| ID | 标题 | 状态 | 一句话 |
|----|------|------|--------|
| [001](./001-todo-drift.md) | Agent 偏移（任务表 · 话题 · 会话） | A 已修；B/C 大部分已修 | 「活干了表不对 / 问 A 答 B / 打断 400」 |
| [002](./002-prompt-cache-vs-dynamic-context.md) | Prompt 缓存命中 | Phase 1/2 已落地 | static/ephemeral 分层 + append-only 历史 |
| [003](./003-resume-opt-in.md) | Resume OpenCode 模式 | 已实施 | 默认全新；续项目要 `/resume project` |
| [004](./004-context-compaction.md) | 上下文压缩丢信息 | Phase 2 已实施 | 入场定型 + 检查点摘要/tail；摘要不压过本条用户话 |
| [005](./005-tool-loop-drift.md) | 工具空转与目标漂移 | 部分缓解 | LookupGuard 硬拦；micro_compact 防嵌套；MCP 超时温和返回 |
| [006](./006-final-answer-buried.md) | 最终回答看不见 | 已缓解 | A：compact 后 turn_start 漏打；B：cache/compact 刷屏盖住；C：loop 内立即打印 |
| [007](./007-permission-interrupt-gbk.md) | 权限卡住 / Esc / GBK | 已缓解 | Allow? 可取消；禁嵌套 agent；bash UTF-8 |
| [008](./008-textual-tui-m1.md) | Textual TUI M1 | 已移除 | 设计取舍存档；当前默认 Rich CLI |
| [009](./009-compact-empty-summary.md) | Compact 空摘要失忆 | 已修复 | 推理模型摘要在 thinking 块；extract 只读 text → `(empty summary)` |
| [010](./010-lookup-guard-calibration.md) | LookupGuard 校准 | 已修复 | 硬失败不烧全局 stale；连续 block 强制收口 + 禁 memory |
| [011](./011-autocompact-token-threshold.md) | Auto-compact 过早 | 已修复 | 50KB 字符硬限 → `0.835×context_window`（token） |
| [012](./012-pie-menus-lookup-throttle.md) | Pie Menus 联网护栏过紧 | 已修复+验过 | near-dup/跑题 SERP/次数硬顶掐死换源；去硬顶后 PASS |
| [013](./013-append-only-history-cache.md) | 工具历史改写破坏缓存前缀 | 已修复+回归通过 | tool result 入场定型；阈值前历史只追加 |
| [014](./014-long-turn-api-stall.md) | 长任务等待模型时 TUI 假死 | 已定位，延期修复 | SDK 最长等待 600 秒；Stop 无法中断阻塞请求 |

相关能力（非 bug 单）：本地 `/usage` 用量统计；mini-eval（[evals.md](../evals.md)）；SWE-bench Lite（`python -m evals.swebench`）。

---

## 关系怎么串起来

```text
启动会话边界 ──► 003 Resume（默认不灌旧论文）
每轮怎么拼 prompt ──► 002 缓存分层（static 稳、动态进 ephemeral）
长了怎么压上下文 ──► 004 compact（摘要+tail+落盘+最新 user）
                   ├► 009 空摘要（thinking 回退 + 不可用则降级，勿失忆）
                   ├► 011 阈值（`0.835×window` token，勿 50KB 字符硬砍）
                   └► 013 缓存前缀（tool 入场定型；普通轮次 append-only）
模型跟不跟任务 ──► 001 偏移（todo / 话题 / 打断）
工具空转 / 检索漂移 ──► 005（重复调用 · 手段取代目的 · 意图展示 · 跟进选择题走偏）
                   ├► 010 LookupGuard（硬失败≠全局 stale；block 升级强制答）
                   └► 012 Pie Menus（near-dup / 跑题 SERP / 去掉次数硬顶）
终答看得见吗 ──► 006（漏打 / 刷屏 / loop 内打印）
权限 / 打断卡死 ──► 007（Allow? + GBK）
默认交互壳 ──► 008（Textual 设计存档；当前 Rich CLI）
```

| 用户痛点 | 主要文档 | 004 有没有帮到 |
|----------|----------|----------------|
| 重启又跟旧论文 | **003** | 无关 |
| 账单命中率低 | **002 / 013** | **有**（静动态分层 + 历史不再逐轮改写） |
| compact 后跑偏、忘路径 | **001-B / 004** | **有**（模板 + tail + persist + 最新 user 置顶） |
| todo 表和进度不一致 | **001-A** | 几乎无（靠 todos.json + 注入） |
| Ctrl+C 后 400 | **001-C** | 无关（靠 repair / undo） |
| 同一 fetch 刷屏、搜着搜着偏题；**找到答案仍不停** | **005** | 间接（大结果易触发 compact；主因是循环与收口） |
| **选模型/数据后跑去改 harness** | **005** | 无关（goal stickiness） |
| **答完了但屏幕上看不见总结** | **006** | A 漏打 / B 刷屏 / C loop 打印 |
| **Allow? 时 Esc 卡住 + GBK 崩** | **007** | 无关 |
| **想要分区 TUI（步骤/终答/输入）** | **008** | 无关 |

防偏移和省缓存**不是二选一**：动态内容放 ephemeral（002），模型仍看得见 todos（001）。

---

## 已落地（按主题）

| 主题 | 做了什么 | 文档 |
|------|----------|------|
| Todo 真相 | 每轮注入全表、3 轮 reminder、Rich Tasks、**`sessions/<id>/todos.json`** | 001-A · 003 |
| 本条用户意图 | `latest_user_query` 进 ephemeral | 001-B |
| 打断 / orphan tool | repair + abort 回滚 | 001-C |
| 缓存分层 | static / dynamic / ephemeral + `if_unchanged` | 002 |
| 缓存前缀稳定 | tool result 入场前落盘/裁剪；普通轮次 append-only；仅检查点重写 | 013 · 004 |
| 默认全新会话 | `/clear` 全清；resume opt-in | 003 |
| 压缩保真 | compact 留 5 条 tail；六段摘要；入场落盘；时间分钟级；**最新 user 覆盖摘要旧目标**；`agent/compact/` 模块化 | 004 · 013 |
| Auto-compact 阈值 | `estimate_tokens ≳ 0.835×context_window`（对齐 Claude Code）；`models.json` 写窗口 | 011 |
| 空摘要 | thinking 回退 + 不可用则降级，禁止 `(empty summary)` 失忆 | 009 |
| 用量可见 | `/usage` 日/周/月/年；提示符 `[model]` | （功能） |
| 工具空转 | RepeatGuard；LookupGuard；micro_compact 防嵌套；MCP 超时温和返回；lookup mode | 005 |
| Lookup 校准 | hard/soft 分维；连续 block → strip tools + FORCE_ANSWER；证据≠记忆 | 010 |
| Pie Menus 护栏 | near-dup/跑题 SERP/去次数硬顶；单题 `run_20260722T152040Z` PASS | 012 |
| 终答可见 | A：`resolve_turn_start`；B：静默 cache/compact；C：loop 内 `emit_final_assistant` | 006 |
| 权限/打断 | 可取消 Allow?；禁嵌套 agent；bash UTF-8 | 007 |

---

## 仍可商量（有痛点再做）

按「你现在最常撞到什么」选，不按编号强制顺序：

| 候选 | 痛点 | 大致做法 | 关联 |
|------|------|----------|------|
| ~~① compact 不压过最新用户话~~ | ~~摘要里旧目标绑架本轮~~ | **已做**：compact 末尾强制 `[Current user request]` | 001-B、004 |
| ~~② snip / 50KB 过早 compact~~ | ~~字符硬限过早压~~ | **已做**：`0.835×window` token 阈值 | 011 · 004 |
| **③ 解释模式** | 问「为什么偏移」仍去改文件 | 检测 meta 问句 → 本轮禁 write/edit | 001-B |
| **④ 多 agent 隔离** | 同 cwd 共享 `.project/` | worktree 约定或 `HARNESS_PROJECT_DIR` | 003 |
| **⑤ memory 写回** | 用户纠正不进 `.memory/` | compact/纠正时 append constraints | 004 |
| **⑥ 缓存继续观测** | append-only 已修，真实长任务仍受 provider/换模型影响 | 看 `[cache]` 曲线；必要时再 slim ephemeral | 002 · 013 |
| **⑦ 工具空转收口** | 同 URL 死循环、搜完不答、**找到仍续爬**、**失败仍换 URL** | lookup LookupGuard + 任务完成判定（非全局小 max_rounds） | 005 |

**① 已落地。** ⑤/⑦ 与长检索体感强相关时可优先。

---

## 文档约定

每条记录尽量含：现象 → 背景 → 根因 → 已改 → 为何这么改 → 相关文件 → 仍观察。  
状态用语：`已实施` / `部分缓解` / `仍观察`（避免写死 Phase 路线图）。
