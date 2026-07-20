# Project Context: Agentic Data Quality Checks with Great Expectations

This document summarizes everything built in this conversation so it
can be used as context elsewhere (a new chat, Claude Code, a teammate)
to build a **sample app** on top of this codebase.

## What this project is

A demo/reference implementation of an **agentic data quality pipeline**:
instead of a human hand-writing every [Great Expectations](https://greatexpectations.io/)
(GX Core 1.x) check, an agent profiles a database table and drafts the
checks; Great Expectations runs them; and when checks fail, a second
agent triages the results into a prioritized report instead of raw JSON.

```
   table  →  [Agent: profile & suggest]  →  draft suite (human reviews)
   table  →  [GX checkpoint]              →  pass/fail + JSON result
                        │ on failure
                        ▼
              [Agent: triage & prioritize]  →  ranked report + optional Slack summary
```

Two deliverables came out of this:
1. **A runnable GitHub repo** (`ge-data-quality-demo`) — code, tests, CI, README, and a Medium article draft.
2. **A reusable Claude Skill** (`agentic-data-quality-checks.skill`) — the same workflow, generalized to work against any SQL table/database, packaged for reuse in future conversations.

## Repo structure (`ge-data-quality-demo/`)

```
ge-data-quality-demo/
├── data/
│   └── seed_db.py             # creates data/orders.db: 500 synthetic e-commerce
│                               # orders with 5 DELIBERATELY injected quality issues
├── gx_checks/
│   └── run_checks.py          # hand-reviewed GX Core 1.x suite + checkpoint;
│                               # writes last_result.json; exits 1 on any failure
├── agents/
│   ├── profile_and_suggest.py # Agent 1: profiles orders table, drafts a suite
│   └── triage_failures.py     # Agent 2: reads last_result.json, ranks failures
├── .github/workflows/
│   └── data-quality.yml       # CI: seed → profile agent → checks → triage agent
├── article/
│   └── medium_article.md      # long-form write-up of the whole project
├── requirements.txt           # great_expectations>=1.19,<2.0, sqlalchemy>=2.0, anthropic (optional)
└── README.md
```

### The demo data (`data/seed_db.py`)

SQLite `orders` table, 500 rows, columns: `order_id`, `customer_id`,
`amount`, `status`, `order_date`. Five issues injected on purpose:
1. A few null `customer_id`s
2. One duplicate `order_id`
3. One negative `amount`
4. One invalid `status` (`"backordered"` — not a real lifecycle value; real ones are `placed`, `shipped`, `delivered`, `cancelled`, `refunded`)
5. One future-dated `order_date`

### The checks (`gx_checks/run_checks.py`)

GX Core 1.x pattern used throughout: `Data Source` → `Data Asset` →
`Batch Definition` → `Expectation Suite` → `Validation Definition` →
`Checkpoint`. Connects via `context.data_sources.add_sqlite(...)`
(swappable for `add_postgres`, `add_snowflake`, etc. — same rest-of-code).
Seven expectations covering schema, uniqueness, nullability, numeric
range, enum membership, and date range. **Confirmed by actually running
it**: 4 of 7 expectations fail against the seeded data, exactly as
designed, and the script exits 1 — usable directly as a CI gate.

### Agent 1: `agents/profile_and_suggest.py`

Profiles every column (nulls, cardinality, min/max, distinct values),
applies heuristic rules, writes a **draft** suite
(`gx_checks/suggested_suite.py`) with a plain-English rationale per
proposal — nothing auto-applies. Optional `ANTHROPIC_API_KEY` gets an
LLM review layered on top.

**Important, actually-observed findings (not hypothetical) — keep these
when building anything on top of this:**
- ✅ Correctly detected the injected `order_id` duplicate and refused to
  propose a uniqueness check for it — flagged it as `# REVIEW:` instead
  of silently writing a check that would fail.
- ❌ **False positive on `customer_id`**: the heuristic "column name ends
  in `_id` → probably needs uniqueness" fired on `customer_id` too, even
  though a customer placing multiple orders is normal (it's a foreign
  key, not a primary key). The heuristic can't distinguish PK from FK
  from the name alone.
- ❌ **Enum set includes a live bug**: value-set detection for `status`
  pulled all 6 observed values, including the deliberately-invalid
  `"backordered"`, and proposed all 6 as "valid." It reflects what's in
  the data, not what should be.

These aren't bugs to silently fix — they're the entire argument in the
article for a "propose, don't auto-apply" design. If a sample app is
built on this, it should probably surface these same caveats to
whoever's using it (e.g. visibly flag proposals for review rather than
just running them).

### Agent 2: `agents/triage_failures.py`

