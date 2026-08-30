---
name: fact-graph
description: 黑板架构(事实-意图图)多智能体协同推理。当用户要求用 fact-graph / 黑板模式 / 多agent协同推理来解决一个起点明确、终点明确但路径未知的复杂问题(深度分析、方案探索、假设验证、调研证明等)时使用。Spawns multiple Cursor CLI (`agent`) workers coordinated through a shared fact-intent graph (blackboard architecture).
---

# fact-graph — 黑板架构多智能体协同推理

把一个大问题建模为从 `origin` 到 `goal` 的有向探索：**Fact**(已确认结论，只增不改)、**Intent**(待探索方向)、**Hint**(外部注入的建议)。多个 **Cursor CLI** (`agent`) worker 进程并行工作，由编排器(`scripts/orchestrator.py`)统一调度：worker 只接收 prompt 并返回结构化 JSON，编排器是唯一写板人。

本 skill 已适配当前 Cursor 环境：本机 CLI 为 `agent`（`~/.local/bin/agent`），不再调用 Claude Code (`claude`)。

三类任务：`bootstrap`(初始态一次直推，可选)、`reason`(读全图判断：完成/提出新 intent/不动)、`explore`(执行一条 intent 产出一个 fact)。超时的 bootstrap/explore 会用同一 Cursor chat (`--resume`) 进入"只总结不推进"的二阶段收尾。

## 适用与不适用

- 适用：需要多轮探索、多条路径试错、结论需要证据链的问题。
- 不适用：一两轮对话能答完的问题；没有明确 goal 的闲聊。

## 操作流程(主会话 = 控制面与人机接口)

1. **明确问题**。与用户确认字段：`origin`（开图前**现状**/已知情境）、`goal`（解题目标，须可判定）、`constraints`（硬约束）、`hint`（策略建议）、预算、是否启用 bootstrap、用哪些模型 worker。  
   **Flame graph 开图约定（harness 组装）：** harness 写 `.flame/graph_seed.json` 并 `init --seed`；act 只 `run`。`goal` = 用户 original + verify_points；`constraints` = plan.constraints；`origin` = preprocess brief +（retry 时）上轮 verify 诊断；`hint` = plan.approach。standalone 用法仍可 `--origin/--goal` 等自行填写。constraints 写验收命令、合格/不合格判据、禁区；hint 不能覆盖 constraints。reason/bootstrap 每轮对照 constraints。
2. **初始化 run**。每个 run 一个目录，目录路径即 run 标识：

   ```bash
   RUN_DIR=.fact-graph/runs/$(date +%Y%m%d-%H%M%S)-<问题slug>
   python3 <SKILL_DIR>/scripts/orchestrator.py init --run-dir "$RUN_DIR" \
     --title "<标题>" --origin "<起点描述>" --goal "<终点判据>" \
     --constraints "<硬约束，可选>" --hint "<初始参考，如 P1P2>"
   # 长文本用 --origin-file/--goal-file/--constraints-file/--hint-file; 禁用 bootstrap 加 --no-bootstrap
   ```

3. **配置 worker**。init 默认写入 reasoner + explorer，两者 `CURSOR_MODEL = "auto"`（继承本机 `agent` 登录态）。若要改模型或加 worker，把 `config.example.toml` 复制为 `$RUN_DIR/config.toml` 再编辑：`[[worker]]` 的 `env` 设置 `CURSOR_MODEL` / `CURSOR_API_KEY`（支持 `${VAR}` 插值，API key 不落明文）。`command = "claude"` 会自动映射为 `agent`。一个 worker = 一个独立并发配额。配置在 init/run 时自动校验。
4. **后台启动编排器**：

   ```bash
   python3 <SKILL_DIR>/scripts/orchestrator.py run --run-dir "$RUN_DIR"
   ```

   用后台方式运行(run_in_background),不要前台阻塞等待。
