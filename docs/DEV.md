# Flame 开发文档

实现真源。skill 安装见 [`SKILLS.md`](SKILLS.md)。

## 1. 目标与完成标准

**目标：** `flame run "…"` 一次跑完。内部 **plan → act → verify**；verify 唯一终裁。预处理 / plan / verify 可降级；act **超时**把半成品交 verify；仅 act **非超时失败**硬错误。stderr 报进展，stdout 交最终答复。

**完成标准：**

1. 假 `agent`（`tests/fake_agent.py`）跑通全档 effort 与降级路径
2. 本机已登录 Cursor CLI 时，`flame run` 能拉起 `agent -p --model auto` 并对 workspace 做事

## 2. 架构

阶段顺序写在 `loop.py`。每个阶段 **fork 一次** `agent`；靠 `.flame/` 文件交接，不 `--resume`。超时杀进程时使用进程组（`killpg`），避免子 shell / orchestrator 残留。

```
original.md
  → preprocess（fast 跳过）
       standard/high：quadrants → factors → brief.json
       max：meld → quadrants → factors → brief.json
  → loop
       plan.json → act → verify.json
       passed → stdout = 本轮 act 文本
       retry=false → stdout = diagnosis
       verify 降级 / fast 一轮未过 → stdout = act 文本
       act 超时 → act_status.json + 半成品交 verify（可 retry）
       retry → 下一轮 plan（不再 preprocess）
```

强制规则：

1. act 必须能跑到
2. 只有 `points_met ∧ aligned ∧ evidence_ok` 才算通过
3. act 非超时失败 → `FlameError`；act 超时 → 半成品交 verify（可 retry）

## 3. 目录

```
flame/
  docs/DEV.md
  docs/SKILLS.md
  README.md
  LICENSE
  todo.md            # 开放 backlog（低优项）
  pyproject.toml
  container/         # flame-worker Docker 镜像
  src/
    flame/           # 单机 plan-act-verify 编排
      loop.py        # 主循环；plan.goal 强制；max 开图 init
      evidence.py    # tool trace + checks 句柄审计
      preprocess.py  # meld / quadrants / factors → brief.json
      prompts.py
      budget.py      # effort → 模块开关 + cycle_limit
      backend.py     # spawn agent + stream-json + timeout
      types.py       # Brief / Plan / VerifyResult
      skills.py      # j-space / fact-graph 路径
      stage_summary.py
      data/fact-graph/
        ui/          # Cytoscape 黑板 UI（本地 serve.py / 静态导入）
  tests/
    fake_agent.py
```

本地试跑目录 `runs/` 不入库。单机 `flame`：Python 3.11+ **仅标准库** + 本机 `agent`。

集群编排 **Flame Fleet** 在独立私有仓库（`flame-fleet`），不在本库。

工作区 `.flame/` 常见产物：`original.md`、`brief.json`、`plan.json`、`verify.json`、`act_skill.json`、`tool_trace.json`；max 另有 `graph_seed.json`、`graph_run.json`。图本身在 `.fact-graph/runs/flame-act-cN/`。

## 4. 拉起 agent

```
agent -p
     --model auto
     --output-format stream-json
     --stream-partial-output
     --workspace <workspace>
     --trust
     [--force]            # plan / act / verify
     [--mode ask]         # preprocess: meld / quadrants / factors
     --
     <prompt>
```

| 阶段 | mode | --force | 产物 |
|---|---|---|---|
| meld（max） | ask | 否 | `meld-judge.json`；写入 brief.judge |
| quadrants | ask | 否 | 四格表 → brief |
| factors | ask | 否 | success/failure ≤3 + decisive_move |
| plan | 默认 | 是 | `plan.json`；失败则 stub |
| act | 默认 | 是 | 仓库改动；`act_skill.json` |
| verify | 默认 | 是 | `verify.json`；无 JSON 则降级交付 act |

## 5. Effort

| effort | preprocess | meld | act skills | 循环 |
|---|---|---|---|---|
| fast | 跳过 | 否 | 否 | 一轮 verify 后交付 |
| standard | 四格表→因素 | 否 | 否 | verify 收；`FLAME_MAX_CYCLES` 默认 8 |
| high | 同 standard | 否 | j-space（plan.`use_ledger`，默认 true） | 同 standard |
| max | 同 high | 是 | fact-graph（固定） | 同 standard |

兼容别名：`low`→`fast`，`medium`→`standard`。

## 6. 数据模型

```python
class Effort(StrEnum):
    fast = "fast"
    standard = "standard"
    high = "high"
    max = "max"

@dataclass
class Brief:
    judge: dict | None
    quadrants: dict   # known_knowns / known_unknowns / unknown_knowns / unknown_unknowns
    success_factors: list[str]  # ≤3
    failure_factors: list[str]  # ≤3
    decisive_move: str          # known unknown，不是 unknown unknown

@dataclass
class Plan:
    goal: str                 # 恒等于 original（harness 强制）
    approach: str
    constraints: list[str]
    verify_points: list[str]
    use_ledger: bool | None   # high only; default True
    degraded: bool
```

