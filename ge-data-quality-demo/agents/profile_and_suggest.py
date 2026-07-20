"""
profile_and_suggest.py  (Agent 1)
----------------------------------
Profiles every column in a SQL table and writes a *draft* expectation suite
as a Python file. Nothing is auto-applied — a human must review the output
before running it.

Key design principle
--------------------
The agent proposes; it does not decide.  Every proposal includes a plain-
English rationale.  Anything the heuristics can't be confident about is
emitted as a  # REVIEW:  comment rather than a runnable expectation.

Known heuristic limitations (documented, not hidden):
  • The "_id suffix → uniqueness" rule catches PKs but also fires on FKs
    (e.g. customer_id on an orders table).  FK columns are flagged with
    # REVIEW: rather than a uniqueness expectation.
  • Enum detection reflects what's in the data, not what *should* be there.
    If bad values are already present they will be included in the proposed
    set — the human reviewer must verify against the authoritative list.

Usage
-----
    # Heuristic-only (no API key required)
    python agents/profile_and_suggest.py

    # With LLM review layer on top
    ANTHROPIC_API_KEY=sk-... python agents/profile_and_suggest.py

    # Against a different database / table
    python agents/profile_and_suggest.py \\
        --connection-string "postgresql+psycopg2://user:pw@host/db" \\
        --table customers \\
        --output gx_checks/suggested_customers_suite.py
"""

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONNECTION = f"sqlite:///{os.path.join(REPO_ROOT, 'data', 'orders.db')}"
DEFAULT_TABLE = "orders"
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "gx_checks", "suggested_suite.py")

# Columns whose names suggest they may be low-cardinality enum-like fields
ENUM_CARDINALITY_THRESHOLD = 20

# Columns whose names end with these strings are treated as ID columns
ID_SUFFIXES = ("_id", "id")

# The table's own PK column name pattern (heuristic: <table>_id or just id)
# We use this to distinguish PK ids (→ uniqueness expectation) from FK ids
# (→ REVIEW comment).
def _is_likely_pk(column_name: str, table_name: str) -> bool:
    """
    Return True if the column is probably the table's own PK.
    Heuristic: column name == "<table>_id" or column name == "id".
    """
    return column_name in (f"{table_name}_id", "id")


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------

def profile_table(engine: sa.Engine, table_name: str) -> dict[str, Any]:
    """
    Return a dict keyed by column name, each value a stats dict:
      null_count, null_pct, cardinality, min_val, max_val,
      sample_values, col_type (SQLAlchemy type string)
    """
    insp = sa_inspect(engine)
    columns = insp.get_columns(table_name)
    col_names = [c["name"] for c in columns]
    col_types = {c["name"]: str(c["type"]) for c in columns}

    stats: dict[str, Any] = {}

    with engine.connect() as conn:
        total_rows = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
        ).scalar()

        for col in col_names:
            # Null count
            null_count = conn.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM {table_name} "  # noqa: S608
                    f"WHERE {col} IS NULL"
                )
            ).scalar()

            # Cardinality (distinct non-null values)
            cardinality = conn.execute(
                sa.text(
                    f"SELECT COUNT(DISTINCT {col}) FROM {table_name}"  # noqa: S608
                )
            ).scalar()

            # Min / max
            min_val = conn.execute(
                sa.text(f"SELECT MIN({col}) FROM {table_name}")  # noqa: S608
            ).scalar()
            max_val = conn.execute(
                sa.text(f"SELECT MAX({col}) FROM {table_name}")  # noqa: S608
            ).scalar()

            # Sample distinct values (up to 25)
            rows = conn.execute(
                sa.text(
                    f"SELECT DISTINCT {col} FROM {table_name} "  # noqa: S608
                    f"WHERE {col} IS NOT NULL LIMIT 25"
                )
            ).fetchall()
            sample_values = [r[0] for r in rows]

            stats[col] = {
                "col_type": col_types[col],
                "total_rows": total_rows,
                "null_count": null_count,
                "null_pct": round(null_count / total_rows * 100, 2) if total_rows else 0,
                "cardinality": cardinality,
                "min_val": min_val,
                "max_val": max_val,
                "sample_values": sample_values,
            }

    return stats


