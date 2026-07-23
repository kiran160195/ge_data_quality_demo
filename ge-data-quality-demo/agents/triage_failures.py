"""
triage_failures.py  (Agent 2)
------------------------------
Reads the raw checkpoint result JSON written by gx_checks/run_checks.py and
produces a prioritized, human-readable triage report.

Priority logic
--------------
  CRITICAL  — identifier columns (order_id, id, *_id as PK) or financial
              columns (amount, price, revenue, total, cost)
  HIGH      — status / type / category columns; date / time columns
  MEDIUM    — everything else

For each failure category, a specific suggested next step is included:
  - Duplicate → check upstream for retry logic or de-duplication gaps
  - Null spike → check the ETL step that populates this column
  - Unexpected category value → confirm with the business what the valid set is
  - Range violation → check for sign errors, unit mismatches, or upstream bugs
  - Date violation → check for timezone handling or clock-skew issues

Optional: set ANTHROPIC_API_KEY to have Claude turn the ranked list into a
          Slack-ready incident summary.

Report output (Markdown, HTML, optional Slack summary) is handled by report.py.

Usage
-----
    # Heuristic triage only
    python agents/triage_failures.py

    # With LLM Slack summary
    ANTHROPIC_API_KEY=sk-... python agents/triage_failures.py

    # Against a different result file
    python agents/triage_failures.py --result-file path/to/result.json
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from report import format_report, write_reports

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RESULT_FILE = os.path.join(REPO_ROOT, "gx_checks", "last_result.json")

# ---------------------------------------------------------------------------
# Priority classification
# ---------------------------------------------------------------------------

CRITICAL_PATTERNS = (
    "order_id", "_id", "id", "amount", "price", "revenue", "total", "cost",
    "payment", "balance", "fee", "charge",
)
HIGH_PATTERNS = (
    "status", "state", "type", "category", "date", "time", "timestamp",
)


def classify_priority(column: str) -> str:
    col_lower = (column or "").lower()
    if any(p in col_lower for p in CRITICAL_PATTERNS):
        return "CRITICAL"
    if any(p in col_lower for p in HIGH_PATTERNS):
        return "HIGH"
    return "MEDIUM"


# ---------------------------------------------------------------------------
# Failure categorization
# ---------------------------------------------------------------------------

def categorize_failure(expectation_type: str, column: str) -> tuple[str, str]:
    """
    Returns (category_label, suggested_next_step).
    """
    et = expectation_type.lower()
    col = (column or "").lower()

    if "unique" in et:
        return (
            "Duplicate values",
            f"Check upstream for retry logic or missing de-duplication on `{column}`. "
            "Query: SELECT {col}, COUNT(*) FROM <table> GROUP BY {col} HAVING COUNT(*) > 1.".format(col=column),
        )
    if "not_be_null" in et or "notnull" in et:
        return (
            "Unexpected nulls",
            f"Trace the ETL step that writes `{column}`. Check for LEFT JOIN gaps, "
            "optional API fields being silently dropped, or a schema migration that "
            "added the column after existing rows were written.",
        )
    if "be_in_set" in et or "inset" in et:
        return (
            "Unexpected category value",
            f"`{column}` contains values not in the approved set. Confirm the authoritative "
            "valid-values list with the owning team. If the new value is legitimate, "
            "update the expectation; if not, trace where the bad value was introduced.",
        )
    if "be_between" in et or "between" in et:
        if "date" in col or "time" in col:
            return (
                "Date/time range violation",
                f"`{column}` has values outside the expected range. Check for timezone "
                "handling bugs, clock skew in the source system, or future-dated test "
                "records leaking into production.",
            )
        return (
            "Numeric range violation",
            f"`{column}` has out-of-range values. Check for sign errors (e.g. credits "
            "recorded as negatives when positives are expected), unit mismatches "
            "(cents vs dollars), or upstream calculation bugs.",
        )
    if "regex" in et or "match_regex" in et:
        return (
            "Format violation",
            f"`{column}` has values that don't match the expected format. Check the "
            "source system's serialization logic and any ETL transformations.",
        )
    if "row_count" in et:
        return (
            "Row count out of range",
            "The table has unexpectedly few or many rows. Check for a failed load, "
            "a partial truncate, or a runaway insert loop.",
        )

    return (
        "Expectation failure",
        "Investigate the raw result for details and trace back to the source system.",
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TriagedFailure:
    expectation_type: str
    column: str
    priority: str
    category: str
    next_step: str
    unexpected_count: int | None
    unexpected_percent: float | None
    partial_unexpected_list: list[Any] = field(default_factory=list)

    @property
    def priority_order(self) -> int:
        return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}.get(self.priority, 3)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_failures(result_dict: dict) -> list[TriagedFailure]:
    """Extract failed expectations from a GX checkpoint result dict."""
    failures: list[TriagedFailure] = []

    validation_results = (
        result_dict
        .get("validation_results", [{}])[0]
        .get("expectations", [])
    )

    for r in validation_results:
        if r.get("success", True):
            continue

        exp_type = r.get("expectation_type", "unknown")
        kwargs = r.get("kwargs", {})
        column = kwargs.get("column", "(table-level)")

        result_detail = r.get("result", {})
        unexpected_count = result_detail.get("unexpected_count")
        unexpected_percent = result_detail.get("unexpected_percent")
        partial_unexpected_list = result_detail.get("partial_unexpected_list", [])

        priority = classify_priority(column)
        category, next_step = categorize_failure(exp_type, column)

        failures.append(
            TriagedFailure(
                expectation_type=exp_type,
                column=column,
                priority=priority,
                category=category,
                next_step=next_step,
                unexpected_count=unexpected_count,
                unexpected_percent=unexpected_percent,
                partial_unexpected_list=partial_unexpected_list,
            )
        )

    return sorted(failures, key=lambda f: f.priority_order)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def triage(result_file: str = DEFAULT_RESULT_FILE) -> None:
    if not os.path.exists(result_file):
        sys.exit(
            f"[triage_failures] Result file not found: {result_file}\n"
            "Run `python gx_checks/run_checks.py` first."
        )

    with open(result_file) as f:
        result_dict = json.load(f)

    # Count total expectations run
    all_results = (
        result_dict
        .get("validation_results", [{}])[0]
        .get("expectations", [])
    )
    total = len(all_results)

    failures = parse_failures(result_dict)

    if not failures:
        print("[triage_failures] All expectations passed. Nothing to triage.")
        return

    # Print the ranked report to stdout (captured by CI as the step summary)
    print(format_report(failures, total))

    # Write all report files (Markdown, HTML, optional Slack summary)
    write_reports(failures, total, output_dir=os.path.dirname(result_file))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage a GX checkpoint result JSON into a prioritized report."
    )
    parser.add_argument(
        "--result-file",
        default=DEFAULT_RESULT_FILE,
        help="Path to last_result.json (default: gx_checks/last_result.json)",
    )
    args = parser.parse_args()
    triage(args.result_file)


if __name__ == "__main__":
    main()