短期记忆 = `original.md` + 可选 `brief.json` / `meld-judge.json` + 本轮 `plan.json` / `verify.json` + 上一轮 verify 报告。无跨会话存储（见 `todo.md` 低优项）。

## 7. 协议

prompt 第一行：`[Flame phase: meld|quadrants|factors|plan|act|verify]`。
非 act 阶段一律带 Skill ban（禁止读 j-space / fact-graph / 任意 `SKILL.md`）；仅 act 在 effort 挂 skill 时加载。无 skill 的 act 同样禁止自开 skill。

1. **preprocess**：只写 `brief.json`。任一步失败则该项留空；全空则无 brief。不问用户。
2. **plan**：冲突优先级 **original > verify > brief**。brief 渲染为标签字段（decisive_move / factors / quadrants / meld_judge），不是整包 JSON。缺 brief 照样规划。**`plan.goal` 恒等于 original**（harness 强制覆盖，禁止代理成功态）。high 写 `use_ledger`（默认 true；短任务或明显多路径可 false）。
3. **act**：按 original(=goal) / approach / constraints。**high** 且 `use_ledger` → j-space；**max** → fact-graph。harness 写 `.flame/graph_seed.json` 并 `init` 开板（act 只 `run`）：`goal`=original+verify_points，`constraints`=plan.constraints，`origin`=brief+上轮 verify，`hint`=approach。图探索 / Flame verify 质检；board `complete` ≠ Flame 通过。Act 可写工作区与 `act.json` / `answer.md`，不得改 `plan.json` / `verify.json`。
4. **verify**：只看 original + verify_points（及 harness 注入的 act 超时说明与本轮 tool 句柄摘要）。通过 → 交付 act 文本。`retry=false` → 交付 diagnosis。无 JSON → 交付 act。Harness 对阶段 JSON 做**格式硬检查**（必要字段与类型；禁止多余键。执行结束后若 `plan.json` / `verify.json` 被 act 改动，恢复定稿并记入 evidence；内容未变则不写盘）。Harness 对 `checks` 做**证据审计**（`evidence.py`：每项 check 须含显式 `path:` / `url:` / `` `command` ``；path 做 workspace 归一化；须存在且本轮 tool 触及；url/command 只认 trace，不出站）。另要求本轮 `answer.md`（或 `.flame/answer.md`）的修改时间不早于本轮计划阶段写下的 `plan.json` 快照（界面只展示该文件；不要用盘面现时 mtime 当验收点）。审计是 verify 的一环：三项都落定后才采纳 `retry`；仅证据腿失败时强制 `retry=true`。`points_met` 且 `checks` 空 → 亦 `evidence_ok=false`。

## 8. 降级与退出码

| 情况 | stdout | passed | CLI |
|---|---|---|---|
| verify 通过 | 最后一轮 act | true | 0 |
| retry=false | diagnosis | false | 3 |
| verify 无 JSON / fast 一轮未过 | 本轮 act | false | 3 |
| safety 触顶 | diagnosis 或 act | false | 3 |
| act 超时 | 半成品 → verify（可再 plan） | 视 verify | 视 verify |
| act 非超时失败 | stderr | 抛错 | 1 |
| 安全拒绝 | stderr | — | 2 |

## 9. Skills

j-space 不打包；探测与安装见 [`SKILLS.md`](SKILLS.md)。fact-graph 在 `src/flame/data/fact-graph/`。

max 的 meld：本机 `agent --model auto --mode ask`，三 Panel 并行 + Judge，不调外部 Fusion HTTP。

## 10. 进展与日志

stderr：`▶ phase`、tool 摘要、截断 assistant、结论（终端默认可见）。  
stdout：最终答复（通过多为最后一轮 act；`retry=false` 为 diagnosis）。  
JSONL：`.flame/logs/<session>.jsonl`（`start` / `phase` / `agent_done` / `act_timeout` / `finish` 等）。

## 11. 安全

默认关。`FLAME_SAFETY=1` 或 `--safety` 打开关键词门；命中退出码 2。拒答策略否则交给 agent LLM。

## 12. CLI / API

```
flame run "任务" [--effort standard] [--workspace .] [--model auto] [--agent-bin agent] [--safety]
flame continue "后续指令" [--workspace .] [--model auto] [--extra-budget 900]
  # max only: read `.flame/graph_run.json`, orchestrator hint+run, then verify (skip preprocess/plan/init/act agent)
flame skills
flame version
```

```python
from flame import run
result = run("修复测试", workspace=".", effort="standard")
```

## 13. Flame Fleet

集群（SQLite + FastAPI UI + docker node 调度）已拆到 **私有仓库 `flame-fleet`**，依赖本库 `flame` 包与 `container/Dockerfile` 构建的 worker 镜像。协议、UI、graph API 见该库 `docs/FLEET.md`。
