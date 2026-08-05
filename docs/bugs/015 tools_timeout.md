# Agent 卡住问题完整复盘：从 12 次超时到 0 次

> 日期：2026-08-03
> 范围：improved_harness 自研 agent 的"工具超时 + 盲目重试"问题
> 状态：已解决并验证（12 次超时 → 0 次，20+ 分钟 → 2~3 分钟）

---

## 1. 问题概述

用户的自研 agent（基于 shareAI-lab/learn-claude-code 的 improved_harness）在"连接远程 SSH 查 U 盘"这类任务上频繁卡住：一个有效工作不到 1 分钟的任务，实际耗时 20~27 分钟，期间工具超时 12 次、换姿势重试 9 种，全部空转。问题在多个会话中反复出现（审查会话、微波任务会话、"你好"短会话均受影响），用户感知为"一直卡住不动"。

量化对比（同一任务，改进前后）：

| 指标 | 改进前 | 改进后 |
|---|---|---|
| 工具超时次数 | 12 次 | 0 次 |
| 换姿势重试 | 9 种 | 0 种 |
| 总耗时 | 20~27 分钟 | 2~3 分钟 |
| 结果质量 | 残缺、反复确认 | 完整清单 |

---

## 2. 具体案例：微波任务会话完整时间线

会话：`.project/sessions/1785667265_99d62f8f`（查远端 10.15.1.169 是否有 U 盘）

任务本身很简单：SSH 连上一台 CentOS 7 服务器，跑 `lsblk` 看有没有 U 盘。第一次连接其实几秒就成功了（后台 bg_0001），但因为输出被截断（见第 3 节），agent 以为没完成，于是开始了 12 次超时的连环撞墙：

| # | 命令 | 超时 | 卡在哪 |
|---|---|---|---|
| 1 | `python check_usb.py` 前台 | 90s | SSH 握手/命令读挂起 |
| 2 | `python -c paramiko 连接` 前台 | 60s | SSH 握手挂起 |
| 3 | `python ssh_test.py` 前台 | 120s | SSH 握手挂起 |
| 4 | `python -c` 轮询循环（多行） | 150s | cmd 下多行 -c 被拆断 |
| 5 | `python check_usb.py` 前台重跑 | 300s | SSH 握手挂起 |
| 6 | `python -c` 轮询循环（多行） | 180s | cmd 下多行 -c 被拆断 |
| 7 | `python -c "time.sleep(30)"` | 60s | 环境异常（偶发） |
| 8 | `python min_lsblk.py` 前台 | 120s | SSH 握手挂起 |
| 9 | `python -c socket recv` | 30s | `recv()` 无超时 |
| 10 | `python -c` socket 重试（多行） | 90s | cmd 下多行 -c 被拆断 |
| 11 | `python -c "time.sleep(45)"` | 90s | 环境异常（偶发） |
| 12 | `python -c` 轮询循环（多行） | 180s | cmd 下多行 -c 被拆断 |

（另有后台任务 bg_0002~bg_0005 全部以 `Error: Timeout (120s)` 结束，未计入 12 次。）

换姿势的 9 种策略：前台 → 后台 → 加大超时（300s）→ 重写脚本（check_usb → ssh_test → check_usb2 → min_lsblk）→ socket 探活 → ping → 后台重跑。每次推理单看都对，但真正的墙是"SSH 握手时好时坏"，agent 始终没意识到，于是所有姿势都白搭。

---

## 3. 为什么明明有答案了，却没有记录

这是最反直觉的一环：**第 2 次尝试其实成功了**（后台 bg_0001 拿到了 lsblk 开头），但结果被丢弃了。根因在代码里，`harness/agent/background.py` 第 115 行：

```python
summary = output[:200] if len(output) > 200 else output
```

后台任务完成通知的 summary 被硬编码只取前 200 字符。那次 `check_usb.py` 输出很长（lsblk + 挂载 + lsusb + dmesg 一大串），200 字符刚好只够显示到 `├─sda2`，后面的 `sdb 233.1G usb Sandisk`（也就是答案本体）被切掉了。

更关键的是：**完整结果一直存在内存里**（`background_results[bg_id]`），只是组装通知时被 200 字符截断，且没有落盘、没有文件路径提示。agent 看到残缺的 summary，逻辑上只能推断"任务没完成"，于是继续换姿势撞墙——截断直接误导了 agent 的决策。

同类问题还有一层：超时被杀时，进程的重定向输出（`> file 2>&1`）会因 `TerminateJobObject` 杀进程树而丢失 print 缓冲，留下 0 字节文件，agent 同样看不到任何中间产物。

