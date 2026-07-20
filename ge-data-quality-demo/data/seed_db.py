"""
seed_db.py
----------
Creates data/orders.db: a SQLite database with an `orders` table containing
500 synthetic e-commerce orders and 5 deliberately injected data quality issues.

Issues injected (for use in GX checks and agent demos):
  1. A few null customer_id values
  2. One duplicate order_id
  3. One negative amount
  4. One invalid status ("backordered" — not a real lifecycle value)
  5. One future-dated order_date

Run:
    python data/seed_db.py
"""

import os
import random
import sqlite3
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RANDOM_SEED = 42
N_ROWS = 500
DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")

VALID_STATUSES = ["placed", "shipped", "delivered", "cancelled", "refunded"]

random.seed(RANDOM_SEED)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_date(start: date, end: date) -> str:
    delta = (end - start).days
    return (start + timedelta(days=random.randint(0, delta))).isoformat()


def generate_clean_rows(n: int) -> list[tuple]:
    """Generate n clean, valid order rows."""
    start = date(2023, 1, 1)
    end = date(2024, 12, 31)
    rows = []
    for i in range(1, n + 1):
        order_id = i
        customer_id = random.randint(1000, 9999)
        amount = round(random.uniform(5.0, 500.0), 2)
        status = random.choice(VALID_STATUSES)
        order_date = random_date(start, end)
        rows.append((order_id, customer_id, amount, status, order_date))
    return rows


# ---------------------------------------------------------------------------
# Inject issues
# ---------------------------------------------------------------------------

def inject_issues(rows: list[tuple]) -> list[tuple]:
    """
    Mutate a copy of rows to inject the 5 quality issues described in the
    project spec. Returns the modified list.
    """
    rows = [list(r) for r in rows]  # make mutable

    # Issue 1: Null customer_id on a handful of rows
    null_indices = random.sample(range(len(rows)), 4)
    for idx in null_indices:
        rows[idx][1] = None  # customer_id

    # Issue 2: Duplicate order_id — copy row 0's order_id onto row 10
    rows[10][0] = rows[0][0]

    # Issue 3: Negative amount on row 25
    rows[25][2] = -19.99

    # Issue 4: Invalid status on row 50
    rows[50][3] = "backordered"

    # Issue 5: Future-dated order_date on row 75
    future_date = (date.today() + timedelta(days=30)).isoformat()
    rows[75][4] = future_date

    return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# Build DB
# ---------------------------------------------------------------------------

def build_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Remove stale DB so re-runs are idempotent
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE orders (
            order_id    INTEGER,
            customer_id INTEGER,
            amount      REAL,
            status      TEXT,
            order_date  TEXT
        )
    """)

    rows = generate_clean_rows(N_ROWS)
    rows = inject_issues(rows)

    cur.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
        rows,
    )

    conn.commit()
    conn.close()

    print(f"[seed_db] Created {db_path} with {N_ROWS} rows and 5 injected issues.")
    _verify(db_path)


def _verify(db_path: str) -> None:
    """Quick sanity-check — print counts for each injected issue."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM orders WHERE customer_id IS NULL")
    nulls = cur.fetchone()[0]

    cur.execute(
        "SELECT order_id, COUNT(*) c FROM orders GROUP BY order_id HAVING c > 1"
    )
    dupes = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM orders WHERE amount < 0")
    negatives = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM orders WHERE status NOT IN "
        "('placed','shipped','delivered','cancelled','refunded')"
    )
    bad_status = cur.fetchone()[0]

    today = date.today().isoformat()
    cur.execute("SELECT COUNT(*) FROM orders WHERE order_date > ?", (today,))
    future = cur.fetchone()[0]

    conn.close()

    print(f"  null customer_id  : {nulls}   (expected ~4)")
    print(f"  duplicate order_id: {dupes}  (expected 1 pair)")
    print(f"  negative amount   : {negatives}   (expected 1)")
    print(f"  invalid status    : {bad_status}   (expected 1 — 'backordered')")
    print(f"  future order_date : {future}   (expected 1)")


if __name__ == "__main__":
    build_db()