5. **监控与汇报**。定期读取 `$RUN_DIR/status.json`(每拍刷新：事实数/在探意图/运行中任务/预算消耗)和 `events.jsonl`,向用户做简要进度汇报。不要刷屏；只在事实数变化、意图结论、run 结束时汇报。
6. **转发用户 hint**。用户随时可能补充判断，立即写入：

   ```bash
   python3 <SKILL_DIR>/scripts/orchestrator.py hint --run-dir "$RUN_DIR" --content "<内容>"
   ```

   hint 会在下一拍被吸收入图，影响后续 reason/explore。
7. **收尾**。编排器退出后读 `$RUN_DIR/RESULT.md` 和 `board.json`,向用户输出最终结论：完成结论 + 支撑 fact 链(含 id 与因果路径);若 budget_exhausted,总结已确认事实、已排除方向、未结论意图和建议的下一步。

## 中断与恢复

- 停止：杀掉编排器进程(或它收到 SIGTERM 自动收尾，状态 `stopped`)。
- 恢复：`orchestrator.py run --run-dir <旧目录>`。board.json 自包含，残留的已认领 intent 会被自动释放后重新调度；`stopped`/`budget_exhausted` 自动转回 active 续跑，`completed` 不可重入。
- 预算用尽可能：`run --run-dir <旧目录> --extra-budget 900`。

## 规则与注意

- **worker 默认带 `--force --trust`**（Cursor CLI 的 Run Everything，等价于旧版 `--dangerously-skip-permissions`）。它们在 `$RUN_DIR` 配置的运行目录(`runtime.cwd`,默认编排器启动目录)里拥有完整工具权限。提醒用户这一点；谨慎场景在 config 里设 `skip_permissions = false`（启用 `--sandbox enabled`）。Cursor CLI 没有 `--allowedTools`。
- 编排器通过 `agent create-chat` 预创建会话，随后 `agent -p --output-format json --resume <chatId> -- ...`。二阶段收尾续用同一 chat。
- 认证：优先使用本机 `agent login`；也可用环境变量 `CURSOR_API_KEY`。旧配置里的 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 会分别映射到 `CURSOR_API_KEY` / `--model`。
- 编排器是唯一写板人；主会话**不要**直接编辑 `board.json`,只通过 `hint` 子命令注入。
- token 消耗与 worker 数、轮次成正比；启动前与用户确认预算护栏(`max_reason_rounds`/`max_facts`/`wallclock_budget`)。
- 多个 run 可并行(各自独立目录与编排器进程),但注意 LLM 配额叠加。
- 含 API key 的 `config.toml` 不要提交 git;`.fact-graph/` 建议加入 gitignore。
- 零 token 自测：`prompt_group = "mock"` + mock worker(见 config.example.toml 尾部),可验证调度、二阶段收尾与护栏路径。

## Web UI（展示黑板图）

`ui/` 是只读的浏览器端展示（另有一项写操作：hint 注入）。两种用法：

- **静态模式**：直接双击 `ui/index.html`（file:// 即可用）。拖拽 `board.json` 或选择整个 run 目录（可顺带读入 events.jsonl / RESULT.md / config.toml）。
- **服务器模式**（推荐配合正在跑的 run）：`python3 ui/serve.py [--root <runs根目录>] [--port 8720]`，自动列出 runs 目录、5 秒轮询刷新、支持 hint 注入（POST 追加 inbox.jsonl）。仅本机监听。

UI 功能：Cytoscape 图（6 种布局、增量更新保留视口、completion 合成到 goal 的完成边、budget_exceeded 徽章）、侧栏详情/时间线（events.jsonl，缺省由 board 推导）/回放/结果/配置、状态横幅（facts/intents/reason 轮次）。

**注意**：UI 是只读 + hint 注入；修改 board 或控制 run 仍走编排器子命令（唯一写板人不变式）。

## 文件布局

```
<SKILL_DIR>/
├── SKILL.md                  # 本文件
├── scripts/orchestrator.py   # 编排器(纯 stdlib, Python>=3.11)
├── prompts/default/          # 场景中性的 5 份任务 prompt
├── prompts/mock/             # mock 组(JSON 信封, 自测用)
├── ui/                       # Web UI: index.html + serve.py + vendor/
└── config.example.toml       # 多模型 worker 配置示例
```
