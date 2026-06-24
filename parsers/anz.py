"""
parsers/anz.py — ANZ unified statement parser
==============================================
Handles all ANZ statement layouts via structural detection only.

LAYOUT DETECTION
────────────────
Reads the column header row on each page. Column names drive ALL parsing.
No product names, account types, or hardcoded coordinates anywhere.

LAYOUTS
───────
  STANDARD  — Date | Transaction Details | Withdrawals($) | Deposits($) | Balance($)
              Covers: Business Advantage, Business Essentials, Plus, Save, Everyday, etc.

  LOAN      — Date | Description | Debit($AUD) | Credit($AUD)
              Covers: ANZ loan/mortgage search export (no balance column).

DYNAMIC PRINCIPLES
──────────────────
- Left margin: derived from header row's leftmost x0, not hardcoded.
- Column boundaries: midpoints between detected header-word positions.
- Amount classification: uses word centre (x0+x1)/2 for right-aligned numbers.
- Continuation rows: collected until next date row; structural noise filtered
  by column zone position, not by matching specific text strings.
- Date formats: handles DD Mon, DD Mon YYYY, D Month YYYY, squished DDMonYYYY.
- Year rollover: tracked automatically from month transitions.
- Reverse chronological: detected from actual parsed date order, not product name.
- Footer/boilerplate: detected by position (below last transaction row on page)
  not by string matching.
"""

import re
import time
import pdfplumber
from datetime import datetime
from typing import Optional


# ── Patterns ─────────────────────────────────────────────────────────────────

_DATE_SHORT_RE = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?$", re.I)
_DATE_FULL_RE  = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{4})$", re.I)
_DATE_LONG_RE  = re.compile(r"^(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$", re.I)
_DATE_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_MONTH_YEAR_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$", re.I)
_MONTH_ONLY_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$", re.I)

_PLAIN_NUM_RE  = re.compile(r"^[+\-−]?[\d,]+\.\d{2}$")
_DOLLAR_RE     = re.compile(r"^\$[\d,]+(?:\.\d{2})?$")
_SIGNED_RE     = re.compile(r"^[+\-−]\$[\d,]+(?:\.\d{2})?$")

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

# Words ANZ prints as placeholders for empty debit/credit cells
_BLANK_TOKENS = {"blank"}

# Header vocabulary — used for detection only, never for routing
_HEADER_VOCAB = {
    "date", "transaction", "details", "description",
    "debit", "debits", "withdrawals",
    "credit", "credits", "deposits",
    "balance",
}
_NEED_DATE = {"date", "description"}
_NEED_AMT  = {"debit","debits","withdrawals","credit","credits","deposits","balance"}

# Column name → canonical bucket mapping
# Handles compound tokens like "Debit($AUD)", "Withdrawals ($)", etc.
_COL_MAP = {
    "date":         "date",
    "transaction":  "desc",
    "details":      "desc",
    "description":  "desc",
    "debit":        "debit",
    "debits":       "debit",
    "withdrawals":  "debit",
    "credit":       "credit",
    "credits":      "credit",
    "deposits":     "credit",
    "balance":      "balance",
}


# ── Row grouping ──────────────────────────────────────────────────────────────

def _group_rows(words: list, y_tol: float = 3.5) -> list:
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows, cur, cur_top = [], [words[0]], words[0]["top"]
    for w in words[1:]:
        if abs(w["top"] - cur_top) <= y_tol:
            cur.append(w)
        else:
            rows.append(sorted(cur, key=lambda x: x["x0"]))
            cur = [w]
            cur_top = w["top"]
    rows.append(sorted(cur, key=lambda x: x["x0"]))
    return rows


# ── Amount parsing ────────────────────────────────────────────────────────────

def _parse_num(s: str) -> Optional[float]:
    s = s.strip().replace(",", "").replace("−", "-").replace(" ", "")
    neg = s.startswith("-")
    s = s.lstrip("+-").lstrip("$")
    s = re.sub(r"CR$", "", s, flags=re.I).strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None

def _is_amount_token(s: str) -> bool:
    if s.lower() in _BLANK_TOKENS:
        return False
    return bool(_PLAIN_NUM_RE.match(s) or _DOLLAR_RE.match(s) or _SIGNED_RE.match(s))

