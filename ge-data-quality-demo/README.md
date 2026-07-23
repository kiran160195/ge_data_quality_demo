# I Put an Agent in Front of Great Expectations for Data Quality Checks. It Caught Real Bugs - and Made a Real Mistake.
Most data quality tutorials show you how to check a table you already understand perfectly. The harder problem is the table you don't - the one a teammate just handed you, with no idea yet what "healthy" looks like.
So I built a small pipeline where an agent takes the first pass: profile a table, propose checks, run them with Great Expectations (GX Core 1.x), and - when something fails - triage the results into a prioritized report instead of raw JSON. Then I ran it against deliberately messy data to see where the agent got it right, and where it didn't.
Full code: https://github.com/kiran160195/ge_data_quality_demo/tree/main
## The setup
A synthetic `orders` table, 500 rows, seeded with five deliberate bugs: null `customer_id`s, a duplicate `order_id`, a negative `amount`, an invalid `status` ("backordered"), and a future-dated `order_date`.
This is roughly how agentic data quality products work under the hood (GX's ExpectAI, Alation's Data Quality Agent) - profile, propose, review, run, triage. Building a minimal version yourself is the fastest way to see where "agentic" earns its keep, and where it still needs a human.
## Agent 1 proposed checks. It got one very right, and two instructively wrong.
The profiling agent scans each column for nulls, cardinality, and range, then applies simple heuristics to draft an `ExpectationSuite`. Nothing gets applied automatically - it writes a reviewable draft, each proposal with a rationale attached.
col_looks_like_id = any(col_lower.endswith(suffix) for suffix in ID_SUFFIXES)

is_text = any(t in col_type_upper for t in ("TEXT", "VARCHAR", "CHAR", "STRING"))
is_enum_like = is_text and 2 <= cardinality <= ENUM_CARDINALITY_THRESHOLD

is_numeric = any(
    t in col_type_upper
    for t in ("INT", "REAL", "FLOAT", "NUMERIC", "DECIMAL", "DOUBLE")
) and not col_looks_like_id
Here's the part that makes this worth writing about:
**Right:** `order_id` had one duplicate. Instead of proposing a uniqueness check that would just fail, the agent flagged it:
# REVIEW: `order_id` looks like a PK but already has duplicates
# (1 duplicate row detected). Fix upstream before adding a uniqueness expectation.
# suite.add_expectation(
#     ExpectColumnValuesToBeUnique(**{"column": "order_id"})
# )

*Wrong #1:** the same "`_id` → unique" heuristic also fired on `customer_id` - flagging 99 "duplicates." But a customer placing four orders isn't a bug, it's a foreign key. The heuristic can't tell a primary key from a foreign key by name alone.

# REVIEW: `customer_id` ends in '_id' but is probably a foreign key
# (cardinality=496 vs 500 rows). Foreign keys should NOT have a uniqueness
# expectation - remove this if customers/entities can appear multiple times.
# suite.add_expectation(
#     ExpectColumnValuesToBeUnique(**{"column": "customer_id"})
# )

*Wrong #2:** enum-detection for `status` found 6 distinct values and proposed all 6 as valid - including `"backordered"`, the value deliberately injected as invalid. It reflects what's in the data, not what should be.

# REVIEW: `status` has low cardinality (6 distinct values).
# Proposed set: ['backordered', 'cancelled', 'delivered', 'placed', 'refunded', 'shipped']
# WARNING: this set is derived from observed data - it may include invalid values
# already present. Verify against the authoritative list before enabling.
Neither is a knock on the approach - it's the argument for it. An agent that profiles data can tell you what's *there*; it can't tell you what *should* be there. That gap is exactly why the output is a draft for review, not a check that goes live on its own.
## Running the checks
Once a human has trimmed the draft, the mechanics are standard GX Core 1.x - a Data Source, a Data Asset, a Batch Definition, a Suite, a Validation Definition, a Checkpoint:

