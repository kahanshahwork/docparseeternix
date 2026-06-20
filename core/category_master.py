"""
core/category_master.py — Single source of truth for the Category Master (Module 4).

Touch this file ONLY when you need to add/edit/retire a category or change
its GST treatment. Nothing else in the app hardcodes category logic — the
GST engine and P&L engine both read from the `categories` table that this
file seeds, so changes here propagate everywhere automatically.

Based on current ATO GST/BAS treatment (taxable @10%, GST-free, input-taxed,
and BAS-excluded are different buckets with different reporting effects).

DESIGN: kept deliberately BROAD (~13 categories) rather than granular.
Real bookkeeping for small/medium clients works better with a handful of
broad buckets that vendor memory + AI can land on confidently, rather than
20+ narrow categories where most transactions don't have a clean fit. If a
specific client genuinely needs a finer split later (e.g. a wholesaler
wanting a dedicated Cost of Goods Sold line), add ONE new category here for
that purpose — don't pre-build categories nothing will ever match.
"""

from core.db import get_db

# (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
DEFAULT_CATEGORIES = [
    ("SALES",         "Sales / Trading Income",         "Income",   1, 0.10, "G1",       10),
    ("OTHER_INCOME",  "Other Income",                    "Income",   0, 0.0,  "excluded", 20),

    ("FOOD",          "Food & Meals",                    "Expense",  1, 0.10, "G11",      110),
    ("TRAVEL",        "Travel & Transport",               "Expense",  1, 0.10, "G11",      120),
    ("OFFICE",        "Office & Operating Expenses",      "Expense",  1, 0.10, "G11",      130),
    ("RENT",          "Rent / Lease Expense",             "Expense",  1, 0.10, "G11",      140),
    ("PROFESSIONAL",  "Professional / Contractor Fees",   "Expense",  1, 0.10, "G11",      150),
    ("INSURANCE",     "Insurance",                         "Expense",  1, 0.10, "G11",      160),
    ("MARKETING",     "Marketing & Advertising",           "Expense",  1, 0.10, "G11",      170),
    ("SALARY",        "Salary & Wages",                    "Expense",  0, 0.0,  "excluded", 180),
    ("BANK_FEES",     "Bank Fees & Charges",               "Expense",  0, 0.0,  "excluded", 190),

    ("LOAN_REPAY",    "Loan Repayment (principal)",        "Excluded", 0, 0.0,  "excluded", 800),
    ("DRAWINGS",      "Drawings / Personal / Private",     "Excluded", 0, 0.0,  "excluded", 900),
    ("UNCATEGORIZED", "Uncategorized",                     "Excluded", 0, 0.0,  "excluded", 999),
]

# Codes that existed in the earlier, more granular version of this file.
# Mapped here so consolidate_to_broad_categories() can fold any data already
# sitting under an old code into its nearest broad replacement, rather than
# orphaning it. Format: {old_code: new_code}
RETIRED_CODE_MERGE_MAP = {
    "EXPORT_SALES":   "OTHER_INCOME",     # GST-free either way, folds into Other Income
    "INTEREST_INC":   "OTHER_INCOME",
    "TELECOM":        "OFFICE",
    "SUBSCRIPTIONS":  "OFFICE",
    "UTILITIES":      "OFFICE",
    "MEALS":          "FOOD",
    "CAPITAL_ASSET":  "OFFICE",           # significant asset purchases: recommend manual review per-transaction
}


def seed_categories():
    """Seeds the full DEFAULT_CATEGORIES set on a brand-new (empty) database."""
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if existing:
        sync_new_categories()
        consolidate_to_broad_categories()
        return
    conn.executemany(
        """INSERT INTO categories (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
           VALUES (?,?,?,?,?,?,?)""",
        DEFAULT_CATEGORIES,
    )
    conn.commit()


def consolidate_to_broad_categories():
    """
    One-time-per-database migration that folds any RETIRED_CODE_MERGE_MAP
    categories (from the earlier, more granular Category Master) into their
    broad replacement, then deactivates the retired ones.

    Safe to call on every startup -- idempotent. Existing transactions
    already pointing at a retired category_id are re-pointed to the
    replacement category_id FIRST, so nothing gets orphaned or silently
    loses its categorization; only then is the old category deactivated
    (is_active=0, not deleted, so the audit trail / history stays intact).
    """
    conn = get_db()
    code_to_id = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM categories").fetchall()}

    for old_code, new_code in RETIRED_CODE_MERGE_MAP.items():
        old_id = code_to_id.get(old_code)
        new_id = code_to_id.get(new_code)
        if old_id is None or new_id is None or old_id == new_id:
            continue

        moved = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE category_id = ?", (old_id,)
        ).fetchone()["c"]
        if moved:
            conn.execute(
                "UPDATE transactions SET category_id = ? WHERE category_id = ?",
                (new_id, old_id),
            )
            print(f"[category_master] Moved {moved} transaction(s) from "
                  f"retired category '{old_code}' to '{new_code}'.")

        conn.execute("UPDATE categories SET is_active = 0 WHERE id = ?", (old_id,))

    conn.commit()


def sync_new_categories():
    """
    Idempotent: inserts any DEFAULT_CATEGORIES rows whose `code` doesn't
    already exist in the DB, without touching existing rows. Safe to call
    on every startup -- this is what lets you add a new category to
    DEFAULT_CATEGORIES above and have it appear in an already-running
    deployment without a manual migration.
    """
    conn = get_db()
    existing_codes = {r["code"] for r in conn.execute("SELECT code FROM categories").fetchall()}
    missing = [row for row in DEFAULT_CATEGORIES if row[0] not in existing_codes]
    if missing:
        conn.executemany(
            """INSERT INTO categories (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
               VALUES (?,?,?,?,?,?,?)""",
            missing,
        )
        conn.commit()
        print(f"[category_master] Added {len(missing)} new category(ies): "
              f"{[m[1] for m in missing]}")


def list_categories(active_only: bool = True):
    conn = get_db()
    q = "SELECT * FROM categories"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY sort_order"
    return [dict(r) for r in conn.execute(q).fetchall()]


def get_category(category_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    return dict(row) if row else None


def create_category(code, name, pnl_group, gst_applicable, gst_rate=0.10, bas_label="G11"):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO categories (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
           VALUES (?,?,?,?,?,?, 500)""",
        (code, name, pnl_group, int(gst_applicable), gst_rate, bas_label),
    )
    conn.commit()
    return cur.lastrowid


def update_category(category_id: int, **fields):
    if not fields:
        return
    conn = get_db()
    allowed = {"name", "pnl_group", "gst_applicable", "gst_rate", "bas_label", "is_active", "sort_order"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(category_id)
    conn.execute(f"UPDATE categories SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
