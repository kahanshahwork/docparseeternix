"""
core/category_master.py — Single source of truth for the Category Master (Module 4).

Touch this file ONLY when you need to add/edit/retire a category or change
its GST treatment. Nothing else in the app hardcodes category logic — the
GST engine and P&L engine both read from the `categories` table that this
file seeds, so changes here propagate everywhere automatically.

Based on current ATO GST/BAS treatment (taxable @10%, GST-free, input-taxed,
and BAS-excluded are different buckets with different reporting effects).
This is a v1 starter set — refine once real client BAS workbooks are
available (see DEFAULT_CATEGORIES below, edit freely).
"""

from core.db import get_db

# (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
DEFAULT_CATEGORIES = [
    ("SALES",        "Sales / Trading Income",          "Income",   1, 0.10, "G1",       10),
    ("EXPORT_SALES",  "Export Sales (GST-free)",          "Income",   0, 0.0,  "G1-free",  20),
    ("INTEREST_INC",  "Interest Income",                  "Income",   0, 0.0,  "excluded", 30),
    ("OTHER_INCOME",  "Other Business Income",            "Income",   1, 0.10, "G1",       40),

    ("RENT",          "Rent / Lease Expense",             "Expense",  1, 0.10, "G11",      110),
    ("TELECOM",        "Telephone / Internet",             "Expense",  1, 0.10, "G11",      120),
    ("TRAVEL",         "Travel & Vehicle",                  "Expense",  1, 0.10, "G11",      130),
    ("OFFICE",         "Office Supplies",                   "Expense",  1, 0.10, "G11",      140),
    ("PROFESSIONAL",   "Professional / Contractor Fees",    "Expense",  1, 0.10, "G11",      150),
    ("SALARY",         "Salary & Wages",                    "Expense",  0, 0.0,  "excluded", 160),
    ("BANK_FEES",      "Bank Fees & Charges",               "Expense",  0, 0.0,  "excluded", 170),
    ("INSURANCE",      "Insurance",                          "Expense",  1, 0.10, "G11",      180),
    ("CAPITAL_ASSET",  "Capital Asset Purchase",             "Expense",  1, 0.10, "G10",      190),
    ("LOAN_REPAY",     "Loan Repayment (principal)",         "Expense",  0, 0.0,  "excluded", 200),
    ("UTILITIES",      "Utilities",                          "Expense",  1, 0.10, "G11",      210),
    ("MARKETING",      "Marketing & Advertising",            "Expense",  1, 0.10, "G11",      220),

    ("DRAWINGS",       "Drawings / Personal / Private",      "Excluded", 0, 0.0,  "excluded", 900),
    ("UNCATEGORIZED",  "Uncategorized",                      "Excluded", 0, 0.0,  "excluded", 999),
]


def seed_categories():
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if existing:
        return
    conn.executemany(
        """INSERT INTO categories (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
           VALUES (?,?,?,?,?,?,?)""",
        DEFAULT_CATEGORIES,
    )
    conn.commit()


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