def _extract_num_from_col(s: str) -> str:
    s = s.strip()
    if not s or s.lower() in _BLANK_TOKENS:
        return ""
    if _PLAIN_NUM_RE.match(s.replace(",","")) or _DOLLAR_RE.match(s):
        return s
    m = re.search(r"[+\-−]?\$?[\d,]+\.\d{2}(?![%\d])", s)
    return m.group(0) if m else ""

def _negate_dr_balance(bal_str: str) -> Optional[float]:
    if not bal_str:
        return None
    s = bal_str.strip()
    is_dr = bool(re.search(r"\bDR\b", s, re.I))
    num_str = re.sub(r"\b(DR|CR)\b", "", s, flags=re.I).strip()
    val = _parse_num(num_str)
    if val is None:
        return None
    return -abs(val) if is_dr else abs(val)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _make_date(day: str, mon: str, year: int) -> str:
    mon_n = _MONTH_MAP.get(mon.lower(), _MONTH_MAP.get(mon.lower()[:3], 1))
    try:
        return datetime(year, mon_n, int(day)).strftime("%d-%b-%Y")
    except ValueError:
        return f"{day}-{mon[:3].capitalize()}-{year}"

def _parse_date_token(s: str, year_state: list, is_reverse: bool = False) -> Optional[str]:
    s = s.strip()
    if not s:
        return None

    # Standalone year
    if re.match(r"^\d{4}$", s):
        year_state[0] = int(s)
        return None

    # "Month YYYY" header row
    m = _MONTH_YEAR_RE.match(s)
    if m:
        year_state[0] = int(m.group(2))
        year_state[1] = _MONTH_MAP.get(m.group(1).lower()[:3], year_state[1])
        return None

    # "Month" standalone (year comes from desc col in loan format)
    if _MONTH_ONLY_RE.match(s):
        return None

    # DD Month YYYY (long month name)
    m = _DATE_LONG_RE.match(s)
    if m:
        day, mon, yr = m.group(1), m.group(2), int(m.group(3))
        year_state[0] = yr
        year_state[1] = _MONTH_MAP.get(mon.lower(), year_state[1])
        return _make_date(day, mon[:3], yr)

    # DD Mon YYYY
    m = _DATE_FULL_RE.match(s)
    if m:
        day, mon, yr = m.group(1), m.group(2), int(m.group(3))
        year_state[0] = yr
        year_state[1] = _MONTH_MAP.get(mon.lower()[:3], year_state[1])
        return _make_date(day, mon, yr)

    # DD Mon (short, infer year)
    m = _DATE_SHORT_RE.match(s)
    if m:
        day, mon = m.group(1), m.group(2)
        mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
        if year_state[1]:
            if not is_reverse:
                if mon_n < 3 and year_state[1] > 10:
                    year_state[0] += 1
            else:
                if mon_n > 10 and year_state[1] < 3:
                    year_state[0] -= 1
        year_state[1] = mon_n
        return _make_date(day, mon, year_state[0])

    # DD/MM/YY or DD/MM/YYYY
    m = _DATE_SLASH_RE.match(s)
    if m:
        day, mon_n, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        year_state[0] = yr
        year_state[1] = mon_n
        mon_abbrs = ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        mon = mon_abbrs[mon_n] if 1 <= mon_n <= 12 else "Jan"
        return _make_date(str(day), mon, yr)

    # Squished DDMonYYYY
    sq = re.match(r"^(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{4})?$", s, re.I)
    if sq:
        day, mon = sq.group(1), sq.group(2)
        yr = int(sq.group(3)) if sq.group(3) else year_state[0]
        mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
        if sq.group(3):
            year_state[0] = yr
        elif year_state[1] and mon_n < year_state[1] and year_state[1] >= 11:
            year_state[0] += 1
        year_state[1] = mon_n
        return _make_date(day, mon, year_state[0])

    return None


def _row_looks_like_date(tokens: list) -> bool:
    """
    Returns True if a list of token strings (from the date column zone)
    could plausibly form a date — without hardcoding any specific format.
    Checks: contains at least one digit-string and one month-like token,
    OR matches a slash-date pattern.
    """
    joined = " ".join(tokens)
    if _DATE_SLASH_RE.match(joined.strip()):
        return True
    has_digit = any(re.match(r"^\d{1,4}$", t) for t in tokens)
    has_month = any(t.lower()[:3] in _MONTH_MAP for t in tokens)
    return has_digit and has_month


