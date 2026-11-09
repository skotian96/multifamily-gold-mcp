# CLAUDE.md — multifamily-gold-mcp

Guidance for Claude Code (and humans) working in this repo. Read this before changing anything.

## Project goal

Show, end to end and fully local, how raw property-management data becomes **AI-ready, governed gold data** that an AI agent can query **safely**:

1. Messy Yardi-like CSV extracts (properties, units, leases, charges, guest cards).
2. A dbt medallion pipeline (bronze → silver → gold) on DuckDB.
3. A semantic layer (`semantic/metrics.yml`) where each business metric is defined **once**.
4. A FastMCP server whose tools answer metric questions, scoped to the caller's owner.
5. A pytest regression eval of business questions with known answers, run in CI.

Scope of the synthetic portfolio: 2 owners, 5 properties, ~600 units, 3 years of history.

Stack: Windows, Python 3.11+, DuckDB, dbt-duckdb, official `mcp` Python SDK (FastMCP), pytest, GitHub Actions. **No cloud services.**

## Folder layout

```
multifamily-gold-mcp/
├── CLAUDE.md                  # this file
├── PLAN.md                    # build steps
├── README.md                  # public-facing overview (written last)
├── requirements.txt           # pinned direct dependencies
├── .github/workflows/ci.yml   # lint, type-check, generate, dbt build, pytest
├── generator/
│   └── generate.py            # simulate clean portfolio -> inject defects -> CSVs + KNOWN_ISSUES.md
├── data/
│   ├── raw/                   # generated CSVs + KNOWN_ISSUES.md (gitignored)
│   └── truth/                 # ground-truth JSON for evals (gitignored)
├── warehouse/                 # DuckDB file(s) (gitignored)
├── dbt/                       # dbt project
│   ├── dbt_project.yml
│   ├── profiles.yml           # local DuckDB profile, path via env var; no secrets
│   ├── models/
│   │   ├── bronze/            # 1:1 with raw CSVs, all text, + load metadata
│   │   ├── silver/            # typed, cleaned, deduped, conformed codes
│   │   └── gold/              # dims, facts, metric-ready marts (documented grain)
│   ├── macros/
│   └── tests/                 # singular data tests (business invariants)
├── semantic/
│   ├── metrics.yml            # single source of truth for metric definitions
│   └── loader.py              # parses/validates metrics.yml, renders SQL
├── mcp_server/                # FastMCP server
│   ├── __main__.py            # python -m mcp_server (stdio transport)
│   ├── server.py              # tool registration
│   ├── auth.py                # resolves caller -> owner scope
│   └── queries.py             # read-only, parameterized DuckDB access
├── evals/                     # business-question regression suite (pytest)
│   ├── questions.yml          # question, tool call, expected answer, tolerance
│   └── test_business_questions.py
└── tests/                     # unit tests for generator, semantic loader, server
```

## How to run each part

All commands are PowerShell from the repo root. (Commands marked *planned* don't work until that build step lands — see PLAN.md.)

```powershell
# One-time setup. Use the py launcher: plain `python` on this machine
# resolves to R's miniconda, not python.org Python.
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import duckdb, mcp, yaml; print('ok')"   # smoke test

# 1. Generate raw data (deterministic for a given seed; ~3s)
python -m generator.generate --seed 42 --out data/raw
#    -> read data/raw/KNOWN_ISSUES.md for the injected defects silver must handle

# 2. Build the medallion pipeline + run dbt tests (~10s)
#    Run from inside dbt/: paths default to ../warehouse/warehouse.duckdb and
#    ../data/raw (override with MFG_DUCKDB_PATH / MFG_RAW_DIR).
cd dbt
dbt build --profiles-dir .            # expect PASS + 1 WARN (unmapped charge codes)
dbt docs generate --profiles-dir .    # lineage + grain docs
cd ..

# 3. Validate the semantic layer                                   [planned]
python -m semantic.loader --check semantic/metrics.yml

# 4. Run the MCP server (stdio); caller scope comes from env        [planned]
$env:MFG_CALLER_OWNER_ID = "OWN-001"
python -m mcp_server
# Inspect interactively:  mcp dev mcp_server/server.py

# 5. Tests and evals                                               [planned]
pytest tests
pytest evals

# Lint / type-check
ruff check .
mypy generator semantic mcp_server
```

CI (`.github/workflows/ci.yml`) runs: ruff → mypy → generate (fixed seed) → `dbt build` → `pytest tests` → `pytest evals`.

## Domain rules (non-negotiable)

These encode how multifamily operators actually measure the business. Code, dbt models, metric definitions and evals must all agree with them.

1. **Non-revenue units are excluded from occupancy denominators.** Units whose status/use is `model`, `employee`, `down`, or `office` are not rentable. They stay in `dim_unit` (flagged `is_rentable = false`) but never enter occupancy or GPR denominators.
2. **Lease dates are distinct events — never conflate them.**
   - `lease_start` / `lease_end`: contractual term.
   - `move_in`: when the resident physically took possession (may differ from `lease_start`).
   - `notice_date`: when the resident gave notice to vacate.
   - `move_out`: when the resident physically left (may differ from `lease_end`).
   Each has its own column and its own meaning. Never coalesce one into another or use one as a proxy for another without an explicit, documented rule.
3. **Physical occupancy** = occupied rentable units ÷ rentable units, at a point in time. "Occupied" is driven by `move_in` / `move_out`, not by lease dates.
4. **Economic occupancy** = rent collected ÷ gross potential rent (GPR), for a period.
5. **Loss to lease** = market rent − in-place rent, **always reported by unit type** (never only as a property-wide total).
6. **Every gold model documents its grain** (one sentence: "one row per …") in its YAML `description`, and has:
   - a primary-key test (`unique` + `not_null`, or a combination-unique test for composite keys), and
   - `relationships` tests for every foreign key.
7. **Owner data is segregated.** A caller scoped to one owner must never see another owner's properties — not in results, error messages, row counts, or metadata. Scope is enforced server-side from the caller's identity, never from a tool argument the caller can set.
8. **Metrics are defined once**, in `semantic/metrics.yml`. MCP tools and evals use that definition; no hand-written duplicate SQL for the same metric.

## Code style

- Small, single-purpose functions; type hints everywhere; docstrings on every public module, class and function.
- No secrets in code or config. Paths and caller identity come from environment variables; `.env` is gitignored.
- Generator is deterministic for a given seed.
- MCP server opens DuckDB **read-only** and only runs parameterized SQL — no free-form SQL from callers.
- dbt: one model per file, `snake_case`, layer prefixes (`brz_`, `slv_`, `dim_`, `fct_`, `mart_`).
- Prefer clarity over cleverness; this repo is a portfolio piece meant to be read.
