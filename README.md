# ge-data-quality-demo

A demo of an **agentic data quality pipeline** built on
[Great Expectations Core 1.x](https://greatexpectations.io/).

Instead of a human hand-writing every data check, an agent profiles a
database table and drafts the checks. Great Expectations runs them. When
checks fail, a second agent triages the raw results into a prioritized,
actionable report.

```
   table  →  [Agent 1: profile & suggest]  →  draft suite (human reviews)
   table  →  [GX checkpoint]               →  pass/fail + JSON result
                         │ on failure
                         ▼
               [Agent 2: triage & prioritize]  →  ranked report + optional Slack summary
```

## Why "propose, don't auto-apply"

The profiling agent intentionally _proposes_ checks rather than applying them
automatically. This is the whole point:

- It correctly detected the injected `order_id` duplicate and refused to write
  a uniqueness check that would immediately fail — instead it flagged it as
  `# REVIEW:`.
- It incorrectly fired the "ends in `_id` → probably unique" heuristic on
  `customer_id` (a foreign key, not a primary key). A customer can place many
  orders.
- Its enum detection for `status` picked up all 6 observed values, including
  the deliberately invalid `"backordered"`. The set reflects reality, not
  what _should_ be there.

These aren't bugs to fix silently — they're the argument for human-in-the-loop
review before any check goes live.

## Quickstart

### Prerequisites

- Python 3.10+ (3.11 recommended — required for `int | None` union syntax used in the triage agent)
- `pip install -r requirements.txt`

### Run the full pipeline manually

```bash
# 1. Create the demo database (data/orders.db)
python data/seed_db.py

# 2. Agent 1 — profile the table and draft a suite
python agents/profile_and_suggest.py
# → writes gx_checks/suggested_suite.py (review this before proceeding)

# 3. Run the hand-reviewed GX checks
python gx_checks/run_checks.py
# → writes gx_checks/last_result.json; exits 1 if any check fails

# 4. Agent 2 — triage the failures
python agents/triage_failures.py
# → prints a prioritized report; writes gx_checks/triage_report.md
```

### Enable LLM features (optional)

Set `ANTHROPIC_API_KEY` before running either agent:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

python agents/profile_and_suggest.py   # adds LLM review to the draft suite
python agents/triage_failures.py       # adds a Slack-ready incident summary
```

## Project structure

```
ge-data-quality-demo/
├── data/
│   └── seed_db.py              # Creates data/orders.db with 5 injected issues
├── gx_checks/
│   └── run_checks.py           # GX Core 1.x suite + checkpoint; exits 1 on failure
├── agents/
│   ├── profile_and_suggest.py  # Agent 1: profiles table, drafts GX suite
│   └── triage_failures.py      # Agent 2: ranks failures, suggests next steps
├── .github/workflows/
│   └── data-quality.yml        # CI: seed → profile → checks → triage
├── article/
│   └── medium_article.md       # Long-form write-up of the whole project
├── requirements.txt
└── README.md
```

## The demo data

`data/orders.db` — SQLite, table `orders`, 500 rows, columns:
`order_id`, `customer_id`, `amount`, `status`, `order_date`.

Five issues injected on purpose:

| # | Issue | Column | Detail |
|---|-------|--------|--------|
| 1 | Null values | `customer_id` | ~4 rows |
| 2 | Duplicate | `order_id` | 1 duplicate pair |
| 3 | Negative value | `amount` | `-19.99` |
| 4 | Invalid enum | `status` | `"backordered"` — not a valid lifecycle value |
| 5 | Future date | `order_date` | Dated 30 days in the future |

## GX checks (`gx_checks/run_checks.py`)

Seven expectations using the GX Core 1.x Fluent API
(`context.data_sources.add_sqlite()` → `DataAsset` → `BatchDefinition` →
`ExpectationSuite` → `ValidationDefinition` → `Checkpoint`):

| Expectation | What it catches |
|-------------|----------------|
| `ExpectTableRowCountToBeBetween` | Accidental truncation / runaway inserts |
| `ExpectColumnValuesToBeUnique` on `order_id` | Duplicate orders |
| `ExpectColumnValuesToNotBeNull` on `customer_id` | Missing customer references |
| `ExpectColumnValuesToBeBetween` on `amount` (min=0) | Negative amounts |
| `ExpectColumnValuesToBeInSet` on `status` | Invalid lifecycle values |
| `ExpectColumnValuesToMatchRegex` on `order_date` | Malformed dates |
| `ExpectColumnValuesToBeBetween` on `order_date` (max=today) | Future-dated records |

Running against the seeded data: **4 of 7 fail**, exactly as designed.
The script exits with code 1, making it usable directly as a CI gate.

## Swap to a real database

Only one line changes — the data source connection:

```python
# SQLite (default)
context.data_sources.add_sqlite(name="...", connection_string="sqlite:///...")

# Postgres
context.data_sources.add_postgres(name="...", connection_string="postgresql+psycopg2://...")

# Snowflake
context.data_sources.add_snowflake(name="...", connection_string="snowflake://...")
```

Everything else — suite, checkpoint, triage — stays the same.

## CI

`.github/workflows/data-quality.yml` runs on every push and PR:

1. Seeds the database
2. Runs the profiling agent (uploads `suggested_suite.py` as a build artifact)
3. Runs GX checks (`continue-on-error: true` so triage always runs)
4. If checks failed: runs the triage agent and writes the report to the
   GitHub Actions step summary
5. Fails the build

## Known limitations

- The PK-vs-FK heuristic in `profile_and_suggest.py` is not perfect. It
  uses column name convention (`<table>_id` = PK, anything else = FK) which
  is a reasonable default but can be wrong.
- Enum detection reflects observed data, not authoritative valid-value lists.
  Always verify proposed sets against a source-of-truth before enabling.
- Only SQLite has been tested end-to-end. Postgres/Snowflake connection
  snippets are from GX documentation but not independently verified here.
- The `ANTHROPIC_API_KEY` code paths (LLM review and Slack summary) were
  written and reviewed but not exercised in automated testing.

## Extending this

- **Web UI**: render profiling proposals as an accept/reject checklist instead
  of a Python file to edit.
- **Multi-table support**: extend profiling to follow foreign keys and check
  referential integrity across tables.
- **Self-healing**: have the triage agent open a GitHub issue automatically for
  CRITICAL failures.
- **Real-time**: plug the checkpoint into a dbt post-hook or Airflow task for
  continuous quality monitoring.
