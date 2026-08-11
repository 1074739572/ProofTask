# 010 — LookupGuard：硬失败烧全局 stale + block 不升级

**状态**：已修复（产品路径）  
**暴露场景**：GAIA L1 fish-bag（模式 B）+ Doctor Who（模式 D）

> Archive note: the described `lookup_guard.py` implementation was removed and is not wired into the current runtime. This document records prior behavior only; current lookup safety relies on permissions and `RepeatGuard`.

## 症状

1. **模式 B**：单个 PDF `404` / `robots` 被算进「连续 stale」，两次后全局禁网。换源（Wikipedia / 其它站）也被拦。
2. **模式 D**：block 文案暗示「用 memory / partial findings」；连续 block 只软拒，模型继续空转或瞎编。
3. **Pie Menus 近重复过紧**（细节与验题见 [012](./012-pie-menus-lookup-throttle.md)）：
   首搜 junk/空 → Jaccard → force → `unverified`；后又叠 soft-stale 拦 URL、次数硬顶卡在作者已确认。

## 根因

| 层 | 问题 |
|----|------|
| `classify` 缺失 | 403/404/robots 与「空结果 / 无关」共用 `consecutive_stale` |
| Block 文案 | 未强调「仅本会话已拉取证据」，易退回参数记忆 |
| Loop | block 后无升级；无 strip-tools 强制收口 |
| Near-dup | 失败查询也进 Jaccard 历史 → 失败后无法换词 |

## 修复（整条链路）

1. **`lookup_guard.py`**
   - `classify_fetch_result` → `ok` / `hard_fail` / `soft_stale`
   - `hard_fail`：只禁 URL（robots/403/429 再禁 host），**不**加 `consecutive_stale`
   - `soft_stale`：才累计全局 stale
   - block / escalate 文案：`THIS conversation` 证据；禁 memory 当事实
   - `note_block` / `finalize_latched`；`HARNESS_LOOKUP_BLOCK_ESCALATE`（默认 2）
   - `LOOKUP_FORCE_ANSWER` 共享文案
   - **Near-dup**：Jaccard 只对 *成功且主题相关* 的检索生效；失败/跑题 SERP 可换词；完全相同 query 仍禁
   - **Soft-stale**：只拦继续 `web_search`，**不拦**首次打开具体 URL（ACM / OpenAlex 换源）
   - GAIA / 日常默认**无** `web_fetch_limit` 硬顶；要硬顶时设 `HARNESS_LOOKUP_FETCH_LIMIT`

2. **`loop.py` + `recovery.py`**
   - 每次 block → `note_block()`；达阈值 → 注入 `LOOKUP_FORCE_ANSWER` + `strip_tools_until_answer`
   - 出文本答案后清 strip 标志

3. **测试**：`tests/test_lookup_guard.py`、`tests/test_lookup_guard_loop.py`

## 验证

```bash
pytest tests/test_lookup_guard.py tests/test_lookup_guard_loop.py -q
```

日常 CLI `lookup_mode` 与 GAIA 共用同一 guard，非评测专用补丁。
