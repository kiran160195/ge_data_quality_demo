# I Put an Agent in Front of Great Expectations. It Caught Real Bugs — and Made a Real Mistake.

*How an LLM-powered profiling agent drafted data checks for a messy SQLite table,
what it got right, what it got wrong, and why that's actually the point.*

---

Data quality tooling has a bootstrapping problem. Before you can run Great
Expectations checks, someone has to write them. That someone is usually a
data engineer who has to stare at a table schema, run some summary queries,
and translate "this column should never be null" into Python. It works, but it
doesn't scale — especially when you're onboarding a new table every week.

So I tried something: what if an agent did the profiling and drafted the checks
for me?

The answer is: it caught things I would have caught anyway, it made a mistake I
would not have made, and the combination of those two things tells you something
important about where agents fit in a data quality workflow.

Here's what I built and what happened.

---

## The Setup

I built a small pipeline with two agents and one Great Expectations checkpoint:

```
table  →  [Agent 1: profile & suggest]  →  draft suite (human reviews)
table  →  [GX checkpoint]               →  pass/fail + JSON result
                  │ on failure
                  ▼
        [Agent 2: triage & prioritize]  →  ranked report + next steps
```

The database is a SQLite file with 500 synthetic e-commerce orders. The schema
is simple: `order_id`, `customer_id`, `amount`, `status`, `order_date`. And I
deliberately injected five data quality issues to see what the agent would catch:

1. A few null `customer_id` values
2. One duplicate `order_id`
3. One negative `amount` (`-19.99`)
4. One invalid `status` (`"backordered"` — not a real lifecycle value)
5. One future-dated `order_date`

These are real categories of problems. I've seen all five in production data.

---

## Agent 1: Profile and Suggest

The first agent profiles every column: null counts, cardinality, min/max values,
distinct value samples. It then applies a set of heuristic rules and writes a
draft expectation suite — a Python file with comments explaining the rationale
for each proposal.

Crucially, **it proposes; it does not apply.** Nothing runs automatically. The
output is a file called `suggested_suite.py` that a human has to read before
anything executes.

Let me show you what happened with each of the five injected issues.

### What it got right: the duplicate order_id

The profiling step counted 500 rows and found only 499 distinct `order_id`
values. The heuristic said: "this column ends in `_id`, so it's probably a
primary key — but it already has duplicates, so I can't propose a uniqueness
check."

The output looked like this:

```python
# REVIEW: `order_id` looks like a PK but already has duplicates
# (1 duplicate row detected). Fix upstream before adding a uniqueness expectation.
# suite.add_expectation(
#     ExpectColumnValuesToBeUnique(**{"column": "order_id"})
# )
```

This is exactly right. Writing a uniqueness check that you know will fail isn't
useful. Flagging the duplicate for investigation is the correct move.

### What it got wrong: customer_id

Here's where it went sideways.

The `customer_id` column has low cardinality (each customer can appear on many
orders), and its name ends in `_id`. The heuristic fired:

```python
# REVIEW: `customer_id` ends in '_id' but is probably a foreign key
# (cardinality=496 vs 500 rows). Foreign keys should NOT have a uniqueness
# expectation — remove this if customers/entities can appear multiple times.
# suite.add_expectation(
#     ExpectColumnValuesToBeUnique(**{"column": "customer_id"})
# )
```

The agent was right to flag it — it didn't blindly write a uniqueness check.
But it's still a false positive in the sense that the check should be removed
entirely, not just reviewed. A customer placing multiple orders is not an
anomaly; it's the whole point of having a `customer_id` column on an `orders`
table.

The underlying limitation is that the heuristic can't distinguish a primary key
from a foreign key from the column name alone. `order_id` on an `orders` table
is almost certainly the PK. `customer_id` on an `orders` table is almost
certainly an FK. But the profiler doesn't know the table's name convention or
its relationships. It can only see what's in front of it.

### The enum bug hiding in the data

The `status` column had six distinct values: `placed`, `shipped`, `delivered`,
`cancelled`, `refunded`, and `backordered`. The profiler saw six distinct values
and proposed all six as the valid set:

```python
# REVIEW: `status` has low cardinality (6 distinct values).
# Proposed set: ['backordered', 'cancelled', 'delivered', 'placed', 'refunded', 'shipped']
# WARNING: this set is derived from observed data — it may include invalid values
# already present. Verify against the authoritative list before enabling.
```

The warning is good. But if a reviewer skims this and approves all six values,
they've just institutionalized the bug. The profiler can't know that
`"backordered"` was injected on purpose as an invalid value — it just sees a
string that appears in the data.

This is the core limitation of data-driven profiling: **it reasons from what's
there, not from what should be there.** An authoritative list of valid statuses
lives in the business logic, not in the database.

---

## The Hand-Reviewed Suite

After reading `suggested_suite.py`, I wrote the actual `run_checks.py` with
seven expectations:

1. Row count between 100 and 10,000
2. `order_id` must be unique
3. `customer_id` must not be null
4. `amount` must be ≥ 0
5. `status` must be in `{placed, shipped, delivered, cancelled, refunded}` — five values, not six
6. `order_date` must match `YYYY-MM-DD` format
7. `order_date` must not be in the future

Running this against the seeded database: **4 of 7 fail**, exactly as designed.

```
[PASS] ExpectTableRowCountToBeBetween (table-level)
[FAIL] ExpectColumnValuesToBeUnique (order_id)
[FAIL] ExpectColumnValuesToNotBeNull (customer_id)
[FAIL] ExpectColumnValuesToBeBetween (amount)
[FAIL] ExpectColumnValuesToBeInSet (status)
[PASS] ExpectColumnValuesToMatchRegex (order_date)
[PASS] ExpectColumnValuesToBeBetween (order_date)
```