Reads the checkpoint's raw result JSON, ranks failures by severity
(identifier/financial columns > status/date columns), and for each
failure type gives a specific suggested next step (duplicate → check
upstream retries; null spike → check the ETL step; unexpected category →
confirm with the business; range violation → check for sign/unit
errors). Optional `ANTHROPIC_API_KEY` turns the ranked list into a
Slack-ready incident summary via Claude.

### CI (`.github/workflows/data-quality.yml`)

Runs the full pipeline on every push/PR: seed → profiling agent
(uploads `suggested_suite.py` as a build artifact) → checks
(`continue-on-error: true`) → if failed, triage agent writes its report
into `$GITHUB_STEP_SUMMARY` → build fails.

### GX Core 1.x version/API notes

Confirmed installed version during testing: **`great_expectations==1.19.0`**.
The API is the post-1.0 rewrite (Fluent API): `context.data_sources.add_*()`,
`context.suites.add()`, `ValidationDefinition`, `Checkpoint` — not the
older 0.18.x YAML-config style. Any sample app should target this API,
not older GX tutorials found online (many still show 0.18.x syntax).

## The Claude Skill (`agentic-data-quality-checks.skill`)

A generalized, packaged version of the same workflow, built via
Anthropic's `skill-creator` tooling, meant to be reusable against *any*
table/database in future Claude conversations (not hardcoded to
`orders`). Contents:

```
agentic-data-quality-checks/
├── SKILL.md                          # workflow instructions for Claude
├── scripts/
│   ├── profile_and_suggest.py        # generalized (SQLAlchemy-based, CLI args:
│   │                                 #   --connection-string, --table, --output)
│   └── triage_failures.py            # generalized (CLI arg: --result-file)
└── references/
    ├── gx_core_api.md                # connection snippets: SQLite/Postgres/
    │                                 #   Snowflake/Redshift/Databricks/pandas
    └── ci_workflow_template.yml      # parameterized GitHub Actions template
```

Key change from the repo version: the profiling agent now uses
`sqlalchemy.inspect()` and generic SQL instead of SQLite-specific
`PRAGMA table_info`, so it works against any SQLAlchemy-supported
backend. Re-tested against the same `orders.db` after the rewrite and
confirmed identical behavior (same duplicate detection, same
`customer_id` false positive, same triage output) — the generalization
didn't change the results, just the portability.

## The article (`article/medium_article.md`)

Title: *"I Put an Agent in Front of Great Expectations. It Caught Real
Bugs — and Made a Real Mistake."* Structured around: the setup → what
agent 1 got right (the duplicate) → what it got wrong (customer_id
false positive, enum bug baked in) → why that's the argument for
human-in-the-loop review, not a flaw → running the checks → agent 2
turning failures into a to-do list → CI wiring → an honest closing
caveat that agentic profiling reasons from what's there, not what
should be there.

## What "sample app" could mean here — pick a direction

This context doc doesn't assume which of these you want; worth deciding
before building:

1. **A web UI wrapping the existing pipeline** — e.g. upload/point at a
   table, see the profiling agent's proposals rendered as an
   accept/reject checklist (rather than a raw `.py` file to read), run
   checks, see the triage report as a dashboard instead of markdown.
   This would make the "human reviews before merging" step from the
   article tangible/interactive rather than "edit a Python file."
2. **A CLI/library** — package `profile_and_suggest.py` +
   `triage_failures.py` as an installable tool (`pip install ...`) with
   a cleaner command interface, usable against any project's database
   without copying files around.
3. **A multi-table / real-warehouse demo** — extend beyond the single
   `orders` SQLite table to something with foreign key relationships
   across multiple tables, to specifically exercise (and then fix) the
   PK-vs-FK heuristic bug found above.
4. **An end-to-end "self-healing" extension** — have the triage agent
   optionally open a GitHub issue automatically for high-severity
   failures (mentioned as a stretch idea in the README's "Extending
   this" section but not built).

## Known gaps / things not yet done

- The PK-vs-FK heuristic in `profile_and_suggest.py` is not fixed, only
  flagged — a real improvement would check whether the column name
  matches the table's own primary key convention (e.g. `order_id` on
  `orders`) vs. referencing another table (`customer_id` referring to a
  `customers` table).
- No real Postgres/Snowflake/etc. connection has actually been tested —
  only SQLite. The connection snippets in `gx_core_api.md` are from GX's
  own documentation, not independently verified in this conversation.
- The LLM-review and LLM-triage-summary code paths (`ANTHROPIC_API_KEY`
  branch) were written but never actually exercised in testing, since no
  API key was available in the sandbox — only the heuristic-only paths
  were confirmed working.