---

## 4. 为什么明明失败了，还要换姿势重试

三个原因叠加，让"失败"变成"盲重试循环"：

### 4.1 超时错误信息贫乏（主因）

旧实现（`harness/tools/filesystem.py`）超时只返回一句固定文案：

```
Error: Timeout (120s). The command did not finish within the timeout.
If it is expected to take longer, retry with a larger `timeout` ...
```

它没告诉模型三件事：卡在哪个阶段（连接中还是执行中？）、死前最后看到了什么输出、有没有中间产物。模型只能瞎猜，于是每次失败都"合理推理"出一个新姿势，实际上是在同一个墙（SSH 握手不稳）上反复撞。9 种姿势就是这么来的。

### 4.2 自踩已知坑：多行 python -c 在 cmd 下被拆断

`ai_tools/01_连接远端服务器.md` 第 64 行自己就写了"多行 python -c 在 cmd 下会被拆断，不要用"，但 agent 在 4 处轮询脚本里都用了多行 `python -c "..."`（换行分隔）。cmd 把换行当成新命令处理，脚本行为异常直接挂起，于是连"等结果"的轮询脚本自己也超时。约束写在了工具文档里，但没写进 System Prompt，agent 不遵守。

### 4.3 被杀进程留半开 TCP → 越重试越慢（恶性循环）

每次 `TerminateJobObject` 杀进程树时，TCP 连接没有正常 FIN，半开连接在服务器端堆积。sshd backlog 满了之后，后续握手越来越慢——这解释了最诡异的"第一次秒成、后面全挂"。重试本身在制造新的故障源。

---

## 5. 我们怎么解决的

三个改动，全部落地并验证：

### 改动 1：后台通知摘要分层（`harness/agent/background.py`）

- 摘要从硬编码 200 字符 → 2000 字符（够 agent 快速判断）
- 通知新增 `exit_code` 和 `output_chars` 字段，agent 能判断任务到底成功没有

```python
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <exit_code>0</exit_code>
  <summary>...（前 2000 字符）...</summary>
  <output_chars>18432</output_chars>
</task_notification>
```

### 改动 2：超时错误结构化诊断（`harness/tools/filesystem.py`）

新增 `_timeout_diagnosis()`，把一句固定文案升级为三段式诊断：

```
Error: Timeout (120s, elapsed 121s). 超时前无输出进展。
  phase: starting —— 命令未产生任何输出，大概率卡在启动或连接阶段（如 ssh/scp 握手）
  last_output: (none)
  建议：
  - 若属网络/连接类命令（ssh/scp/ping/下载），先探测目标连通性（如 ping、nc）再重试，不要盲目重跑
  - 若命令确实需要更久，用更大的 timeout（毫秒，上限 3600000）或 run_in_background: true
  - 禁止多行 python -c：Windows cmd 会拆断换行导致挂起，请写入临时 .py 文件再执行
```

`phase` 告诉模型卡在哪一步（starting=连接/启动、executing=执行中），`last_output` 给出死前最后输出，`建议` 直接给出对症动作，杜绝盲重试。

### 改动 3：超时前探测，分级处置（`harness/tools/filesystem.py`）

新增 `_wait_with_escalation()`，把"到点一刀切杀掉"升级为"先探测再决策"：

- 进程已退出 → 正常返回退出码
- 到超时点但输出仍在增长（近期有进展）→ 自动延长（最多 2 次，每次不超过 60s）
- 到超时点且从未输出/长期静默 → 判定超时，带结构化诊断
- 超时后杀进程 → 保留已收集输出用于诊断

开发过程中发现并修复了一个逻辑缺陷：初版把"从未输出的静默进程"误判为"有进展"而延长（因为 `last_output_at` 初始值设为 start）。修复后只有真正产生过新输出才算有进展，静默卡死准时 3s 判定超时。

---

## 6. 其他家 agent 是怎么做的

调研了 Hermes、pi、OpenClaw、Claude Code 四个框架在"工具超时、重试循环、挂起"上的实际做法：

### 6.1 Hermes（NousResearch）：工具循环护栏

独立 `agent/tool_guardrails.py`（约 500 行），按三类条件计数，超过阈值就注入提示或强制收口：

| 检测类型 | 条件 | 警告阈值 | 硬阻断阈值 |
|---|---|---|---|
| exact_failure | 完全相同参数 + 相同错误结果 | 2 次 | 5 次 |
| same_tool_failure | 同工具不同参数仍失败 | 3 次 | 8 次 |
| no_progress | 连续只用只读工具无进展 | 2 次 | 5 次 |