# ---------------------------------------------------------------------------
# Heuristic rule engine
# ---------------------------------------------------------------------------

class Proposal:
    """Represents a single suggested expectation or a review flag."""

    def __init__(
        self,
        kind: str,           # "expectation" | "review"
        expectation_type: str,
        column: str,
        kwargs: dict,
        rationale: str,
    ):
        self.kind = kind
        self.expectation_type = expectation_type
        self.column = column
        self.kwargs = kwargs
        self.rationale = rationale

    def to_code(self) -> str:
        kwargs_str = json.dumps(self.kwargs, default=str)
        if self.kind == "review":
            return (
                f"    # REVIEW: {self.rationale}\n"
                f"    # suite.add_expectation(\n"
                f"    #     {self.expectation_type}(**{kwargs_str})\n"
                f"    # )\n"
            )
        return (
            f"    # {self.rationale}\n"
            f"    suite.add_expectation(\n"
            f"        {self.expectation_type}(**{json.dumps(self.kwargs, default=str)})\n"
            f"    )\n"
        )


def apply_heuristics(
    stats: dict[str, Any],
    table_name: str,
) -> list[Proposal]:
    """
    Walk each column's stats and emit Proposal objects based on heuristic rules.
    """
    proposals: list[Proposal] = []
    today = date.today().isoformat()

    # Table-level: row count
    sample_col = next(iter(stats.values()))
    total_rows = sample_col["total_rows"]
    proposals.append(
        Proposal(
            kind="expectation",
            expectation_type="ExpectTableRowCountToBeBetween",
            column="(table)",
            kwargs={"min_value": max(1, total_rows // 2), "max_value": total_rows * 2},
            rationale=(
                f"Table currently has {total_rows} rows. "
                "Guard against accidental truncation or runaway growth."
            ),
        )
    )

    for col, s in stats.items():
        null_pct = s["null_pct"]
        cardinality = s["cardinality"]
        total = s["total_rows"]
        col_lower = col.lower()

        # ---- Nullability ----
        if null_pct == 0:
            proposals.append(
                Proposal(
                    kind="expectation",
                    expectation_type="ExpectColumnValuesToNotBeNull",
                    column=col,
                    kwargs={"column": col},
                    rationale=(
                        f"`{col}` has 0 nulls in the current data. "
                        "Propose NOT NULL constraint."
                    ),
                )
            )
        elif null_pct < 5:
            proposals.append(
                Proposal(
                    kind="review",
                    expectation_type="ExpectColumnValuesToNotBeNull",
                    column=col,
                    kwargs={"column": col},
                    rationale=(
                        f"`{col}` has {null_pct}% nulls — low but non-zero. "
                        "Decide if nulls are intentional before adding NOT NULL."
                    ),
                )
            )

        # ---- Uniqueness for ID columns ----
        col_looks_like_id = any(col_lower.endswith(suffix) for suffix in ID_SUFFIXES)
        if col_looks_like_id:
            if _is_likely_pk(col_lower, table_name):
                # Check whether the data already has duplicates
                if cardinality < total:
                    proposals.append(
                        Proposal(
                            kind="review",
                            expectation_type="ExpectColumnValuesToBeUnique",
                            column=col,
                            kwargs={"column": col},
                            rationale=(
                                f"`{col}` looks like a PK but already has duplicates "
                                f"({total - cardinality} duplicate rows detected). "
                                "Fix upstream before adding a uniqueness expectation."
                            ),
                        )
                    )
                else:
                    proposals.append(
                        Proposal(
                            kind="expectation",
                            expectation_type="ExpectColumnValuesToBeUnique",
                            column=col,
                            kwargs={"column": col},
                            rationale=(
                                f"`{col}` appears to be the table's primary key. "
                                "Enforce uniqueness."
                            ),
                        )
                    )
            else:
                # Likely a FK — uniqueness doesn't apply
                proposals.append(
                    Proposal(
                        kind="review",
                        expectation_type="ExpectColumnValuesToBeUnique",
                        column=col,
                        kwargs={"column": col},
                        rationale=(
                            f"`{col}` ends in '_id' but is probably a foreign key "
                            f"(cardinality={cardinality} vs {total} rows). "
                            "Foreign keys should NOT have a uniqueness expectation — "
                            "remove this if customers/entities can appear multiple times."
                        ),
                    )
                )

        # ---- Enum / value-set for low-cardinality text columns ----
        col_type_upper = s["col_type"].upper()
        is_text = any(t in col_type_upper for t in ("TEXT", "VARCHAR", "CHAR", "STRING"))
        if is_text and 2 <= cardinality <= ENUM_CARDINALITY_THRESHOLD:
            value_set = sorted(str(v) for v in s["sample_values"] if v is not None)
            proposals.append(
                Proposal(
                    kind="review",
                    expectation_type="ExpectColumnValuesToBeInSet",
                    column=col,
                    kwargs={"column": col, "value_set": value_set},
                    rationale=(
                        f"`{col}` has low cardinality ({cardinality} distinct values). "
                        f"Proposed set: {value_set}. "
                        "WARNING: this set is derived from observed data — it may include "
                        "invalid values already present. Verify against the authoritative "
                        "list before enabling."
                    ),
                )
            )

        # ---- Numeric range ----
        is_numeric = any(
            t in col_type_upper
            for t in ("INT", "REAL", "FLOAT", "NUMERIC", "DECIMAL", "DOUBLE")
        )
        if is_numeric and s["min_val"] is not None:
            min_v = s["min_val"]
            max_v = s["max_val"]
            # Only suggest a lower bound if observed min >= 0 (likely non-negative)
            if min_v >= 0:
                proposals.append(
                    Proposal(
                        kind="expectation",
                        expectation_type="ExpectColumnValuesToBeBetween",
                        column=col,
                        kwargs={"column": col, "min_value": 0},
                        rationale=(
                            f"`{col}` observed min={min_v}, max={max_v}. "
                            "Looks non-negative — propose min_value=0."
                        ),
                    )
                )

        # ---- Date range ----
        is_date_col = "date" in col_lower or "time" in col_lower
        if is_date_col and is_text:
            proposals.append(
                Proposal(
                    kind="expectation",
                    expectation_type="ExpectColumnValuesToMatchRegex",
                    column=col,
                    kwargs={"column": col, "regex": r"^\d{4}-\d{2}-\d{2}$"},
                    rationale=f"`{col}` looks like an ISO date column. Enforce YYYY-MM-DD format.",
                )
            )
            proposals.append(
                Proposal(
                    kind="review",
                    expectation_type="ExpectColumnValuesToBeBetween",
                    column=col,
                    kwargs={"column": col, "max_value": today},
                    rationale=(
                        f"`{col}` — consider capping max_value at today ({today}) "
                        "to catch future-dated records. Update the date in production use."
                    ),
                )
            )

    return proposals


# ---------------------------------------------------------------------------
# LLM review layer (optional — requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

def llm_review(
    proposals: list[Proposal],
    stats: dict[str, Any],
    table_name: str,
) -> str:
    """
    Send the profiling stats and heuristic proposals to Claude for a review.
    Returns the model's commentary as a string, or an empty string if no key.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        print(
            "[profile_and_suggest] `anthropic` package not installed. "
            "Skipping LLM review. Run: pip install anthropic",
            file=sys.stderr,
        )
        return ""

    client = anthropic.Anthropic(api_key=api_key)

    stats_summary = json.dumps(
        {
            col: {
                k: v
                for k, v in s.items()
                if k in ("null_pct", "cardinality", "min_val", "max_val", "col_type")
            }
            for col, s in stats.items()
        },
        default=str,
        indent=2,
    )

    proposals_summary = "\n".join(
        f"[{p.kind.upper()}] {p.expectation_type} on `{p.column}`: {p.rationale}"
        for p in proposals
    )

    prompt = f"""You are a senior data engineer reviewing auto-generated Great Expectations proposals.

Table: `{table_name}`

Column statistics:
{stats_summary}

Heuristic proposals:
{proposals_summary}

Please:
1. Flag any proposals that look incorrect or risky.
2. Suggest any important checks that were missed.
3. Comment on any columns that warrant special attention.

Be concise. Use bullet points."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

HEADER_TEMPLATE = '''\
"""
suggested_suite.py  —  AUTO-GENERATED by profile_and_suggest.py
---------------------------------------------------------------
THIS FILE IS A DRAFT. Do not run it without human review.

Generated for table : {table_name}
Connection          : {connection_string}
Generated at        : {timestamp}

How to use
----------
1. Read every # REVIEW: comment and decide whether to enable, modify, or drop it.
2. Verify enum value sets against authoritative source-of-truth lists.
3. Copy accepted expectations into gx_checks/run_checks.py (or import this module).

{llm_commentary}
"""

import great_expectations as gx
from great_expectations.core.expectation_suite import ExpectationSuite
from great_expectations.expectations import (
    ExpectColumnValuesToBeInSet,
    ExpectColumnValuesToBeUnique,
    ExpectColumnValuesToNotBeNull,
    ExpectColumnValuesToBeBetween,
    ExpectColumnValuesToMatchRegex,
    ExpectTableRowCountToBeBetween,
)


def build_suggested_suite() -> ExpectationSuite:
    suite = ExpectationSuite(name="{suite_name}")

'''

FOOTER = '''\

    return suite


if __name__ == "__main__":
    suite = build_suggested_suite()
    print(f"Suite '{suite.name}' built with {len(suite.expectations)} expectations.")
'''


def write_output(
    proposals: list[Proposal],
    table_name: str,
    connection_string: str,
    output_path: str,
    llm_commentary: str = "",
) -> None:
    from datetime import datetime

    commentary_block = ""
    if llm_commentary:
        commentary_block = (
            "LLM Review\n"
            "----------\n"
            + "\n".join(f"# {line}" for line in llm_commentary.splitlines())
        )

    header = HEADER_TEMPLATE.format(
        table_name=table_name,
        connection_string=connection_string,
        timestamp=datetime.now().isoformat(timespec="seconds"),
        llm_commentary=commentary_block,
        suite_name=f"{table_name}_suggested",
    )

    body = "\n".join(p.to_code() for p in proposals)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(header + body + FOOTER)

    print(f"[profile_and_suggest] Draft suite written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile a SQL table and draft a GX expectation suite."
    )
    parser.add_argument(
        "--connection-string",
        default=DEFAULT_CONNECTION,
        help="SQLAlchemy connection string (default: orders.db)",
    )
    parser.add_argument(
        "--table",
        default=DEFAULT_TABLE,
        help="Table name to profile (default: orders)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Path to write the suggested suite (default: gx_checks/suggested_suite.py)",
    )
    args = parser.parse_args()

    print(f"[profile_and_suggest] Connecting to: {args.connection_string}")
    print(f"[profile_and_suggest] Profiling table: {args.table}")

    engine = sa.create_engine(args.connection_string)

    stats = profile_table(engine, args.table)
    print(f"[profile_and_suggest] Profiled {len(stats)} columns.")

    proposals = apply_heuristics(stats, args.table)
    print(
        f"[profile_and_suggest] Generated {len(proposals)} proposals "
        f"({sum(1 for p in proposals if p.kind == 'expectation')} runnable, "
        f"{sum(1 for p in proposals if p.kind == 'review')} flagged for review)."
    )

    llm_commentary = llm_review(proposals, stats, args.table)
    if llm_commentary:
        print("[profile_and_suggest] LLM review appended.")
    else:
        print("[profile_and_suggest] No LLM review (set ANTHROPIC_API_KEY to enable).")

    write_output(
        proposals,
        table_name=args.table,
        connection_string=args.connection_string,
        output_path=args.output,
        llm_commentary=llm_commentary,
    )


if __name__ == "__main__":
    main()
