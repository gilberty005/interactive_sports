# Plan: Make *almost all* NHL-API-Reference endpoints available to the agent (catalog-driven)

This plan assumes you want a **hard** tool-selection problem: the agent has broad access, but must **choose** what to query, interpret results, and stay under a call budget.

Your current file already has the right foundation:
- `nhl_api_list_endpoints(category=None)` ✅
- `nhl_api_call(base, path_template, path_params, query_params)` ✅
- An allowlist `ENDPOINT_CATALOG` ✅ (but currently tiny)

The most efficient path is to **stop adding per-endpoint Python tools** and instead scale via an **endpoint catalog + single generic call tool**.

---

## Goal architecture

### Tools exposed to the agent (target)
Expose only these tools to the agent:

1. `nhl_api_list_endpoints`  
2. `nhl_api_call`  
3. `fantasy_score` (your scoring engine; add later)  

Optional (only if needed):
- `normalize_ids` (if you have ID mismatches)
- `cache_*` (if you want agent-controlled caching; otherwise keep caching inside your clients)

Everything from the README becomes available as **catalog entries** (data), not individual tools (code).

---

## Repository changes (high level)

### Today
- Endpoints are partly accessible via many wrapper tools in `build_tool_specs()`.
- Catalog exists in `src/tools/tools.py` but contains only ~11 endpoints.
- The agent can call `nhl_api_call`, but only for those ~11 endpoints.

### Target
- All/most endpoints from the README are expressed in **one JSON catalog**.
- Catalog is loaded at runtime.
- A script can **regenerate** catalog from README automatically.
- Agent sees only catalog + generic caller (and scoring engine).

---

## Step-by-step implementation plan (very concrete)

### Step 1 — Move the catalog to JSON (so it can scale)
**Why:** You will end up with 100–300 endpoints. Keeping them as a Python list will be painful.

**Create:**
- `src/tools/endpoint_catalog.json`

**Schema (recommended):**
```json
[
  {
    "name": "roster_by_season",
    "base": "web",
    "path": "roster/{team}/{season}",
    "method": "GET",
    "category": "roster",
    "cost": 2,
    "description": "Get team roster for a season",
    "params_schema": {
      "path": {"team": "string", "season": "string"},
      "query": {}
    }
  }
]
```

**Update `src/tools/tools.py`:**
1) Remove or shrink `ENDPOINT_CATALOG = [...]` (keep a tiny fallback if you want).
2) Add a loader function at the top (near imports):

```python
import json
from pathlib import Path

def load_endpoint_catalog() -> list[dict[str, Any]]:
    catalog_path = Path(__file__).resolve().parent / "endpoint_catalog.json"
    with catalog_path.open("r", encoding="utf-8") as f:
        return json.load(f)

ENDPOINT_CATALOG: List[Dict[str, Any]] = load_endpoint_catalog()
```

**Acceptance check:**
- `pytest -q` (or run a minimal script) should import `NHLTools` without error.
- `nhl_api_list_endpoints()` returns the JSON-loaded endpoints.

---

### Step 2 — Build a generator script that parses the README into a catalog
**Why:** Hand-entering 200 endpoints is a waste. The README already describes them.

**Create:**
- `scripts/generate_endpoint_catalog.py`

**Input:**
- Use the NHL API reference README content.
  - If you keep a copy in repo: `docs/NHL-API-Reference.md`
  - Or you can point it at a local file path.

**Output:**
- `src/tools/endpoint_catalog.generated.json`

**Minimal parsing strategy (works well):**
- Detect section headings to set `category` and `base`.
- Extract endpoint lines that contain `/v1/` or stats REST paths.
- Convert `/v1/foo/bar` → `foo/bar` (because your `NHLApiClient` likely prefixes `/v1/` already).
- Extract `{tokens}` from the path and add them to `params_schema.path`.
- Set defaults:
  - `method="GET"`
  - `cost` by category (see below)

**Heuristic mapping for base:**
- If section is "Web API" → `base="web"`
- If section is "Stats REST API" → `base="stats"`

**Cost defaults (tune later):**
- `edge`: 4
- `gamecenter`, `play-by-play`: 3
- `stats`: 3
- `schedule`, `standings`, `roster`: 1–2
- `meta`, `where-to-watch`, `tv`: 1 but tag as low-utility

**Generator should produce entries even if descriptions are missing:**
- `description` can be the heading + endpoint name.

**Acceptance check:**
- Running:
  ```bash
  python scripts/generate_endpoint_catalog.py --readme docs/NHL-API-Reference.md
  ```
  produces a JSON file with 100+ entries.
- Spot check: roster + schedule + edge endpoints appear.

---

### Step 3 — Add an “overrides” file for manual curation
Generated catalogs are rarely perfect. Don’t edit the generated file by hand.

**Create:**
- `src/tools/endpoint_catalog.overrides.json`

