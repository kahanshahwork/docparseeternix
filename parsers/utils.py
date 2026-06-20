"""
parsers/utils.py
Shared helpers: row grouping, amount parsing, date formatting.
No bank-specific logic here.
"""

import re
from datetime import datetime


# ── Row assembly ──────────────────────────────────────────────────────────────

def group_rows(words: list[dict], y_tol: float = 3.5) -> list[list[dict]]:
    """Group pdfplumber word dicts into visual rows sorted by y then x."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"] / y_tol), w["x0"]))
    rows, cur, prev_top = [], [words[0]], words[0]["top"]
    for w in words[1:]:
        if abs(w["top"] - prev_top) <= y_tol:
            cur.append(w)
        else:
            rows.append(sorted(cur, key=lambda x: x["x0"]))
            cur = [w]
            prev_top = w["top"]
    rows.append(sorted(cur, key=lambda x: x["x0"]))
    return rows


def row_top(row: list[dict]) -> float:
    return row[0]["top"] if row else 0.0


def row_text(row: list[dict]) -> str:
    return " ".join(w["text"] for w in row)


def words_in_band(row: list[dict], x_min: float, x_max: float) -> list[str]:
    """Return text tokens whose x0 falls in [x_min, x_max)."""
    return [w["text"] for w in row if x_min <= w["x0"] < x_max]


def words_right_edge_in_band(row: list[dict], x_min: float, x_max: float) -> list[str]:
    """Return text tokens whose x1 (right edge) falls in (x_min, x_max]."""
    return [w["text"] for w in row if x_min < w["x1"] <= x_max]


# ── Amount parsing ────────────────────────────────────────────────────────────

_AMT_CLEAN = re.compile(r"[\$,\s]")
_AMT_NUM   = re.compile(r"^[+\-−]?[\d]+(?:\.\d{2})?$")


def parse_amount(s: str) -> float | None:
    """Parse '$1,234.56', '+$1,234', '−$1,234.56', '-1234.56' → float | None."""
    s = s.strip()
    # Normalise unicode minus
    s = s.replace("−", "-").replace("–", "-")
    negative = s.startswith("-")
    # Strip currency signs and commas
    s2 = _AMT_CLEAN.sub("", s).lstrip("-").lstrip("+")
    try:
        val = float(s2)
        return -val if negative else val
    except ValueError:
        return None


def parse_balance(s: str) -> float | None:
    """
    Handles formats: '$1,234.56CR', '$1,234.56', '1,234.56', '197.06-' (trailing dash = negative).
    """
    s = s.strip()
    overdrawn = s.endswith("-")
    s = s.rstrip("-")
    cr = bool(re.search(r"CR$", s, re.I))
    s = re.sub(r"[Cc][Rr]$", "", s).strip()
    s = _AMT_CLEAN.sub("", s)
    try:
        val = float(s)
        if overdrawn:
            return -val
        return val  # CR = positive balance (normal)
    except ValueError:
        return None


def is_number_token(s: str) -> bool:
    s2 = _AMT_CLEAN.sub("", s.strip()).lstrip("-").lstrip("+")
    try:
        float(s2)
        return True
    except ValueError:
        return False


# ── Date parsing ─────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_MON_ABBR = {v: k.capitalize() for k, v in _MONTH_MAP.items() if len(k) == 3}


def month_num(mon: str) -> int | None:
    return _MONTH_MAP.get(mon.lower().rstrip("."), None)


def make_date(day: int | str, mon: int | str, year: int) -> str:
    """Return DD-Mon-YYYY string, or raw fallback."""
    try:
        if isinstance(mon, str):
            mon = month_num(mon) or int(mon)
        return datetime(year, int(mon), int(day)).strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return f"{day}-{mon}-{year}"


def detect_year_from_text(text: str, fallback: int = 2025) -> int:
    """Extract the first 4-digit year from free text."""
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else fallback


def year_rollover(new_month: int, prev_month: int, year: int) -> int:
    """If month decreased going forward (e.g. Dec→Jan), increment year."""
    if prev_month and new_month < prev_month and prev_month >= 11:
        return year + 1
    return year


# ── Sign detection ────────────────────────────────────────────────────────────

def sign_from_balance_delta(new_balance: float, prev_balance: float | None,
                             raw_amount: float) -> float:
    """
    Ground truth: if we know prev balance, compute delta.
    If delta matches raw amount (within 2 cents), use delta for sign.
    Otherwise fall back to raw_amount.
    """
    if prev_balance is None:
        return raw_amount
    delta = round(new_balance - prev_balance, 2)
    if abs(abs(delta) - abs(raw_amount)) <= 0.02:
        return delta
    return raw_amount


# ── Result builder ────────────────────────────────────────────────────────────

def build_result(transactions: list[dict], ambiguous: list[dict], meta: dict) -> dict:
    return {
        "transactions": transactions,
        "ambiguous":    ambiguous,
        "meta":         meta,
    }


def make_txn(txn_id: str, date: str, description: str, amount: float,
             balance: float | None, page: int, row_top: float,
             confidence: float = 1.0) -> dict:
    return {
        "transaction_id": txn_id,
        "date":           date,
        "description":    description.strip(),
        "amount":         round(amount, 2),
        "balance":        round(balance, 2) if balance is not None else None,
        "source_page":    page,
        "row_top":        row_top,
        "confidence":     round(confidence, 3),
    }
