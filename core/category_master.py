"""
core/category_master.py — Single source of truth for the Category Master.

52 nature-wise heads sourced from Australian_Charts_of_Account.csv.
GST treatment per ATO rules:
  GST on Income    → gst_applicable=1, bas_label=G1,  pnl_group=Income / Direct Cost
  GST on Expenses  → gst_applicable=1, bas_label=G11, pnl_group=Expense / Direct Cost
  GST Free Income  → gst_applicable=0, bas_label=G1,  pnl_group=Income
  GST Free Expenses→ gst_applicable=0, bas_label=G11, pnl_group=Expense
  BAS Excluded     → gst_applicable=0, bas_label=excluded, pnl_group=Excluded
"""

from core.db import get_db

# (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
# pnl_group values: "Income" | "Direct Cost" | "Expense" | "Excluded"
DEFAULT_CATEGORIES = [
    # ── Revenue ──────────────────────────────────────────────────────────
    ("SALES",               "Sales",                        "Income",      1, 0.10, "G1",       10),
    ("INCOME",              "Income",                       "Income",      1, 0.10, "G1",       20),
    ("OTHER_REVENUE",       "Other Revenue",                "Income",      1, 0.10, "G1",       30),
    ("INTEREST_INCOME",     "Interest Income",              "Income",      0, 0.0,  "G1",       40),

    # ── Other Income ─────────────────────────────────────────────────────
    ("GOVT_GRANT",          "Government Grant",             "Excluded",    0, 0.0,  "excluded", 50),

    # ── Direct Costs ─────────────────────────────────────────────────────
    ("COGS",                "Cost of Goods Sold",           "Direct Cost", 1, 0.10, "G11",      60),

    # ── Expenses — GST on Expenses ───────────────────────────────────────
    ("IT_DEV",              "IT Development Expense",       "Expense",     1, 0.10, "G11",      110),
    ("SUPPLIES",            "Supplies Expense",             "Expense",     1, 0.10, "G11",      120),
    ("FRANCHISE",           "Franchise Expense",            "Expense",     1, 0.10, "G11",      130),
    ("ADVERTISING",         "Advertising",                  "Expense",     1, 0.10, "G11",      140),
    ("MEMBERSHIP_FEES",     "Membership Fees",              "Expense",     1, 0.10, "G11",      150),
    ("STAFF_AMENITIES",     "Staff Amenities",              "Expense",     1, 0.10, "G11",      160),
    ("CLIENT_GIFTS",        "Client Gifts",                 "Expense",     1, 0.10, "G11",      170),
    ("CLEANING",            "Cleaning",                     "Expense",     1, 0.10, "G11",      180),
    ("MEETING_EXP",         "Meeting Expenses",             "Expense",     1, 0.10, "G11",      190),
    ("SUBCONTRACTORS",      "Subcontractors",               "Expense",     1, 0.10, "G11",      200),
    ("CONSULTING",          "Consulting & Accounting",      "Expense",     1, 0.10, "G11",      210),
    ("EQUIP_RENTAL",        "Equipment Rental",             "Expense",     1, 0.10, "G11",      220),
    ("FREIGHT",             "Freight & Courier",            "Expense",     1, 0.10, "G11",      230),
    ("GENERAL_EXP",         "General Expenses",             "Expense",     1, 0.10, "G11",      240),
    ("INSURANCE",           "Insurance",                    "Expense",     1, 0.10, "G11",      250),
    ("ASSETS_U30K",         "Assets less than 30K",         "Expense",     1, 0.10, "G11",      260),
    ("LEGAL",               "Legal expenses",               "Expense",     1, 0.10, "G11",      270),
    ("LIGHT_POWER",         "Light, Power, Heating",        "Expense",     1, 0.10, "G11",      280),
    ("WEBSITE",             "Website Expenses",             "Expense",     1, 0.10, "G11",      290),
    ("MOTOR_VEHICLE",       "Motor Vehicle Expenses",       "Expense",     1, 0.10, "G11",      300),
    ("OFFICE_EXP",          "Office Expenses",              "Expense",     1, 0.10, "G11",      310),
    ("PRINTING",            "Printing & Stationery",        "Expense",     1, 0.10, "G11",      320),
    ("RENT",                "Rent",                         "Expense",     1, 0.10, "G11",      330),
    ("BAD_DEBTS",           "Bad Debts written off",        "Expense",     1, 0.10, "G11",      340),
    ("REPAIRS",             "Repairs and Maintenance",      "Expense",     1, 0.10, "G11",      350),
    ("SUBSCRIPTIONS",       "Subscriptions",                "Expense",     1, 0.10, "G11",      360),
    ("TELEPHONE",           "Telephone & Internet",         "Expense",     1, 0.10, "G11",      370),
    ("TRAVEL_NATIONAL",     "Travel - National",            "Expense",     1, 0.10, "G11",      380),

    # ── Expenses — GST Free ──────────────────────────────────────────────
    ("DONATION",            "Donation",                     "Expense",     0, 0.0,  "G11",      390),
    ("FORMATION",           "Formation Expense",            "Expense",     0, 0.0,  "G11",      400),
    ("COUNCIL_RATES",       "Council Rates",                "Expense",     0, 0.0,  "G11",      410),
    ("FILING_FEES",         "Filing Fees",                  "Expense",     0, 0.0,  "G11",      420),
    ("BANK_FEES",           "Bank Fees",                    "Expense",     0, 0.0,  "G11",      430),
    ("ENTERTAINMENT",       "Entertainment",                "Expense",     0, 0.0,  "G11",      440),
    ("INTEREST_EXP",        "Interest Expense",             "Expense",     0, 0.0,  "G11",      450),
    ("MV_REGO",             "MV Rego",                      "Expense",     0, 0.0,  "G11",      460),
    ("TRAVEL_INTL",         "Travel - International",       "Expense",     0, 0.0,  "G11",      470),

    # ── BAS Excluded ─────────────────────────────────────────────────────
    ("DEPRECIATION",        "Depreciation",                 "Excluded",    0, 0.0,  "excluded", 500),
    ("WAGES",               "Wages and Salaries",           "Excluded",    0, 0.0,  "excluded", 510),
    ("SUPER",               "Superannuation",               "Excluded",    0, 0.0,  "excluded", 520),
    ("BANK_REVALUATIONS",   "Bank Revaluations",            "Excluded",    0, 0.0,  "excluded", 530),
    ("UNREALISED_FX",       "Unrealised Currency Gains",    "Excluded",    0, 0.0,  "excluded", 540),
    ("REALISED_FX",         "Realised Currency Gains",      "Excluded",    0, 0.0,  "excluded", 550),
    ("INCOME_TAX",          "Income Tax Expense",           "Excluded",    0, 0.0,  "excluded", 560),
    ("STRIPE_FEES",         "Stripe Fees",                  "Excluded",    0, 0.0,  "excluded", 570),

    # ── System ───────────────────────────────────────────────────────────
    ("UNCATEGORIZED",       "Uncategorized",                "Excluded",    0, 0.0,  "excluded", 999),
]


