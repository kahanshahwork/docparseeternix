"""
core/vendor_memory.py — Description normalization, transaction grouping (item 7),
and the vendor-memory lookup table (Part D, path 1: deterministic suggestion engine).

Self-contained: takes/returns plain dicts. Swappable later for an
embedding-based matcher (Part D, path 2) without touching callers — the
function signatures (normalize, group_transactions, suggest_category,
remember) are the contract other modules rely on.
"""

import re
from core.db import get_db

_NOISE_PATTERNS = [
    r"\b\d{2}/\d{2}/\d{2,4}\b",      # dates
    r"\b\d{4,}\b",                    # long reference/account numbers
    r"\bxx+\d*\b",                    # masked card numbers
    r"\breceipt\s*#?\d*\b",
    r"\bref\s*#?\w*\b",
    r"[^a-z\s]",                       # punctuation/digits left over
]

# ── Semantic buckets (item: "group uber eats + other food delivery together") ──
# Ordered list of (bucket_label, [keywords]). First match wins. Edit/extend freely —
# nothing else in the app needs to change when you add a keyword or bucket here.
SEMANTIC_BUCKETS = [
    ("Food Delivery",      ["uber eats", "ubereats", "menulog", "doordash", "deliveroo", "hungry panda"]),
    ("Ride Share / Taxi",  ["uber trip", "uber *", "uber technologies", "ola ride", "didi", "taxi"]),
    ("Bank Transfers",     ["npp payment", "osko payment", "payid", "transfer to", "transfer from", "internal transfer"]),
    ("Interest",           ["interest charged", "interest payment", "interest credit", "interest paid"]),
    ("Subscriptions",      ["netflix", "spotify", "stan ", "disney+", "amazon prime", "adobe", "microsoft 365",
                             "dropbox", "google storage", "youtube premium", "apple.com/bill"]),
    ("Merchant Settlement",["merchant settlement"]),
    ("BPAY",                ["bpay debit", "bpay payment", "bpay "]),
    ("Direct Debit",        ["direct debit"]),
    ("Salary / Payroll",    ["salary", "payroll", "wages"]),
    ("Bank Fees",           ["account fee", "monthly fee", "service fee", "merchant fee", "account keeping"]),
]


def normalize_description(desc: str) -> str:
    """Strip dates, reference numbers, punctuation -> stable grouping key."""
    s = (desc or "").lower()
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split(" ")
    return " ".join(tokens[:4])


def semantic_bucket(description: str) -> str | None:
    """Returns a human-readable bucket label like 'Food Delivery' if the description
    matches a known keyword set, else None (caller falls back to description grouping)."""
    s = (description or "").lower()
    for label, keywords in SEMANTIC_BUCKETS:
        if any(kw in s for kw in keywords):
            return label
    return None


def group_transactions(transactions: list[dict]) -> dict[str, list[dict]]:
    """Two-pass grouping:
       1) Try to place each transaction into a semantic bucket (Food Delivery, Transfers, ...)
       2) Anything left over groups by normalized description (old behavior) as a fallback.
    This is what lets 'Uber Eats' and 'Menulog' land in the same group even though
    their raw description text is completely different."""
    groups: dict[str, list[dict]] = {}
    for t in transactions:
        desc = t.get("description", "")
        bucket = semantic_bucket(desc)
        if bucket:
            key = bucket
        else:
            key = normalize_description(desc) or "(uncategorized text)"
        groups.setdefault(key, []).append(t)
    return groups


def suggest_category(client_id: int, description: str):
    """Look up vendor memory for an exact normalized-pattern match. Returns category_id or None."""
    key = normalize_description(description)
    if not key:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT category_id FROM vendor_memory WHERE client_id = ? AND pattern = ? ORDER BY hit_count DESC LIMIT 1",
        (client_id, key),
    ).fetchone()
    return row["category_id"] if row else None


def remember(client_id: int, description: str, category_id: int):
    """Called whenever a user confirms a category for a transaction — reinforces the mapping."""
    key = normalize_description(description)
    if not key:
        return
    conn = get_db()
    existing = conn.execute(
        "SELECT id, hit_count FROM vendor_memory WHERE client_id = ? AND pattern = ?",
        (client_id, key),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE vendor_memory SET category_id = ?, hit_count = hit_count + 1, updated_at = datetime('now') WHERE id = ?",
            (category_id, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO vendor_memory (client_id, pattern, category_id) VALUES (?,?,?)",
            (client_id, key, category_id),
        )
    conn.commit()
