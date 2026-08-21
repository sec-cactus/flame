# Flame

Minimal **plan → act → verify** harness over the local Cursor CLI (`agent`, default `--model auto`). Flame only orchestrates stages, writes `.flame/` handoffs, and enforces degrade rules. Verify is the only judge.

## Requirements

- Python 3.11+ (stdlib only)
- Cursor CLI (`agent`) on `PATH`, logged in

## Install

```bash
pip install -e .
```

### Act skills (high / max)

**fact-graph** ships in the package. **j-space does not** — install it before `effort=high` or `max` with `search=depth`:

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
```

**Progress → stderr** (`▶ plan` / tools / `✓ passed`). **Final answer → stdout** (usually last act text; `retry=false` → diagnosis).

```
▶ preprocess         # fast skips; standard/high quadrants+factors; max adds meld
  · quadrants
  · factors
▶ plan  cycle 1
  · goal: …
  · search=depth skill=j-space
▶ act   cycle 1
▶ verify  cycle 1
✓ passed in 1 cycle(s)
```

## Effort

| effort | preprocess | meld | act skills | loop |
|---|---|---|---|---|
| **fast** | skip | no | no | one verify round, then deliver |
| **standard** (default) | quadrants → factors → `brief.json` | no | no | until verify judges |
| **high** | same as standard | no | j-space / fact-graph | same |
| **max** | same + meld first | yes | same as high | same |

On high/max, plan writes this cycle's **`approach` first**, then majority-votes **`search` against that approach** (not the full original wording): wide / compare-list / fear-missing → `breadth` (fact-graph); deep / verify-pierce / fear-breaking → `depth` (j-space). Tie or single-path simulation → depth. Each replan re-votes.

`FLAME_MAX_CYCLES` (default 8) is only a runaway guard for standard/high/max. Aliases: `low`→`fast`, `medium`→`standard`.

## Degrade floor

Preprocess / plan / verify can fail open. **Act timeout** (`FLAME_TIMEOUT_SEC`, default 1800) hands leftover workspace artifacts to verify (which may retry). Only a **non-timeout** act failure is a hard error. If verify sets `retry=false` (infeasible), stdout is the diagnosis, not the act text.

## Config

| env / flag | default |
|---|---|
| `FLAME_AGENT_BIN` / `--agent-bin` | `agent` |
| `FLAME_MODEL` / `--model` | `auto` |
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

- [`design.md`](design.md) — architecture and degrade rules
- [`docs/DEV.md`](docs/DEV.md) — protocol, artifacts, prompts
- [`docs/SKILLS.md`](docs/SKILLS.md) — j-space / fact-graph install

## License

MIT. See [`LICENSE`](LICENSE).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
