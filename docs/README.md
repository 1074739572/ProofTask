# 文档索引

本目录记录 **improved_harness** 的设计取舍、踩坑修复与阶段性改动。  
用户向总览见 [README.md](../README.md)；模块职责见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

---

## 快速导航

| 你想了解… | 去看 |
|-----------|------|
| 项目是什么、怎么跑 | [README.md](../README.md) |
| 模块怎么拆、数据落哪 | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| 问题编号与关联关系 | [bugs/README.md](./bugs/README.md) |
| 本地能力回归怎么评 | [evals.md](./evals.md) |
| 本地 RAG 仿写怎么接 | [rag.md](./rag.md) |
| RAG 从纯文本到 PDF 图表的演进与选型 | [rag-evolution.md](./rag-evolution.md) |
| RAG 解析、检索、回答怎么分层评测 | [rag-evaluation.md](./rag-evaluation.md) |
| 项目说明书 HARNESS.md | [project-instructions.md](./project-instructions.md) |
| 工具注册 / 调用 / 权限 | [tools.md](./tools.md) |
| 队友生命周期 | [teammate-lifecycle.md](./teammate-lifecycle.md) |
| 2026-07-07 大改总览 | [CHANGELOG-2026-07-07.md](./CHANGELOG-2026-07-07.md) |
| 2026-07-12 会话 / lookup / 评测 | [CHANGELOG-2026-07-12.md](./CHANGELOG-2026-07-12.md) |
| 2026-07-15 落锚 / 工具 UI / skills | [CHANGELOG-2026-07-15.md](./CHANGELOG-2026-07-15.md) |

本地约定：`.local/` 只放下载、实验与运行产物（不入库，见根 [.gitignore](../.gitignore)）。

---

## Bug & 改进记录（`bugs/`）

按编号记录「现象 → 根因 → 改了什么 → 仍观察」。**不是路线图**。

| ID | 标题 | 状态 |
|----|------|------|
| [001](./bugs/001-todo-drift.md) | Agent 偏移（任务表 · 话题 · 会话） | A 已修；B/C 大部分已修 |
| [002](./bugs/002-prompt-cache-vs-dynamic-context.md) | Prompt 缓存命中 | 分层已落地 |
| [003](./bugs/003-resume-opt-in.md) | Resume OpenCode 模式 | 已实施 |
| [004](./bugs/004-context-compaction.md) | 上下文压缩丢信息 | Phase 1 + 最新 user 优先 |
| [005](./bugs/005-tool-loop-drift.md) | 工具空转与目标漂移 | 部分缓解（RepeatGuard · Lookup mode） |
| [006](./bugs/006-final-answer-buried.md) | 最终回答看不见（漏打 · 刷屏） | 已缓解（turn_start + 静默诊断行） |
| [007](./bugs/007-permission-interrupt-gbk.md) | 权限卡住 / Esc / GBK | 已缓解 |
| [008](./bugs/008-textual-tui-m1.md) | Textual TUI M1 | 已移除（设计存档；当前默认 Rich CLI） |
| [009](./bugs/009-compact-empty-summary.md) | Compact 空摘要失忆 | 已修复 |
| [010](./bugs/010-lookup-guard-calibration.md) | LookupGuard 校准 | 已修复 |
| [011](./bugs/011-autocompact-token-threshold.md) | Auto-compact 过早 | 已修复 |
| [012](./bugs/012-pie-menus-lookup-throttle.md) | Pie Menus 联网护栏过紧 | 已修复+验过 |
| [013](./bugs/013-append-only-history-cache.md) | 工具历史改写破坏缓存前缀 | 已修复+回归通过 |
| [014](./bugs/014-long-turn-api-stall.md) | 长任务等待模型时 TUI 假死 | 已定位，延期修复 |

五条线的关系见 [bugs/README.md](./bugs/README.md) 里的关系图（含 006 终答可见）。

---

## 文档约定

每条 bug 记录尽量包含：

1. **现象** — 用户看到什么  
2. **根因** — harness 哪一层出了问题  
3. **已改** — 代码锚点与行为变化  
4. **仍观察** — 未承诺、有痛点再做  

状态用语：`已实施` / `部分缓解` / `仍观察`（避免写死 Phase 路线图）。

---

## 运行时数据（与文档交叉引用）

```text
.project/
  active_session.json
  sessions/<id>/
    session.jsonl      # 对话
    session.meta.json  # 标题 / 时间
    todos.json         # 本会话 todo（A 层）
  state.json           # 长任务档案（B 层，论文章节等）
  usage/               # token 流水
```

- **A（会话 + todos）**：跟 session；`/clear` 结束当前 id，旧目录保留。  
- **B（state.json）**：单槽长任务；`/resume project` 显式注入；`/clear` 默认删除。

详见 [003-resume-opt-in.md](./bugs/003-resume-opt-in.md)。