This file can:
- rename endpoints
- adjust cost/category
- add missing query params
- mark deprecated ones
- add aliases (two different paths for same concept)

**Then in `tools.py`, load + merge:**
- generated + overrides → final catalog

Merge policy suggestion:
- Match by `path` (or by `name`)
- If overrides entry exists, overlay fields onto generated entry.

**Acceptance check:**
- You can tune 10 endpoints without regenerating everything.

---

### Step 4 — Restrict the agent tool surface to “catalog + generic caller”
This is the key step that actually makes the task hard.

In `build_tool_specs(tools: NHLTools)`:
- Keep:
  - `nhl_api_list_endpoints`
  - `nhl_api_call`
- Remove all the wrapper tools from the agent’s view **(or put them behind a `DEBUG_TOOLS` flag)**.

Example:

```python
def build_tool_specs(tools: NHLTools) -> List[ToolSpec]:
    return [
        ToolSpec(... nhl_api_list_endpoints ...),
        ToolSpec(... nhl_api_call ...),
        # ToolSpec(... fantasy_score ...)  # add later
    ]
```

Keep wrappers in code if you want for debugging, but don’t register them as tools.

**Acceptance check:**
- Running the agent shows only two tools.
- Agent must browse catalog and call endpoints explicitly.

---

### Step 5 — Enforce max tool calls (e.g., 20) in the runner (not only in prompt)
You *should* still tell the agent in the prompt: “use as few calls as possible.”

But **do not rely on the prompt** for the hard constraint.

**Implement in the tool-execution loop** (likely `src/agent/runner.py` or similar):
- Maintain `tool_calls_used`
- Stop executing tools after N calls
- Force final answer

Pseudocode:

```python
MAX_TOOL_CALLS = 20

tool_calls_used = 0
trace = []

while True:
    model_msg = llm.generate(...)
    if model_msg.is_final:
        return final

    if model_msg.tool_call:
        if tool_calls_used >= MAX_TOOL_CALLS:
            return {
                "status": "tool_budget_exceeded",
                "tool_calls_used": tool_calls_used,
                "trace": trace,
                "final": None,
            }

        tool_calls_used += 1
        result = tool_registry.execute(...)
        trace.append({
            "i": tool_calls_used,
            "tool": tool_name,
            "args": tool_args,
            "result_summary": summarize(result),
        })
```

**Optional but excellent:** Add a *cost budget* too (sum endpoint `cost` from catalog):
- This prevents the agent from spending 20 calls purely on expensive EDGE endpoints.

---

### Step 6 — Update the system prompt so the agent behaves correctly
Add 4 hard rules (these make your eval cleaner):

1) **Never invent endpoint paths** — must call `nhl_api_list_endpoints` first.  
2) **Before first call**, output a short plan: signals → endpoints.  
3) **Minimize calls** and avoid redundant queries.  
4) **Final answer must include a trace** of tool calls actually used.

Example snippet to include:

- “You must not guess endpoints. Use the endpoint catalog tool first.”
- “You have a hard limit of 20 API calls.”
- “If you hit the limit, return the best possible answer with uncertainty.”

---

### Step 7 — Testing plan
#### Unit tests
- `tests/test_catalog_loads.py`:
  - `NHLTools().nhl_api_list_endpoints()` returns non-empty list
- `tests/test_call_allowlist.py`:
  - calling `nhl_api_call` with non-catalog path returns `path_template_not_allowed`

#### Network smoke tests
- Pick 5 endpoints from distinct categories:
  - schedule, roster, gamecenter, stats REST report, edge
- Call them with known params and assert non-empty payload

#### “Hardness” test
- Run a task like:
  - “Pick the best 2 waiver adds for next week under my scoring settings.”
- Confirm:
  - model does not spam endpoints
  - stays under budget
  - trace is complete

---

## Suggested file layout (final)
```
src/tools/
  tools.py
  endpoint_catalog.json                 # merged final catalog committed
  endpoint_catalog.generated.json       # generated output (optional committed)
  endpoint_catalog.overrides.json       # manual patches
scripts/
  generate_endpoint_catalog.py
docs/
  NHL-API-Reference.md                  # copy of README for stable parsing
```

---

## What you should do next (in order)
1) **Move `ENDPOINT_CATALOG` to `endpoint_catalog.json`** and load it in `tools.py`.  
2) **Write the generator script** and generate `endpoint_catalog.generated.json`.  
3) Add a small `endpoint_catalog.overrides.json` (start with 20–30 fixes).  
4) **Expose only** `nhl_api_list_endpoints` + `nhl_api_call` to the agent.  
5) Add **hard 20-call cap** in the runner.  
6) Write 5 network smoke tests across categories.

---

## Notes specific to your current `tools.py`
- You already have the two key meta-tools implemented correctly.
- The only thing “small” right now is catalog coverage.
- Once the catalog is generated and loaded from JSON, your current `nhl_api_call` becomes a universal gateway to *everything*.

