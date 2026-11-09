# PLAN.md — build steps

Each step ends with something runnable and a review checkpoint. Domain rules live in [CLAUDE.md](CLAUDE.md).

## Step 0 — Scaffolding
- `pyproject.toml` (deps: duckdb, dbt-duckdb, mcp, pyyaml, pydantic; dev: pytest, ruff, mypy).
- `.gitignore` (`.venv/`, `data/`, `warehouse/`, `dbt/target/`, `dbt/logs/`, `.env`).
- Empty package folders per CLAUDE.md layout.
- **Done when:** `pip install -e ".[dev]"` and `ruff check .` succeed.

## Step 1 — Synthetic data generator
- `config.py`: 2 owners (`OWN-001`, `OWN-002`), 5 properties (e.g. 3 / 2 split), ~600 units, unit mix (studio, 1x1, 2x2, 3x2), 3 years of history, fixed seed.
- Entities, generated clean first: properties → units (incl. non-revenue: model, employee, down, office) → leases (renewals, early move-outs, move-in ≠ lease_start, notice dates, vacancies between leases) → monthly charges and payments (rent, concessions, fees, partial/late payments) → guest cards (lead source, tour, application, lease conversion).
- `truth.py`: from the **clean** data, compute ground-truth answers (occupancy, economic occupancy, loss to lease by unit type, etc.) and write `data/truth/*.json`.
- `mess.py`: then inject Yardi-like defects into the CSVs only: mixed date formats, trailing whitespace, inconsistent casing/codes (`1x1`, `1BR`, `1 Bed`), duplicate rows, currency strings (`"$1,250.00"`), blanks vs `NULL` strings, reversed charges, renamed columns between extract files.
- Unit tests: determinism (same seed → same bytes), row counts, every defect type present.
- **Done when:** `python -m generator --seed 42` writes CSVs + truth files.

## Step 2 — dbt bronze
- `profiles.yml` → local DuckDB via env var.
- Bronze models: read each CSV as all-VARCHAR, add `_source_file`, `_loaded_at`. No cleaning.
- Source freshness/row-count tests.
- **Done when:** `dbt build --select bronze` passes.

## Step 3 — dbt silver
- Typing, trimming, date parsing (multiple formats), currency parsing, code normalization via seed mapping tables (`unit_type_map`, `unit_status_map`, `charge_code_map`).
- Deduplication with explicit tie-break rules.
- Keep all four lease dates as separate columns (rule 2).
- Tests: not_null on keys, accepted_values on normalized codes, singular tests (e.g. `move_out >= move_in`, `notice_date <= move_out`).
- **Done when:** `dbt build --select silver` passes with zero unresolved codes.

## Step 4 — dbt gold
Proposed models, each with a documented grain, PK test and relationship tests (rule 6):

| Model | Grain (one row per…) |
|---|---|
| `dim_owner` | owner |
| `dim_property` | property (carries `owner_id`) |
| `dim_unit` | unit (carries `unit_type`, `is_rentable`, `market_rent`) |
| `dim_date` | calendar day |
| `fct_lease` | lease (term dates, move dates, notice date, in-place rent) |
| `fct_unit_day` | unit × calendar day (occupied flag, rentable flag, in-place rent) |
| `fct_charge` | posted charge/payment line |
| `fct_unit_month_rent` | unit × month (GPR, rent billed, rent collected) |
| `fct_guest_card` | guest card / lead |

- Singular tests encoding domain rules: non-revenue units never counted as rentable; a unit has at most one occupying lease per day; every property belongs to exactly one owner.
- **Done when:** `dbt build` passes and `dbt docs generate` shows grain on every gold model.

## Step 5 — Semantic layer (`semantic/metrics.yml`)
- Metrics: `physical_occupancy`, `economic_occupancy`, `loss_to_lease` (required dimension: `unit_type`), `gross_potential_rent`, `rent_collected`, `avg_in_place_rent`, `lead_to_lease_conversion`, `notices_given`, `move_ins`, `move_outs`.
- Each metric: description, numerator/denominator or expression, source gold model, time grain (point-in-time vs period), allowed/required dimensions, owner-scope column.
- `loader.py`: Pydantic validation + SQL rendering with bound parameters; reject undefined dimensions.
- Unit tests: schema validation, rendered SQL snapshots, `loss_to_lease` refuses to render without `unit_type`.
- **Done when:** `python -m semantic.loader --check` passes.

## Step 6 — FastMCP server
- Tools (read-only): `list_properties`, `list_metrics`, `describe_metric`, `get_metric(metric, property_ids?, start, end, group_by?)`.
- `auth.py`: caller identity → `owner_id` from env (`MFG_CALLER_OWNER_ID`) for local stdio; designed so a real deployment swaps in token-based identity.
- Owner scope injected into every query server-side; requested property IDs outside scope are treated as not found (no existence leak).
- DuckDB opened read-only; only semantic-layer-rendered, parameterized SQL.
- Unit tests: cross-owner access returns nothing and leaks nothing; unknown metric/dimension errors are clean.
- **Done when:** `mcp dev mcp_server/server.py` lists tools and answers a scoped question.

## Step 7 — Business-question eval suite
- `evals/questions.yml`: ~15–25 questions, e.g. "Physical occupancy at Property X on 2025-06-30?", "Loss to lease by unit type for Owner 1 in Q4 2024?", "Economic occupancy for Property Y in March 2025?", plus negative cases ("As OWN-001, occupancy for an OWN-002 property" → must be refused/empty).
- Tests call MCP tools in-process and compare to `data/truth/*.json` within a stated tolerance.
- Includes trap questions that fail if a rule is broken (e.g. counting a model unit, using `lease_start` as move-in).
- **Done when:** `pytest evals` passes locally.

## Step 8 — CI
- GitHub Actions (`windows-latest`, Python 3.11): install → ruff → mypy → generate (seed 42) → `dbt build` → `pytest tests` → `pytest evals`.
- Upload dbt docs / eval report as artifacts.
- **Done when:** CI is green on a PR.

## Step 9 — README and demo
- Architecture diagram, how domain rules map to tests, sample agent transcript using the MCP server, "what I'd do in production" section (Snowflake/Databricks, real IdP, row-level security, data contracts).

## Open questions for your review
1. **GPR basis:** market rent for all rentable units (standard), or in-place rent for occupied + market for vacant? Plan assumes market rent for all rentable units.
2. **"Occupied" definition:** physically occupied (`move_in` ≤ day < `move_out`) — plan assumes yes; on-notice units still count as occupied until move-out.
3. **Rent collected:** cash applied to rent charges in the period, excluding fees and prepayments for future periods — confirm.
4. **Owner split:** 3 properties for OWN-001, 2 for OWN-002 — fine?
5. **CI runner:** `windows-latest` to match local, or `ubuntu-latest` for speed?
