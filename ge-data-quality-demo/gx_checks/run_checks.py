"""
run_checks.py
-------------
Runs a Great Expectations Core 1.x suite against data/orders.db and writes
the result to gx_checks/last_result.json.

Exits with code 1 if any expectation fails (suitable as a CI gate).
Exits with code 0 if all expectations pass.

GX 1.x Fluent API pattern used throughout:
  DataSource → DataAsset → BatchDefinition → ExpectationSuite
  → ValidationDefinition → Checkpoint

Run:
    python gx_checks/run_checks.py
"""

import json
import os
import sys
from datetime import date

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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "orders.db")
RESULT_PATH = os.path.join(REPO_ROOT, "gx_checks", "last_result.json")
GX_DIR = os.path.join(REPO_ROOT, ".gx")  # ephemeral GX context directory

# Today's date as ISO string — used for the date-range expectation
TODAY = date.today().isoformat()

VALID_STATUSES = ["placed", "shipped", "delivered", "cancelled", "refunded"]


# ---------------------------------------------------------------------------
# Build the expectation suite
# ---------------------------------------------------------------------------

def build_suite() -> ExpectationSuite:
    suite = ExpectationSuite(name="orders_suite")

    # 1. Row count sanity check
    suite.add_expectation(
        ExpectTableRowCountToBeBetween(min_value=100, max_value=10_000)
    )

    # 2. order_id must be unique (PK)
    suite.add_expectation(
        ExpectColumnValuesToBeUnique(column="order_id")
    )

    # 3. customer_id must not be null
    suite.add_expectation(
        ExpectColumnValuesToNotBeNull(column="customer_id")
    )

    # 4. amount must be >= 0
    suite.add_expectation(
        ExpectColumnValuesToBeBetween(column="amount", min_value=0)
    )

    # 5. status must be one of the known lifecycle values
    suite.add_expectation(
        ExpectColumnValuesToBeInSet(
            column="status",
            value_set=VALID_STATUSES,
        )
    )

    # 6. order_date must be a valid ISO date string (YYYY-MM-DD)
    suite.add_expectation(
        ExpectColumnValuesToMatchRegex(
            column="order_date",
            regex=r"^\d{4}-\d{2}-\d{2}$",
        )
    )

    # 7. order_date must not be in the future
    suite.add_expectation(
        ExpectColumnValuesToBeBetween(
            column="order_date",
            max_value=TODAY,
        )
    )

    return suite


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_checks(db_path: str = DB_PATH, result_path: str = RESULT_PATH) -> bool:
    """
    Returns True if all expectations pass, False otherwise.
    Always writes results to result_path.
    """
    if not os.path.exists(db_path):
        sys.exit(
            f"[run_checks] Database not found at {db_path}. "
            "Run `python data/seed_db.py` first."
        )

    connection_string = f"sqlite:///{db_path}"

    # Use an ephemeral (in-memory) GX context so we don't pollute the repo
    # with a persistent great_expectations/ directory during development.
    context = gx.get_context(mode="ephemeral")

    # Data source & asset
    data_source = context.data_sources.add_sqlite(
        name="orders_sqlite",
        connection_string=connection_string,
    )
    data_asset = data_source.add_table_asset(
        name="orders_asset",
        table_name="orders",
    )

    # Batch definition — "whole table" batch
    batch_definition = data_asset.add_batch_definition_whole_table(
        name="orders_batch"
    )

    # Suite
    suite = build_suite()
    context.suites.add(suite)

    # Validation definition
    validation_definition = context.validation_definitions.add(
        gx.ValidationDefinition(
            name="orders_validation",
            data=batch_definition,
            suite=suite,
        )
    )

    # Checkpoint
    checkpoint = context.checkpoints.add(
        gx.Checkpoint(
            name="orders_checkpoint",
            validation_definitions=[validation_definition],
        )
    )

    # Run
    print("[run_checks] Running checkpoint against orders table...")
    result = checkpoint.run()

    # Serialize to JSON
    result_dict = result.describe_dict()
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(result_dict, f, indent=2, default=str)

    # Summary
    passed = result.success
    _print_summary(result_dict)

    print(f"\n[run_checks] Result written to {result_path}")
    print(f"[run_checks] Overall success: {passed}")

    return passed


def _print_summary(result_dict: dict) -> None:
    """Print a human-readable per-expectation summary."""
    print("\n--- Expectation Results ---")
    try:
        validation_results = (
            result_dict
            .get("validation_results", [{}])[0]
            .get("expectations", [])
        )
        for r in validation_results:
            expectation_type = r.get("expectation_type", "unknown")
            column = r.get("kwargs", {}).get("column", "table-level")
            success = r.get("success", False)
            status = "PASS" if success else "FAIL"
            print(f"  [{status}] {expectation_type} ({column})")
    except (KeyError, IndexError, TypeError) as exc:
        print(f"  (could not parse detailed results: {exc})")
    print("---------------------------")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    all_passed = run_checks()
    sys.exit(0 if all_passed else 1)