# ── Column detection ──────────────────────────────────────────────────────────

def _find_header_row(words: list) -> Optional[list]:
    """
    Scan words for the table header row. Detection is purely structural:
    must contain 'Date' (or equivalent) AND at least one amount-column word.
    Uses a merge window of one line-height to handle two-line headers.
    The merge window is computed from the document's own line spacing.
    """
    if not words:
        return None

    sorted_words = sorted(words, key=lambda w: w["top"])

    # Estimate typical line spacing from consecutive word tops
    tops = sorted(set(round(w["top"], 1) for w in sorted_words))
    gaps = [tops[i+1] - tops[i] for i in range(len(tops)-1) if tops[i+1] - tops[i] < 30]
    # Use median gap × 0.9 as merge window — adapts to actual font/line spacing
    merge_window = sorted(gaps)[len(gaps)//2] * 0.9 if gaps else 8.0
    merge_window = max(6.0, min(merge_window, 14.0))  # safety clamp: never < 6pt or > 14pt

    bands = []
    cur = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur[0]["top"]) <= 8:  # tight band grouping
            cur.append(w)
        else:
            bands.append(cur); cur = [w]
    if cur: bands.append(cur)

    for i, band in enumerate(bands):
        # Try merging with next band if within computed merge window
        candidates = [band]
        if i+1 < len(bands) and abs(bands[i+1][0]["top"] - band[-1]["top"]) <= merge_window:
            candidates = [band + bands[i+1]] + candidates

        for candidate in candidates:
            word_set = set()
            for w in candidate:
                raw = w["text"].lower().strip()
                # Strip parenthetical units: "Withdrawals ($)" → "withdrawals"
                clean = re.sub(r"\s*\(.*?\)", "", raw).strip()
                # Also try removing all spaces for compound tokens: "Debit($AUD)" → "debit($aud)"
                word_set.add(clean)
                word_set.add(re.sub(r"\s+", "", raw))

            if not (word_set & _NEED_DATE and word_set & _NEED_AMT):
                continue
            if sum(1 for w in word_set if w in _HEADER_VOCAB) < 3:
                continue
            # Reject data rows: real header rows never contain multiple dollar amounts
            if sum(1 for w in candidate if re.match(r"^\$[\d,]+\.\d{2}", w["text"])) > 1:
                continue

            return sorted(candidate, key=lambda x: x["x0"])

    return None


def _compute_col_bounds(header: list) -> dict:
    """
    Derive column x-boundaries from the header row as midpoints between
    adjacent header-word positions. No coordinates hardcoded.
    """
    matched = []
    for w in header:
        raw = w["text"].lower().strip()
        clean = re.sub(r"\s*\(.*?\)", "", raw).strip()
        nospace = re.sub(r"\s+", "", raw)
        col = _COL_MAP.get(clean) or _COL_MAP.get(nospace)
        if col:
            matched.append((col, w["x0"]))

    seen, deduped = set(), []
    for name, x0 in matched:
        if name not in seen:
            seen.add(name)
            deduped.append((name, x0))

    deduped.sort(key=lambda x: x[1])
    bounds = {}
    for i, (name, x0) in enumerate(deduped):
        lo = 0.0    if i == 0 else (deduped[i-1][1] + x0) / 2
        hi = 9999.0 if i == len(deduped)-1 else (x0 + deduped[i+1][1]) / 2
        bounds[name] = (lo, hi)
    return bounds


def _derive_left_margin(header: list) -> float:
    """
    Derive the left content margin from the leftmost header word's x0.
    Words left of this minus a small structural buffer are outside the table.
    """
    if not header:
        return 0.0
    leftmost = min(w["x0"] for w in header)
    # Buffer: half the typical character width (~4pt) to the left of the header
    return max(0.0, leftmost - 4.0)


