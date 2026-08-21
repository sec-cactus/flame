# Act skills：部署与探测

high / max 时，plan 写 `search`，act 按选择调用 skill：

| `plan.search` | act 调用 | 是否打进 Flame 包 |
|---|---|---|
| `depth`（缺省；非法/未知 search 字符串也回落 depth） | j-space | **否，需本机安装** |
| `breadth` | fact-graph | 是（`flame/data/fact-graph`） |

算法名（如题目里的 BFS/DFS）**不会**映射成 Flame 的 `search`。plan 应写 `depth` 或 `breadth`。

fast / standard 不加载这两个 skill。fact-graph 的 `complete` 不是 Flame 通过；verify 仍是终裁。

默认 fact-graph worker 使用 `CURSOR_MODEL=auto`（见包内 `config.example.toml` / init 模板）。

查当前机器解析到哪：

```bash
flame skills
```

`j-space` 一行若是 `MISSING`，按下面安装；不要指望 `pip install -e .` 带上它。

## j-space（必装，若要用 depth / high / max）

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

推荐拷到 Cursor 会加载的那份：

```bash
SRC=J-Space-Cognition-Suite-V3.6/j-space
DEST="$HOME/.cursor/skills-cursor/j-space"
mkdir -p "$(dirname "$DEST")"
cp -a "$SRC" "$DEST"
python3 "$DEST/scripts/verify_suite.py"
```

`verify_suite.py` 应退出 0。若宿主只在启动时扫 Skills，重载 Cursor / 再开一次 `agent`。

不放进上述目录时，必须显式指定：

```bash
export FLAME_JSPACE=/absolute/path/to/j-space
flame skills
```

`FLAME_JSPACE` 必须指向**含 `SKILL.md` 的 `j-space` 目录本身**，不是套件仓库根。

### 3. 确认 Flame 能看见

```bash
flame skills
```

期望类似：

```
j-space     /home/you/.cursor/skills-cursor/j-space
fact-graph  .../flame/data/fact-graph
```

跑一条 high 任务后，看工作区 `.flame/act_skill.json` 的 `jspace` 字段是否为该路径。stderr 会有 `skill=j-space`（plan 选了 depth 时）。

装不上或路径不对时：high/max + depth 的 act 仍会跑，但 prompt 里是「skill not found」——没有 ledger、没有模块，等于白写 depth。

## fact-graph（包内已带）

`pip install` / 源码树里的 `src/flame/data/fact-graph/` 即可，不必另装。覆盖路径：

```bash
export FLAME_FACTGRAPH=/absolute/path/to/fact-graph
```

该目录同样需要 `SKILL.md` 和 `scripts/orchestrator.py`。包内副本不含 Web UI。

breadth 时 act **前台**跑编排器并等到退出；`FLAME_TIMEOUT_SEC`（默认 1800）要盖得住 fact-graph 的 `wallclock_budget`。

## 和 Cursor Skill 加载的关系

Flame 把 skill 路径写进 **act 的 prompt**，由内层 `agent` 去读文件、跑 `jspace.py` / `orchestrator.py`。即使 Cursor 已经把 j-space 当宿主 Skill 加载了，也请按上面装好目录：Flame 不依赖宿主的 Skill 选择器，只认磁盘路径。

preprocess / plan / verify（以及无 skill 的 act）prompt 会显式 **Skill ban**，要求忽略宿主已加载的 skill、不读任何 `SKILL.md`。这是软约束；若 agent 仍违禁，属于宿主行为，不是 Flame 挂载。