行为分三级：ALLOW 静默通过 → WARN 注入系统消息提示"换个策略" → HALT 强制 LLM 给最终答案。另有 `iteration_budget.py` 迭代预算（父 agent 90 次、子 agent 50 次），用完只注入软提示。

### 6.2 pi（sst 系）：别碰 Bash

System Prompt 明令"Do NOT use Bash to run commands when a relevant dedicated tool is provided"。Bash schema 只有 `command` 和可选 `timeout` 两个字段，无默认超时，让 LLM 按命令性质自己决定等多久。输出处理用 truncateTail（保留末尾，因为错误和 Summary 总在最后）。

### 6.3 OpenClaw：事件流 + 硬超时

`runEmbeddedPiAgent` 带 `timeoutMs`（默认 600 秒）、`abortSignal`（外部可取消）、`bashElevated`（危险命令提升审批）。工具执行全程发 `tool start/update/end` 事件，进度实时可见，而不是黑盒等结果。

### 6.4 Claude Code：权限模式 + 后台任务

用 default（每次确认）/ acceptEdits（自动接受编辑）/ plan（只规划不动手）三档权限，后台任务机制配合环境变量可禁用。

---

## 7. 借鉴了什么，怎么映射到自己项目

| 别人的做法 | 借鉴点 | 落地到本项目 |
|---|---|---|
| Hermes 的 same_tool_failure 计数 | 同一工具连续失败 N 次应强制收口 | 本项目有 RepeatGuard 但拦不住"换参数重试"变体；改 2 的结构化诊断先让模型不再盲目换姿势，护栏升级留作后续 |
| pi 的"约束进 System Prompt" | 工具文档里的坑，agent 不读就不遵守 | cmd 多行坑已写进改 2 的超时诊断建议，让模型在失败瞬间直接看到 |
| OpenClaw 的进度可见 | 工具卡住时用户/模型应该知道卡在哪 | 改 2 的 phase + last_output、改 3 的超时前探测，都是"进度透明化"的具体化 |
| OpenClaw 的 abortSignal | 外部可取消的阻塞调用 | 未落地（bug 014 记载的 Stop 无法中断问题仍待处理） |
| pi 的 truncateTail | 命令输出保留末尾关键信息 | 改 1 摘要从 200→2000 字符，配合落盘可按需读全量 |

一句话总结借鉴方向：**别的框架的答案不是"让重试更快"，而是"让 agent 根本不敢反复撞墙"——要么用护栏硬性计数拦截，要么把 Bash 贬为最后手段，要么让失败信息足够具体让模型一次做对。**

---

## 8. 最后解决了什么问题

### 8.1 已解决

1. **"有答案却丢"**：后台通知摘要 200→2000 字符 + exit_code + output_chars，agent 不再因为信息残缺误判"任务没完成"
2. **"失败就盲重试"**：超时错误带 phase / last_output / 对症建议，模型知道卡在哪、该做什么，不再换 9 种姿势撞同一个墙
3. **"到点一刀切"**：超时前先探测，有进展自动延长（最多 2 次），静默卡死才判定超时
4. **端到端实测**：同一 SSH 查 U 盘任务，0 次超时、0 次盲重试、2~3 分钟完成，结果完整（5 站点 MWR/SONDA 数据 + 战点数据清单）

### 8.2 遗留事项

1. RepeatGuard 的"同工具连续失败 N 次强制收口"尚未落地（Hermes 的 same_tool_failure 机制）
2. bug 014 的"阻塞中 Stop 无法中断"（abortSignal 等价物）未处理
3. 远端 sshd 的 `UseDNS no` / `GSSAPIAuthentication no` 未配置（握手慢的根源之一）
4. `del *` 权限仍是 ask，agent 清理临时文件会被拦，需用户决定是否放开

### 8.3 数据对比（同一任务）

| 指标 | 改进前 | 改进后 |
|---|---|---|
| 工具超时次数 | 12 次 | 0 次 |
| 换姿势重试 | 9 种 | 0 种 |
| 总耗时 | 20~27 分钟 | 2~3 分钟 |

---

## 相关文件

- 会话日志：`.project/sessions/1785667265_99d62f8f/session.jsonl`
- 修改文件：`harness/tools/filesystem.py`（`_wait_with_escalation` / `_timeout_diagnosis` / `run_bash` / `run_bash_streaming`）、`harness/agent/background.py`（通知摘要）
- 备份：`filesystem.py.bak` / `background.py.bak`（临时工作目录）
- 测试：单元测试 4 场景全过 + 端到端实测
- 简版记录：`agent_卡住问题记录.md`（本文件为完整版）
