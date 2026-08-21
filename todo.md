# Flame todos

## Verify: audit evidence handles (not re-execution)

**Status:** done

**Principle:** Evidence check is an **audit**, not a replay.

- Trust the model to be honest: it may hallucinate or err, but does not intentionally forge.
- Do **not** re-run tests or re-derive conclusions in the harness (no Archon-style mechanical CI gate).
- Do **not** enumerate task scenarios (code vs research, etc.).
- Any conclusion that claims success must cite **objective evidence handles**.
- Those handles must **really exist**, and this cycle must show they were **actually touched** (tool trace) — “done” and “exists”, not “correct”.

**Harness job:** collect act/verify tool traces; extract handles from `checks[]`; confirm each handle exists and appears in the trace; on failure set `evidence_ok=false` and fill `evidence_gaps`. Interpretation of evidence stays with the model.

**Still true:** `points_met` with empty `checks` → `evidence_ok=false`.

**Non-goal:** Mandatory bash/`pytest` pass-as-judge; scenario-specific rule tables.

## Effort → skill topology (replace plan `search` vote)

**Status:** done (step 1 + step 2)
**Touch:** `src/flame/prompts.py`, act skill attach in `loop.py` / types, fact-graph reason + intent metadata, `docs/DEV.md` / README / SKILLS.

**Keep plan.** Plan still writes goal / approach / constraints / verify_points every cycle. Do **not** fold plan into the graph. Drop plan’s `search` majority-vote; topology comes from **effort**, with small on/off flex.

### Binding (step 1 — landed)

| effort | Act topology | Flex | Default |
|---|---|---|---|
| fast / standard | no j-space / no fact-graph | — | — |
| **high** | single act (no graph) | plan sets whether act mounts **ledger** (j-space) | **on** |
| **max** | act runs **fact-graph** | *(step 2)* reason sets whether **this intent** mounts ledger | **off** |

**Tradeoffs (document honestly):**

- **high** — deeper thinking on one trajectory; user accepts direction risk.
- **max** — pays graph cost for coverage / mid-course branching; not “strictly stronger high.”

### high — plan `use_ledger` (default true) — landed

Mount ledger on act **unless** plan decides otherwise:

- **Off** if approach is a **short** task (few seams, one-shot checkable), **or**
- **Off** if plan sees a **clearly complex / multi-path** approach.

Omit / tie → **true**. `SearchKind` / `plan.search` removed (no compat).

### max — per-intent ledger (step 2 — landed)

- Act always opens fact-graph.
- **Default:** no ledger on any intent (`use_ledger` false / omitted on reason intent).
- Reason may set `use_ledger=true` only when the new intent will be the sole open branch, is long / needs deep reasoning, and has no multi-path risk.
- Orchestrator **forces off** if open intents already exist at `add_intent` time.
- Explore mounts ledger only when `intent_allows_ledger` (sole open + flag); ledger dir = `run_dir/ledgers/<intent_id>/` (never workspace `.jspace/`). Missing j-space → skip mount, still explore.

### Layering (unchanged intent)

Graph **explores**; Flame verify **QAs** against original + `verify_points`. Board `complete` is not Flame success.

## Max: mid-graph / mid-run quality checks

**Status:** done (wiring; no separate mid_qa agent)
**Priority:** medium — long max runs vs Fable-style interval self-check  
**Scope:** `effort=max` (plan.goal force is **all** efforts)

### Landed

Reuse in-graph reason (complete vs Goal + constraints) by fixing what Goal/constraints *are*:

| Board field | Source |
|---|---|
| **goal** | original + `verify_points` (harness `graph_seed` / act addendum) |
| **constraints** | `plan.constraints` (no fallback to proxy goal) |
| **origin** | 现状：brief + prior verify diagnosis (empty → discover placeholder) |
| **hint** | `plan.approach` |

Also: **`plan.goal` harness-forced to original** every cycle (all efforts). Creativity stays in approach / constraints / verify_points.

### Not in scope here

- End verify evidence **audit** (separate, done).
- Mid-run **user** hint injection (todo below).
- Extra mid_qa agent round (deferred; wiring preferred).

## Max / Fable5: mid-run user input

**Status:** open  
**Priority:** low — nice for long Fable-style runs; not this arc  
**Scope:** `effort=max` (Fable5对照只谈 max)

During a long graph + Flame loop run, the user cannot inject course corrections (hints, scope changes, “stop that branch”) without killing the process. Fact-graph already has a `hint` path; Flame loop does not expose pause / inject / continue.

**Direction:** allow mid-run user input (e.g. inject hint into the board, adjust constraints, or signal verify early) without discarding the run.

## Cross-run memory (low priority)

**Status:** open  
**Priority:** low — noted for Fable scaffolding parity; not a current focus

Fable recommends durable lesson files across sessions. Flame state today is per-run (`.flame/`, board, ledgers). Optional later: export/load lessons between `flame run`s.
