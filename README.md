# Flame

Minimal **plan → act → verify** harness over a local coding agent CLI. Supports **Cursor** (`agent`) and **OpenCode** (`opencode`). Flame only orchestrates stages, writes `.flame/` handoffs, and enforces degrade rules. Verify is the only judge.

## Requirements

- Python 3.11+ (stdlib only)
- One of:
  - Cursor CLI (`agent`) on `PATH`, logged in (`agent login`)
  - OpenCode CLI (`opencode`) on `PATH`, logged in (`opencode auth login`)

## Install

```bash
pip install -e .
```

### Act skills (high / max)

**fact-graph** ships in the package (used on **max**). **j-space does not** — install it before `effort=high` (default ledger):

```bash
git clone --depth 1 https://github.com/Tiger3807861189/J-Space-Cognition-Suite-V3.6
cp -a J-Space-Cognition-Suite-V3.6/j-space "$HOME/.cursor/skills-cursor/j-space"
python3 "$HOME/.cursor/skills-cursor/j-space/scripts/verify_suite.py"
flame skills
```

`flame skills` must show a real `j-space` path. Details: [`docs/SKILLS.md`](docs/SKILLS.md).

## Run

```bash
flame run "fix the failing tests"
flame run "summarize this repo" --effort fast --workspace .
flame run "…" --effort max --model auto
# OpenCode backend (model is provider/model):
flame run "…" --agent-backend opencode --model opencode-go/deepseek-v4-flash
```

**Progress → stderr** (`▶ plan` / tools / `✓ passed`). **Final answer → stdout** (usually last act text; `retry=false` → diagnosis).

```
▶ preprocess         # fast skips; standard/high quadrants+factors; max adds meld
  · quadrants
  · factors
▶ plan  cycle 1
  · goal: original (harness-forced)
  · skill=j-space                 # high default; max → fact-graph + harness init
▶ act   cycle 1
▶ verify  cycle 1
✓ passed in 1 cycle(s)
```

`plan.goal` is always the user request (harness overwrites). On **max**, Flame writes `.flame/graph_seed.json` and inits `.fact-graph/runs/flame-act-cN/` before act; act only `run`s the orchestrator. Verify audits evidence handles in `checks` (exist + touched this cycle) — not a test replay.

## Effort

| effort | preprocess | meld | act skill | loop |
|---|---|---|---|---|
| **fast** | skip | no | no | one verify round, then deliver |
| **standard** (default) | quadrants → factors → `brief.json` | no | no | until verify judges |
| **high** | same as standard | no | j-space ledger (plan may set `use_ledger=false`) | same |
| **max** | same + meld first | yes | fact-graph (always) | same |

**high** deepens one trajectory (direction risk accepted); plan defaults `use_ledger=true`, and turns it off only for short or clearly multi-path approaches. **max** pays for graph coverage / branching — not “strictly stronger high.” Topology is fixed by effort; plan no longer votes `search`.

`FLAME_MAX_CYCLES` (default 8) is only a runaway guard for standard/high/max. Aliases: `low`→`fast`, `medium`→`standard`.

## Degrade floor

Preprocess / plan / verify can fail open. **Act timeout** (`FLAME_TIMEOUT_SEC`, default 1800) hands leftover workspace artifacts to verify (which may retry). Only a **non-timeout** act failure is a hard error. If verify sets `retry=false` (infeasible), stdout is the diagnosis, not the act text.

## Config

| env / flag | default |
|---|---|
| `FLAME_AGENT_BACKEND` / `--agent-backend` | `cursor` (`cursor` \| `opencode`) |
| `FLAME_AGENT_BIN` / `--agent-bin` | `agent` or `opencode` by backend |
| `FLAME_MODEL` / `--model` | `auto` (OpenCode: use `provider/model`) |
| `FLAME_OPENCODE_MODEL` | `opencode-go/deepseek-v4-flash` (when OpenCode + model=`auto`) |
| `FLAME_WORKSPACE` / `--workspace` | cwd |
| `FLAME_EFFORT` / `--effort` | `standard` |
| `FLAME_TIMEOUT_SEC` | `1800` |
| `FLAME_MAX_CYCLES` | `8` |
| `FLAME_SAFETY` / `--safety` | off |
| `FLAME_JSPACE` | auto-detect |
| `FLAME_FACTGRAPH` | packaged `flame/data/fact-graph` |

## Python

```python
from flame import run

result = run("add a smoke test", workspace=".", effort="fast")
print(result.passed, result.output)
```

## Exit codes

| code | meaning |
|---|---|
| 0 | verify passed |
| 1 | process / env / act error |
| 2 | safety deny |
| 3 | not passed (retry=false, degrade deliver, or safety cycle cap) |

## Docs

- [`docs/DEV.md`](docs/DEV.md) — architecture, protocol, artifacts, prompts
- [`docs/SKILLS.md`](docs/SKILLS.md) — j-space / fact-graph install
- [`todo.md`](todo.md) — open backlog (mid-run hints / cross-run memory, low priority)

Cluster dispatch (**Flame Fleet**) lives in a separate private repo; not part of this package.

## License

MIT. See [`LICENSE`](LICENSE).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
