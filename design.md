# Flame 设计说明

Flame 是一层薄编排器：用本机 Cursor CLI `agent` 跑完任务，外层只负责阶段顺序、产物交接、努力档位和降级地板。模型不是 HTTP LLM，也不是 Cursor SDK。

## 目标

- `flame run "…"` 一次跑完；**stderr** 报进展，**stdout** 交最终答复
- 闭环是 **plan → act → verify**；verify 是唯一终裁
- 预处理 / plan / verify 都可以降级；act **超时**把半成品交 verify（可再 plan）
- 只有 act **非超时失败**才硬错误（`FlameError`）

## 非目标

- 自研工具栈、OpenAI / Anthropic / Cursor SDK 客户端
- Docker 沙箱、独立安全分类模型、跨会话记忆、Web UI、REPL
- 阶段之间 `--resume` 同一会话（每次 fork 一次 agent，靠 `.flame/` 文件交接）

## 主路径

```
用户任务 → original.md
  → [可选] SafetyGate
  → preprocess（按 effort；失败则丢 brief）
  → loop：plan → act → verify
       verify.passed → 交付最后一轮 act 文本
       retry=false → 交付 diagnosis（不可行说明）
       verify 无法判断 / fast 一轮未过 → 交付本轮 act
       act 超时 → 半成品交 verify（通常可 retry）
       retry=true（非 fast）→ 下一轮 plan（不再跑 preprocess）
```

### Preprocess（一次）

| effort | 做什么 |
|---|---|
| fast | 跳过 |
| standard / high | 四格表 → 成功/失败因素与胜负手 → `brief.json` |
| max | meld（3 panel + judge）→ 同上 |

四格表里的 **unknown unknowns** 对应 Anthropic Blind Spot Pass 的意图。  
**胜负手（decisive_move）** 是 known unknown 的调度选择，不是 unknown unknown。

### Plan

产出两份合同，写入 `plan.json`：

- 给 act：`goal` / `approach` / `constraints` / `search`（仅 high/max）
- 给 verify：`verify_points`（harness 另附 original）

冲突优先级：**original > verify > brief**。brief 按字段渲染进 prompt，不是整包 JSON 倾销。

high/max 的 `search`：**先写本轮 approach，再对该 approach 做三问多数表决**（不是对整段 original）。宽/比较/怕漏 → `breadth`；深/验证/怕断 → `depth`。每轮 replan 都会重选。

### Act / Verify

- act 按 goal / approach / constraints 落地；high/max 且 `search=depth` → j-space，`breadth` → fact-graph
- 非 act 阶段带 Skill ban（禁止自开 Cursor skill）；仅 act 在 Flame 挂载时读 skill
- verify 对照 original + verify_points（超时当轮另附 harness 说明）；通过则 stdout = 最后一轮 act 文本
- act 触达 `FLAME_TIMEOUT_SEC`：杀进程组、写 `act_status.json`、进 verify，不整次硬失败

## Effort

effort **开关模块与节奏**，不再按档位限制循环次数（除 fast）：

| effort | preprocess | meld | act skills | 循环 |
|---|---|---|---|---|
| fast | 否 | 否 | 否 | 最多一轮 verify，然后交付 |
| standard | 四格表+因素 | 否 | 否 | verify 收；`FLAME_MAX_CYCLES` 防 runaway |
| high | 同 standard | 否 | j-space / fact-graph | 同 standard |
| max | 同 high | 是 | 同 high | 同 standard |

## 降级地板

| 阶段 | 失败时 |
|---|---|
| preprocess | 无 brief，plan 吃 original |
| plan | stub 计划（`degraded`），仍进 act |
| verify 无 JSON | 交付本轮 act，`passed=false` |
| verify `retry=false` | 交付 diagnosis |
| act 超时（`FLAME_TIMEOUT_SEC`） | 写 `act_status.json`，半成品交 verify；verify 可 retry |
| act 其它失败 | `FlameError` |

## 与早期草案的关系

早期草案含 scout/refuter/Docker/长期记忆等。Flame 落地时裁掉了那些，只保留：沿验证边界规划、最小失败检查、失败带诊断回规划、努力档位。本文件是 **当前实现的设计真源**；实现细节见 `docs/DEV.md`。