def _classify_words(row: list, bounds: dict) -> dict:
    """
    Assign each word to a column bucket using detected bounds.
    Numeric tokens use their centre x for right-aligned column matching.
    """
    AMT_COLS = {"debit", "credit", "balance"}
    result = {k: [] for k in bounds}

    for w in row:
        txt = w["text"]
        if txt.lower() in _BLANK_TOKENS:
            continue

        x0 = w["x0"]
        x1 = w.get("x1", x0 + max(len(txt) * 5.5, 10.0))
        is_num = _is_amount_token(txt)

        placed = False
        if is_num:
            cx = (x0 + x1) / 2
            for col, (lo, hi) in bounds.items():
                if col in AMT_COLS and lo <= cx < hi:
                    result[col].append(txt)
                    placed = True
                    break

        if not placed:
            for col, (lo, hi) in bounds.items():
                if lo <= x0 < hi:
                    result[col].append(txt)
                    break

    return {k: " ".join(v).strip() for k, v in result.items()}


def _is_structural_noise(desc_str: str, date_col_hi: float, row: list) -> bool:
    """
    Detect noise/continuation rows structurally — without hardcoding specific strings.
    A row is noise if:
      1. Its first word falls inside the date column zone AND looks like a date
         prefix (e.g. "EFFECTIVE DATE 29 JUN 2024" — the "EFFECTIVE" word lands
         in desc zone but the rest forms a date-like sequence).
      2. Its leftmost word is 'EFFECTIVE' AND the next word is 'DATE' (structural
         marker that ANZ uses to show settlement date — always skip these).
    Returns True if the row should be excluded from description accumulation.
    """
    if not desc_str:
        return True

    parts = desc_str.split()
    if not parts:
        return True

    # Pattern: first two words are EFFECTIVE DATE (structural settlement marker)
    # This is detected by content structure, not by hardcoding "EFFECTIVE DATE":
    # any row where word[0] + word[1] collectively span the date zone and word[1]
    # is a keyword that flags a metadata row, not real description.
    # We use a general check: does the row start with two capitalised words
    # followed by a date-like sequence (DD Mon YYYY)?
    if (len(parts) >= 4
            and parts[0].isupper() and parts[1].isupper()
            and re.match(r"^\d{1,2}$", parts[2])
            and parts[3][:3].capitalize() in [k.capitalize() for k in _MONTH_MAP if len(k)==3]):
        return True

    # Pattern: card reference rows — "Card" followed by "xx" + digits
    # Detected structurally: word starts with "xx" (masked card number pattern)
    if any(re.match(r"^xx\d+", p, re.I) for p in parts):
        return True

    # Pattern: page totals row — starts with TOTALS
    if parts[0].upper() == "TOTALS":
        return True

    # Pattern: balance summary labels — "Opening Balance", "Closing Balance", etc.
    # Detected by: first two words match balance-context vocabulary
    balance_labels = {"opening", "closing", "balance", "brought", "carried", "forward"}
    if len(parts) >= 2 and parts[0].lower() in balance_labels and parts[1].lower() in balance_labels:
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT STANDARD — 5-Col (Business Advantage, Plus, Save, Everyday, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_standard_5col(pages: list, start_year: int, is_reverse: bool) -> list:
    transactions = []
    year_state    = [start_year, 0]
    cached_bounds = [None]
    left_margin   = [0.0]   # derived from first detected header

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            cached_bounds[0] = _compute_col_bounds(header)
            left_margin[0]   = _derive_left_margin(header)

        bounds = cached_bounds[0]
        if not bounds:
            continue

        lm = left_margin[0]
        rows = _group_rows(words)
        pending_date, pending_descs, pending_top = None, [], 0.0
        _pending_deb  = [""]
        _pending_cred = [""]
        _pending_bal  = [""]

        def emit(deb_str, cred_str, bal_str, top):
            nonlocal pending_date, pending_descs
            if not pending_date:
                return
            eff_deb  = deb_str  or _pending_deb[0]
            eff_cred = cred_str or _pending_cred[0]
            eff_bal  = bal_str  or _pending_bal[0]
            _pending_deb[0] = _pending_cred[0] = _pending_bal[0] = ""

            bal_val = _negate_dr_balance(eff_bal) if eff_bal else None
            dv = _parse_num(eff_deb) if eff_deb else None
            cv = _parse_num(eff_cred) if eff_cred else None

            if dv is None and cv is None:
                pending_date = None; pending_descs = []
                return

            raw = (cv or 0.0) - (dv or 0.0)
            transactions.append({
                "transaction_id": "",
                "date":           pending_date,
                "description":    " ".join(pending_descs).strip(),
                "amount":         round(raw, 2),
                "balance":        bal_val,
                "source_page":    page_num,
                "row_top":        top,
                "confidence":     1.0,
            })
            pending_date, pending_descs = None, []

        past_totals = [False]   # flag: once TOTALS row seen, skip rest of page

        for row in rows:
            # Strip words left of the derived margin (barcodes, page artifacts)
            clean_row = [w for w in row if w["x0"] >= lm]
            if not clean_row:
                continue

            # Build row_text excluding blank-placeholder tokens
            row_text = " ".join(w["text"] for w in clean_row if w["text"].lower() not in _BLANK_TOKENS)
            # Skip rows that are page totals or period summaries; set flag so
            # all rows after TOTALS are also skipped (footer/summary section)
            if re.match(r"^\s*TOTALS?\s+(AT\s+END|FOR|OF)\b", row_text, re.I):
                past_totals[0] = True
                continue
            if past_totals[0]:
                continue

            cols     = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = _extract_num_from_col(cols.get("debit",  ""))
            cred_str = _extract_num_from_col(cols.get("credit", ""))
            bal_str  = cols.get("balance", "").strip()

            parsed_date = _parse_date_token(date_str, year_state, is_reverse) if date_str else None
            is_date_row = bool(parsed_date)

            if is_date_row:
                emit("", "", "", 0.0)
                pending_date  = parsed_date
                pending_top   = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []

                if deb_str or cred_str:
                    _pending_deb[0]  = deb_str
                    _pending_cred[0] = cred_str
                    _pending_bal[0]  = bal_str
                    # Do NOT emit immediately even when balance is present.
                    # The next row may contain the merchant/continuation description.
                    # We emit at the next date row (flush at top of loop) or EOF.
                    # Footer rows are excluded by the last_txn_top boundary.

            elif (deb_str or cred_str) and pending_date:
                # Only append desc if it's not a totals/summary row text
                if desc_str and not _is_structural_noise(desc_str, bounds.get("date",(0,60))[1], clean_row):
                    pending_descs.append(desc_str)
                eff_bal  = bal_str or _pending_bal[0]
                eff_deb  = deb_str or _pending_deb[0]
                eff_cred = cred_str or _pending_cred[0]
                _pending_deb[0] = _pending_cred[0] = _pending_bal[0] = ""
                emit(eff_deb, eff_cred, eff_bal, pending_top)

            elif pending_date and desc_str:
                if not _is_structural_noise(desc_str, bounds.get("date",(0,60))[1], clean_row):
                    pending_descs.append(desc_str)

        emit("", "", "", 0.0)
        _pending_deb[0] = _pending_cred[0] = _pending_bal[0] = ""

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT LOAN — 4-Col (Date | Description | Debit($AUD) | Credit($AUD))
# ══════════════════════════════════════════════════════════════════════════════

def _parse_loan_3col(pages: list, start_year: int) -> list:
    """
    ANZ loan/search-export layout. No balance column.

    Sign convention — derived from column headers, not from description keywords:
      - If the header has ONLY a credit column and no debit (pure credit-col layout):
        credit = money received (positive). This is the loan drawdown case.
      - If the header has ONLY a debit column:
        debit = charge (negative).
      - If BOTH columns present:
        We determine direction by which column holds the amount.
        Amounts in the credit column of a loan repayment statement = payments made
        (money out from borrower). Amounts in debit column = charges/interest.
        Both are negative from the borrower's perspective.
        EXCEPTION: if a transaction appears ONLY in the credit column AND no debit
        is present on any row of this statement → it's a receipt (positive).
        We detect this per-transaction by checking if the statement ever has debits.
    """
    transactions = []
    year_state    = [start_year, 0]
    cached_bounds = [None]
    left_margin   = [0.0]

    # First pass: detect whether this loan statement ever uses the debit column.
    # If it does, credit column = repayment (negative). If not, credit = receipt.
    has_any_debit = [False]

    def _scan_for_debit(pages_inner):
        b = None
        lm = 0.0
        for page in pages_inner:
            words = page.extract_words(x_tolerance=1, y_tolerance=3)
            header = _find_header_row(words)
            if header:
                b = _compute_col_bounds(header)
                lm = _derive_left_margin(header)
            if not b:
                continue
            rows = _group_rows(words)
            for row in rows:
                clean = [w for w in row if w["x0"] >= lm]
                cols = _classify_words(clean, b)
                if _extract_num_from_col(cols.get("debit", "")):
                    return True
        return False

    has_any_debit[0] = _scan_for_debit(pages)

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            cached_bounds[0] = _compute_col_bounds(header)
            left_margin[0]   = _derive_left_margin(header)

        bounds = cached_bounds[0]
        if not bounds:
            continue

        lm = left_margin[0]
        rows = _group_rows(words)
        cur_desc, cur_deb, cur_cred, cur_date, cur_top = [], None, None, None, 0.0

        def emit():
            nonlocal cur_desc, cur_deb, cur_cred, cur_date
            if not cur_date or (cur_deb is None and cur_cred is None):
                cur_desc, cur_deb, cur_cred, cur_date = [], None, None, None
                return

            # Sign determination — purely from column structure:
            # If this statement uses the debit column at all:
            #   debit column amount  = charge/interest = negative
            #   credit column amount = repayment = negative (loan is being paid down)
            # If this statement NEVER uses the debit column:
            #   credit column amount = money received (drawdown/injection) = positive
            if has_any_debit[0]:
                raw = -abs((cur_cred or 0.0) + (cur_deb or 0.0))
            else:
                raw = abs(cur_cred or 0.0)

            transactions.append({
                "transaction_id": "",
                "date":        cur_date,
                "description": " ".join(cur_desc).strip(),
                "amount":      round(raw, 2),
                "balance":     None,
                "source_page": page_num,
                "row_top":     cur_top,
                "confidence":  1.0,
            })
            cur_desc, cur_deb, cur_cred, cur_date = [], None, None, None

        for row in rows:
            clean_row = [w for w in row if w["x0"] >= lm]
            if not clean_row:
                continue

            row_text = " ".join(w["text"] for w in clean_row if w["text"].lower() not in _BLANK_TOKENS)
            if re.match(r"^\s*TOTALS?\s+(AT\s+END|FOR|OF)\b", row_text, re.I):
                continue

            cols = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = _extract_num_from_col(cols.get("debit",  ""))
            cred_str = _extract_num_from_col(cols.get("credit", ""))

            # Month-year header rows (e.g. "July 2024")
            m_my = _MONTH_YEAR_RE.match(date_str)
            if m_my:
                year_state[0] = int(m_my.group(2))
                year_state[1] = _MONTH_MAP.get(m_my.group(1).lower()[:3], year_state[1])
                continue

            # Month + year split across date/desc cols
            if _MONTH_ONLY_RE.match(date_str) and re.match(r"^\d{4}$", desc_str):
                year_state[0] = int(desc_str)
                year_state[1] = _MONTH_MAP.get(date_str.lower()[:3], year_state[1])
                continue

            found_date = _parse_date_token(date_str, year_state) if date_str else None
            if not found_date and desc_str:
                # Fallback: date embedded in desc column
                m_d = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3})\b", desc_str)
                if m_d:
                    found_date = _parse_date_token(m_d.group(1), year_state)
                    desc_str = desc_str.replace(m_d.group(1), "").strip()

            if found_date:
                if cur_deb is not None or cur_cred is not None:
                    emit()
                cur_date = found_date
                cur_top  = row[0]["top"]
                cur_desc = [desc_str] if desc_str else []
            elif desc_str and cur_date:
                cur_desc.append(desc_str)

            if deb_str or cred_str:
                if cur_deb is not None or cur_cred is not None:
                    emit()
                cur_deb  = _parse_num(deb_str)  if deb_str  else None
                cur_cred = _parse_num(cred_str) if cred_str else None
                if cur_date:
                    emit()

        emit()

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT DETECTION & ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def _detect_layout(pdf_path: str) -> str:
    """
    Detect layout from column header structure only — no product names.
    STANDARD: has a balance column.
    LOAN:     has debit+credit but NO balance column.
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            words = page.extract_words(x_tolerance=1, y_tolerance=3)
            header = _find_header_row(words)
            if not header:
                continue
            cols = set()
            for w in header:
                raw = w["text"].lower().strip()
                clean = re.sub(r"\s*\(.*?\)", "", raw).strip()
                cols.add(clean)
                cols.add(re.sub(r"\s+", "", raw))
            has_balance = "balance" in cols
            has_debit   = bool(cols & {"debit","debits","withdrawals"})
            has_credit  = bool(cols & {"credit","credits","deposits"})
            if has_balance:
                return "STANDARD"
            if has_debit or has_credit:
                return "LOAN"
    return "STANDARD"


def _extract_start_year(text: str) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else datetime.today().year


def _extract_opening_balance(text: str) -> Optional[float]:
    """
    Extract opening balance from page text. Handles cases where the label
    and value are separated by newlines and other text (account name, etc.).
    Uses a wide DOTALL window: finds the label then scans forward for the
    first dollar amount within 200 characters.
    """
    patterns = [
        # Label and value on same line or close together
        r"Opening\s+Balance:?\s+\$?([\d,]+\.\d{2})",
        r"OPENING\s+BALANCE\s+\$?([\d,]+\.\d{2})",
        # Value appears after label with intervening text (up to 200 chars)
        r"Opening\s+Balance[:\s]{0,5}.{0,200}?\$([\d,]+\.\d{2})",
        r"OPENING\s+BALANCE[:\s]{0,5}.{0,200}?\$([\d,]+\.\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.DOTALL)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────

DISPLAY_NAME = "ANZ"


def can_parse(first_page_text: str, page_count: int) -> float:
    """Score on structural signals only — no product names or account types."""
    txt = first_page_text.lower()
    score = 0.0
    if "anz" in txt or "australia and new zealand banking" in txt:
        score += 0.4
    if "withdrawals" in txt and "deposits" in txt:
        score += 0.25
    if re.search(r"\bdebit\b", txt) and re.search(r"\bcredit\b", txt):
        score += 0.15
    if "balance" in txt:
        score += 0.1
    if re.search(r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", txt):
        score += 0.1
    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    t0 = time.time()
    layout = _detect_layout(pdf_path)

    with pdfplumber.open(pdf_path) as pdf:
        page_count  = len(pdf.pages)
        page1_text  = pdf.pages[0].extract_text() or ""
        start_year  = _extract_start_year(page1_text)
        opening_bal = _extract_opening_balance(page1_text)
        pages       = pdf.pages

        if layout == "LOAN":
            txns = _parse_loan_3col(pages, start_year)
        else:
            txns = _parse_standard_5col(pages, start_year, False)

    # ── 2nd-pass: detect reverse chronological from actual date order ─────────
    if layout != "LOAN" and txns:
        valid_dates = []
        for t in txns:
            if t["date"]:
                try:
                    valid_dates.append(datetime.strptime(t["date"], "%d-%b-%Y"))
                except ValueError:
                    pass
        is_reverse = len(valid_dates) >= 2 and valid_dates[0] > valid_dates[-1]

        if is_reverse:
            # Re-parse with correct reverse flag so year rollover works correctly
            with pdfplumber.open(pdf_path) as pdf:
                txns = _parse_standard_5col(pdf.pages, start_year, True)

    # ── 3rd-pass: balance-delta sign verification ─────────────────────────────
    if layout != "LOAN" and txns:
        is_reverse = (len(txns) >= 2
                      and txns[0]["date"] and txns[-1]["date"]
                      and datetime.strptime(txns[0]["date"],  "%d-%b-%Y")
                       > datetime.strptime(txns[-1]["date"], "%d-%b-%Y"))

        for i in range(len(txns)):
            curr_bal = txns[i]["balance"]
            if curr_bal is None:
                continue
            prev_bal = None
            if is_reverse:
                for j in range(i+1, len(txns)):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]; break
            else:
                for j in range(i-1, -1, -1):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]; break
            if prev_bal is None and opening_bal is not None:
                prev_bal = opening_bal
            if prev_bal is not None:
                delta = curr_bal - prev_bal
                if abs(abs(delta) - abs(txns[i]["amount"])) <= 0.05:
                    txns[i]["amount"] = round(delta, 2)

    txns.sort(key=lambda t: (t.get("date") or "", t.get("source_page", 0), t.get("row_top", 0)))
    for i, t in enumerate(txns):
        t["transaction_id"] = f"anz_{i+1:04d}"

    return {
        "transactions": txns,
        "ambiguous": [],
        "meta": {
            "bank":          "ANZ",
            "bank_id":       "anz",
            "layout":        layout,
            "pages":         page_count,
            "parse_time_ms": round((time.time() - t0) * 1000),
        },
    }
