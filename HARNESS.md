# improved_harness

Agent harness（基于 shareAI-lab/learn-claude-code s20 拆包改进）。CLI 入口 `main.py`，核心在 `harness/` 包。

## Start Here

- 项目总览与历史：`README.md`
- 模块职责 / 数据落盘：`ARCHITECTURE.md`
- 文档索引：`docs/README.md`
- Harness 可靠性改进总计划：`docs/harness-reliability-plan.md`
- 两个入口：**TUI（推荐，日常）** = `python main.py --event-stream` 后端 + `node_tui\run.bat` 前端；**CLI** = `python main.py`

## Commands

- 安装依赖：`pip install -r requirements.txt`（后端）；`cd node_tui && npm install`（前端）
- **TUI 入口（推荐，日常使用）**：
  - `node_tui\run.bat`
- 命令行入口：`python main.py`（Rich 行式 CLI）
- **自主目标（L6）**：`/goal --verify "pytest -q" -- <目标>` 启动；
  `/goal status|pause|resume|cancel` 控制；状态落盘 `.project/goal.json`（见 `docs/goal-mode-mvp-spec.md`）
- 基础回归评测：`python -m evals`（`--live` 跑真 LLM 冒烟）
- 可靠性评测：`python -m evals.harness_reliability --task H001 --variant baseline --runs 1`
- 单元测试：`python -m pytest tests -q`
- 运行前注意：Windows 下 shell 工具里 python 需加 `< NUL` 防挂起（stdin 未关闭）；**但 goal 模式下禁止任何 shell 元字符（`<` `>` `|` `&` `&&`），会触发权限护栏**，见下节
- **当前按 Windows 开发**：bash 工具走 cmd.exe，用 `dir`/`findstr`/`type`/`del`，不要用 `ls`/`grep`/`cat`/`rm`（动态上下文每轮也会注入系统环境提示）

## Windows / cmd 踩坑记录（实战总结）

> 环境：Windows + cmd.exe（bash 工具走 cmd）。以下都是真实踩过的坑。

### 1. 命令不存在 / 目录参数报错

- cmd 里没有 `ls`/`cat`/`grep`/`rm`：用 `dir`/`type`/`findstr`/`del`
- `dir /s /b <目录>` 报 `Parameter format not correct`：目录名带空格时**加双引号** `dir /b "harness\permissions"`

### 2. findstr 对反斜杠路径极不稳定

- `findstr "harness\goal\runner.py"` 报 `Cannot open`；`findstr /s /i "x" harness\*.py` 报 `Bad command line`
- **不要用 findstr 搜带路径的文件**，改用：`read_file` 工具直接读，或写一次性 Python 脚本（`os.walk`+`open`）搜索

### 3. python 无输出/挂起：stdin 没关

- `python xxx.py` 卡住无输出 → 加 `< NUL`（`python xxx.py < NUL`）
- 但 `<` 会触发 goal 权限护栏（见坑 6），**goal 里不能加**

### 4. 禁止多行 `python -c`

- Windows cmd 会拆断换行导致挂起/输出被吞
- **多行逻辑一律写成 .py 文件再执行**，不要 `python -c`

### 5. goal 模式下 bash 命令带 shell 元字符被拒

```
Permission denied: bash needs human approval, but the goal runner is non-interactive
```

- `harness/permissions/engine.py`：bash 命令含 `& | > < \n \r` 之一 → 强制 `ask`；goal 的 ACT 非交互 → `permission_wait` 暂停
- **goal 里跑 bash 必须不带元字符**：不要 `cd xxx && python ...`，不要 `... < NUL`，不要 `| more`
- 用 `python scripts/xxx.py` 这种干净命令（脚本内部用 subprocess 处理多步）

### 6. subprocess 调 npm 报 FileNotFoundError

- Windows 上 `npm` 是 `npm.cmd`，`subprocess.run(["npm", ...])` 找不到
- **用 `npm.cmd`**（或 `shell=True`）；claude/codex 等同理

### 7. `del` 多个文件被权限拦

- goal 模式下 `del a.py b.py` 含空格被当复合命令拦
- 一次删一个，或用脚本删

### 速查表

| 想做什么 | 别用 | 用 |
|---|---|---|
| 列文件 | `ls` | `dir /b` |
| 读文件 | `cat` | `type` 或 read_file |
| 删文件 | `rm` | `del`（一次一个） |
| 搜代码 | `grep`/findstr+路径 | read_file 或 Python 脚本 |
| 跑多行 python | `python -c` | 写 .py 文件 |
| goal 里跑 bash | 带 `& \| > <` | 干净命令 |
| Windows 跑 npm | `npm` | `npm.cmd` |

## Hard Constraints

- 保持单一 `agent_loop` 作为主执行循环（`harness/loop.py`），新能力挂其上。
- 工具修改必须同步：schema（`harness/tools/registry.py`）+ handler + 权限（`config/permissions.json`）。
- 不绕过权限系统；危险命令走确认。
- 不通过修改已有测试来掩盖失败。
- 跨模块修改必须运行对应测试验证。
- 未通过验证不得声称任务完成。
- 评测 fixture 的隐藏检查（`evals/harness_reliability/checks/`）只验证行为契约，
  不锁死命名/返回结构，避免误杀合理实现。

## Task Routing

- 修改主循环 / 会话：读 `ARCHITECTURE.md` + `harness/loop.py`（必读）
- 修改工具 / 权限：读 `docs/tools.md` + `config/permissions.json`（必读）
- 修改评测：读 `docs/evals.md` + `docs/harness-reliability-plan.md`（必读）
- 修改 UI / 渲染：读 `harness/ui/renderer.py` + `harness/ui/tool_display.py`
- 修改会话 / resume：读 `docs/bugs/003-resume-opt-in.md`
- 压缩 / compact：读 `docs/bugs/004-context-compaction.md`
- 查找 / lookup：读 `docs/bugs/010-lookup-guard-calibration.md`

## Definition Of Done

- 行为修改有对应测试，且相关测试通过（`python -m pytest tests -q`）。
- 回归评测通过（`python -m evals`）。
- 没有新增未说明的失败。
- 修改范围符合用户任务，不顺手改无关文件。
- 最终回复包含：改了什么文件、跑了什么验证命令、结果如何。