4 of 7 checks fail against the seeded data - catching the duplicate, the nulls, the negative amount, and the invalid status. `run_checks.py` exits `1` on any failure, so it drops straight into CI as a gate.
## Agent 2 turns the failure JSON into a to-do list
Checks that fail correctly but get ignored because reading the output is annoying are just alerts nobody reads. So the second agent takes the raw result and ranks it:

Severity ranks identifier/financial columns above status/date fields; each failure type gets a specific next step (duplicate → check upstream retries; null spike → check the ETL step; new category → confirm with the business). With `ANTHROPIC_API_KEY` set, it also asks Claude to write a Slack-ready summary.
## Wired into CI

Point the connection string at a real warehouse via a secret, and this runs on every push and PR.
## What "agentic" buys you here
Not autonomy - a human still approves checks before they go live. What it buys is speed: a faster path from "unfamiliar table" to "reviewable draft of what to check," and from "build failed" to "here's what to do, ranked by severity." It won't replace judgment. It removes the tedium that usually stops judgment from getting applied before something's already broken.
The honest caveat: an agent reasoning from data alone will sometimes propose exactly the wrong thing with total confidence - a legit foreign key flagged as broken, a live bug enshrined as a valid category. That's not a reason to skip it. It's the reason the review step exists. Ship the draft, not the conclusion.
**Full code, including both agents:** [github.com/kiran160195/ge_data_quality_demo](https://github.com/kiran160195/ge_data_quality_demo/tree/main)
 - -
*If the heuristics get something else wrong on your data, I'd like to hear about it - PRs improving the id/foreign-key detection welcome.*## Real-world example: vehicle inventory pipeline

The same pattern — profile a feed, run checks in CI, triage failures into a ranked to-do list — applies directly to a dealer inventory pipeline.

**What the data looks like:** every dealer pushes a vehicle feed (VIN, price, condition, dealer attribution, listing date) that lands in an inventory warehouse. Bad data here has direct business cost: a pricing algorithm running on records with negative or null prices, listings routed to the wrong dealer, invalid condition values confusing search and ranking.

**How the priority logic maps:**

| Column | Priority in triage | Why |
|---|---|---|
| `vin`, `price` | CRITICAL | Identifier and financial — same as `order_id` and `amount` |
| `condition`, `listing_date` | HIGH | Category and date fields |
| Everything else | MEDIUM | |

**Failure-specific next steps for this domain:**

- *VIN duplicate* → check the dealer's feed sync job for retry logic that sends the same record twice.
- *Null dealer attribution* → check the ingestion mapping step; a new dealer onboarded without a mapping will produce nulls for every row in their feed.
- *Unexpected condition value* (e.g. `"wrecked"` appears in data but not in the approved set) → confirm with the merchandising team: is this a new category that needs to be added to the expectation, or a routing bug in the feed parser?

**Wired into CI:** point the connection string at the inventory warehouse via a GitHub Actions secret, then trigger on a schedule or a data-arrival event instead of `on: push`. The pipeline runs on every dealer feed sync, not just on code changes:

```yaml
- name: Run Great Expectations checks
  id: gx
  run: python gx_checks/run_checks.py
  continue-on-error: true

- name: Agent -- triage any failures
  if: steps.gx.outcome == 'failure'
  run: python agents/triage_failures.py >> "$GITHUB_STEP_SUMMARY"

- name: Fail the build if checks failed
  if: steps.gx.outcome == 'failure'
  run: exit 1
```

With `ANTHROPIC_API_KEY` set, the triage agent also asks Claude to write a Slack-ready summary for the data platform team — useful when a failure needs to be escalated to merchandising or a dealer ops contact.

**The honest caveat:** an agent reasoning from data alone will sometimes propose exactly the wrong thing with confidence — a legitimate dealer flagged as a data error, an invalid condition value enshrined as valid because it appeared in the feed. That's not a reason to skip it; it's the reason the human-review step exists. The point isn't autonomy — a human on the data platform or merchandising team still approves checks before they go live. The point is speed: faster from "we just onboarded a new dealer feed we don't fully understand" to "here's a reviewable draft of what to check," and faster from "the pricing algorithm looks off" to "here's exactly which rows and why, ranked by severity."

## Known limitations