def seed_categories():
    """
    Wipes all existing categories and re-seeds from DEFAULT_CATEGORIES.
    Transactions pointing at old category IDs will have category_id set to NULL
    (uncategorized) — user re-categorizes from the new list.
    Called once on app start.
    """
    conn = get_db()
    # Null out any transaction references so FK constraints don't block deletion
    conn.execute("UPDATE transactions SET category_id = NULL, gst_amount = 0, net_amount = amount WHERE category_id IS NOT NULL")
    conn.execute("DELETE FROM categories")
    conn.executemany(
        """INSERT INTO categories (code, name, pnl_group, gst_applicable, gst_rate, bas_label, sort_order)
           VALUES (?,?,?,?,?,?,?)""",
        DEFAULT_CATEGORIES,
    )
    conn.commit()
    print(f"[category_master] Seeded {len(DEFAULT_CATEGORIES)} categories.")


def sync_new_categories():
    """Inserts any DEFAULT_CATEGORIES rows missing from DB. Safe to call on startup."""
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
        print(f"[category_master] Added {len(missing)} new category(ies).")


def list_categories(active_only: bool = True):
    conn = get_db()
    q = "SELECT * FROM categories"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY sort_order"
    return [dict(r) for r in conn.execute(q).fetchall()]


def get_category(category_id: int):
    if category_id is None:
        return None
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