Wait — why did the `order_date` checks pass? Because the future-dated record
I injected is 30 days out, and the check compares against today's date at
runtime. It caught it correctly. The two date checks both pass when the data is
clean and fail when it isn't.

---

## Agent 2: Triage Failures

The second agent reads the raw checkpoint result JSON and produces a
prioritized report. It doesn't just list failures — it classifies them by
severity and suggests a specific next step for each failure type.

The priority logic:

- **CRITICAL**: identifier columns (`order_id`, `*_id`) and financial columns (`amount`, `price`, etc.)
- **HIGH**: status, type, category, and date columns
- **MEDIUM**: everything else

For each failure type, a specific next step:

- **Duplicate**: check upstream for retry logic or missing de-duplication
- **Null spike**: trace the ETL step that writes this column
- **Unexpected category value**: confirm the valid set with the owning team
- **Range violation**: check for sign errors, unit mismatches, or upstream bugs
- **Date violation**: check for timezone handling or clock-skew

The output for our four failures looked like this:

```
[1] [CRITICAL] Duplicate values
     Column    : order_id
     Check     : ExpectColumnValuesToBeUnique
     Failures  : 1 rows
     Next step : Check upstream for retry logic or missing de-duplication on `order_id`.
                 Query: SELECT order_id, COUNT(*) FROM <table> GROUP BY order_id HAVING COUNT(*) > 1.

[2] [CRITICAL] Unexpected nulls
     Column    : customer_id
     Check     : ExpectColumnValuesToNotBeNull
     Failures  : 4 rows (0.8%)
     Next step : Trace the ETL step that writes `customer_id`. Check for LEFT JOIN gaps,
                 optional API fields being silently dropped, or a schema migration that
                 added the column after existing rows were written.

[3] [CRITICAL] Numeric range violation
     Column    : amount
     Check     : ExpectColumnValuesToBeBetween
     Failures  : 1 rows
     Next step : Check for sign errors (e.g. credits recorded as negatives when positives
                 are expected), unit mismatches (cents vs dollars), or upstream calculation bugs.

[4] [HIGH] Unexpected category value
     Column    : status
     Check     : ExpectColumnValuesToBeInSet
     Failures  : 1 rows
     Sample    : ['backordered']
     Next step : `status` contains values not in the approved set. Confirm the authoritative
                 valid-values list with the owning team. If the new value is legitimate,
                 update the expectation; if not, trace where the bad value was introduced.
```

This is significantly more useful than the raw JSON. The ranking tells you
what to fix first. The next steps tell you where to look. A data engineer
seeing this report knows exactly what to do.

---

## The CI Wire-Up

The full pipeline runs on every push via GitHub Actions:

```yaml
steps:
  - Seed the database
  - Run profiling agent → upload suggested_suite.py as artifact
  - Run GX checks (continue-on-error: true)
  - If failed: run triage agent → write report to step summary
  - Fail build if checks failed
```

The `continue-on-error: true` on the check step is deliberate — it ensures
the triage agent always runs when there are failures, and the report lands in
the GitHub Actions step summary where it's visible to the entire team without
opening any files.

---

## What This Is and What It Isn't

**What it is**: a workflow where an agent does the boring profiling work, flags
what it can, and defers everything it can't be confident about to a human.
The human's job shifts from "write the checks from scratch" to "review a draft
and remove the wrong ones."

**What it isn't**: an autonomous data quality system. The agent's proposals
for `customer_id` and `status` were both wrong in different ways. If either
of them had been applied automatically, the first would have caused false
alarms forever and the second would have quietly blessed a known-bad value.

The two bugs the agent made are structurally different:
- The `customer_id` false positive is a **heuristic limitation** — the rule
  can be improved (check whether the column matches the table's own PK
  convention).
- The `status` enum bug is a **fundamental limitation** — no amount of
  heuristic improvement can tell you what values *should* be in the data.
  That knowledge lives outside the database.

The second kind of limitation is why the "propose, don't auto-apply" design
isn't just a safety feature — it's the correct architecture. An agent that
profiles data is, by definition, reasoning from what exists. Business rules
about what *should* exist are a human input, not something that can be
inferred.

---

## What I'd Build Next

A few natural extensions from here:

- **PK vs FK detection**: check whether a column name matches the table's own
  name convention (`order_id` on `orders`) vs. referencing another table.
  A schema introspection step could make this reliable.

- **Multi-table support**: extend profiling to follow foreign keys and check
  referential integrity across related tables. The `customer_id` issue above
  would be trivially solvable if the profiler knew that a `customers` table
  existed.

- **Self-healing loop**: have the triage agent open a GitHub issue automatically
  for CRITICAL failures. The report is already structured — the issue body
  almost writes itself.

- **Web UI**: render proposals as an accept/reject checklist instead of a
  Python file. The current UX of "read the comments and edit the file" works
  for data engineers; it doesn't work for analysts or product owners who
  should probably also be reviewing these proposals.

---

The code for all of this is on GitHub. The pipeline works end-to-end: seed,
profile, check, triage. Four of seven expectations fail exactly as designed,
and the triage report tells you what to do about each one.

The agent got the hard thing right (detect the duplicate, don't write a check
that will fail). It got an easy-looking thing wrong (FK vs PK). That gap is
more interesting than either result on its own.
