# Act skills：部署与探测

拓扑由 **effort** 决定（不再有 plan `search` 表决）：

| effort | act skill | 是否打进 Flame 包 |
|---|---|---|
| fast / standard / meld | 无 | — |
| **ledger** | j-space（plan.`use_jspace`，默认 true；短任务或明显多路径可 false） | **否，需本机安装** |
| **graph** | fact-graph（固定；执行阶段不挂账本） | 是（`flame/data/fact-graph`） |

fact-graph 的 `complete` 不是 Flame 通过；verify 仍是终裁。图探索 / Flame 质检。

默认 fact-graph worker 使用 `CURSOR_MODEL=auto`（见包内 `config.example.toml` / init 模板）。

查当前机器解析到哪：

```bash
flame skills
```

`j-space` 一行若是 `MISSING`，按下面安装；不要指望 `pip install -e .` 带上它。

## j-space（必装，若要用 ledger 默认账本）

来源：[J-Space Cognition Suite V3.6](https://github.com/Tiger3807861189/J-Space-Cognition-Suite-V3.6)。Apache-2.0。入口是仓库里的 **`j-space/` 整目录**（`SKILL.md`、`modules/`、`references/`、`scripts/` 必须相对完整，不要只拷 `SKILL.md`）。

### 1. 取源码

```bash
git clone --depth 1 https://github.com/Tiger3807861189/J-Space-Cognition-Suite-V3.6
# 或下载发行包后解压
```

### 2. 装进 Cursor 用户 Skills 目录

Flame 默认按这个顺序找（`FLAME_JSPACE` 一旦设置，就只认这一处）：

1. `$FLAME_JSPACE`
2. `~/.cursor/skills-cursor/j-space`
3. `~/.cursor/skills/j-space`
4. 若干仓库相对路径回退（见 `skills.py`）

```bash
mkdir -p ~/.cursor/skills-cursor
cp -a J-Space-Cognition-Suite-V3.6/j-space ~/.cursor/skills-cursor/j-space
```

### 3. 自检

```bash
python3 ~/.cursor/skills-cursor/j-space/scripts/verify_suite.py
flame skills
```

跑一条 ledger 任务后，看工作区 `.flame/act_skill.json` 的 `jspace` / `use_jspace` 字段。stderr 会有 `skill=j-space`（默认挂账本时）。

装不上或路径不对时：ledger + `use_jspace=true` 的 act 仍会跑，但 prompt 里是「skill not found」——没有 ledger、没有模块。

## fact-graph（graph，已打包）

包内 `flame/data/fact-graph/`。可用 `FLAME_FACTGRAPH` 覆盖。

graph 时 act **前台**跑编排器并等到退出；`FLAME_TIMEOUT_SEC`（默认 1800）要盖得住 fact-graph 的 `wallclock_budget`。

开图前 harness 写 `.flame/graph_seed.json` 并直接 `init` 到 `.fact-graph/runs/flame-act-cN/`（act 只 `run`，不得改 origin/goal）。字段：`goal`=original+verify_points，`constraints`=plan.constraints，`origin`=brief/上轮 verify（现状），`hint`=approach。`plan.goal` 在所有 effort 上都被强制为 original。

执行阶段不挂 j-space 账本。
