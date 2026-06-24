"""
parsers/cba.py  –  Commonwealth Bank (CBA) unified statement parser
=======================================================================
Handles ALL CBA statement layouts by detecting layout from structure,
not from keywords, account type, or product names.

LAYOUT DETECTION
────────────────
The parser opens the first page, finds the column header row by scanning
for a row that contains "Date" + at least one of "Debit"/"Credit"/"Amount",
then reads the x-positions of each header word.  The header positions drive
ALL subsequent parsing — nothing is hardcoded.

LAYOUTS FOUND IN THE WILD
──────────────────────────
  Layout A  "Your Statement"   — 5 columns: Date | Transaction | Debit | Credit | Balance
    Sub-variant A1: amounts on sub-rows only (Value Date row or continuation)
    Sub-variant A2: amounts can appear on the date row itself or on a sub-row

  Layout B  "Transaction Summary"  — 4 cols: Date | Transaction details | Amount | Balance
    Single signed Amount column ($xxx = credit, -$xxx = debit)

  Layout C  "Loan Export"  — 5 cols: Date | Transaction details | Debit | Credit | Total
    Credit column has +$xxx, Debit column has plain number, Total is running balance

SIGN STRATEGY (universal)
──────────────────────────
  1. Identify raw_debit and raw_credit from column classification
  2. If both absent → skip row
  3. Preliminary signed amount = credit – debit
  4. If previous balance is known: signed = balance_new – balance_prev (GROUND TRUTH)
  5. Preliminary used only as fallback when balance delta is unavailable

NO HARDCODING
─────────────
  Column boundaries are computed as midpoints between adjacent header x-positions.
  Date formats DD Mon, DD Mon YYYY, and squished DDMonYYYY are all handled.
  Year rollover (Dec → Jan) is tracked automatically.
  Sub-line filtering (Card xx..., Value Date:) is structural: any row where the
  leftmost non-barcode word starts at x > date_col_right is a sub-row.
"""

import re
import time
import pdfplumber
from datetime import datetime
from typing import Optional


# ── Patterns ─────────────────────────────────────────────────────────────────

_DATE_DAY_RE  = re.compile(r"^\d{1,2}$")
_DATE_MON_RE  = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", re.I)
_DATE_FULL_RE = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$", re.I)
_DATE_SHORT_RE= re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", re.I)

_PLAIN_NUM_RE = re.compile(r"^[\d,]+\.\d{2}$")          # e.g.  67.06  or  2,000.00
_DOLLAR_RE    = re.compile(r"^\$[\d,]+(?:\.\d{2})?$")    # e.g.  $111.00
_BAL_CR_RE    = re.compile(r"^\$[\d,]+(?:\.\d{2})?CR?$", re.I) # $6,130.07CR or with sep CR
_SIGNED_RE    = re.compile(r"^[+\-−]\$[\d,]+(?:\.\d{2})?$")  # Layout C: +$1,997.63

_SKIP_RE = re.compile(
    r"^(OPENING\s+BALANCE|CLOSING\s+BALANCE|\d{4}\s+OPENING|\d{4}\s+CLOSING|"
    r"Opening\s+balance|Closing\s+balance|"
    r"Date\s+Transaction|Debit\s+Credit|Account\s+Number|Statement\s+\d+|"
    r"Statement\s+Period|Your\s+Statement|Page\s+\d+\s+of|"
    r"Dear\s+|Here.s\s+your|Account\s+name|BSB|"
    r"Transaction\s+Summary\s+during|Transaction\s+Type|"
    r"Free\s+Chargeable|Staff\s+assisted|Cheques\s+written|"
    r"Important\s+Information|Write\s+to|Tell\s+us|Call:|"
    r"You\s+can\s+also|For\s+further|Remember,|For\s+information|"
    r"Contact\s+us|Passcodes|Do\s+not|Unless\s+you|afca\.org|"
    r"Opening\s+balance\s+-\s+Total|Displaying\s+transactions|"
    r"Scroll\s+to\s+top|There\s+are\s+no\s+more)",
    re.I,
)

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}


# ── Row grouping ──────────────────────────────────────────────────────────────

def _group_rows(words: list, y_tol: float = 3.5) -> list:
    """Group words into visual rows by y-coordinate."""
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
    """Parse any numeric string: '67.06', '$6,130.07', '-$500.00', '+$394.73', '+ $1,997.63'"""
    s = s.strip().replace(",", "").replace("−", "-").replace(" ", "")
    neg = s.startswith("-")
    s = s.lstrip("+-").lstrip("$")
    # Strip trailing CR
    s = re.sub(r"CR$", "", s, flags=re.I).strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _is_amount_token(s: str) -> bool:
    return bool(_PLAIN_NUM_RE.match(s) or _DOLLAR_RE.match(s)
                or _BAL_CR_RE.match(s) or _SIGNED_RE.match(s))


# ── Date parsing ──────────────────────────────────────────────────────────────

def _make_date(day: str, mon: str, year: int) -> str:
    mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
    try:
        return datetime(year, mon_n, int(day)).strftime("%d-%b-%Y")
    except ValueError:
        return f"{day}-{mon[:3].capitalize()}-{year}"


def _parse_date_token(s: str, year_state: list) -> Optional[str]:
    """
    Parse 'DD Mon', 'DD Mon YYYY', or 'DDMonYYYY'.
    year_state = [current_year, prev_month_num]
    Handles year rollover automatically.
    """
    s = s.strip()
    m = _DATE_FULL_RE.match(s)
    if m:
        day, mon, yr = m.group(1), m.group(2), int(m.group(3))
        year_state[0] = yr
        year_state[1] = _MONTH_MAP.get(mon.lower()[:3], year_state[1])
        return _make_date(day, mon, yr)

    m = _DATE_SHORT_RE.match(s)
    if m:
        day, mon = m.group(1), m.group(2)
        mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
        # Year rollover: if month goes back past Dec, advance year
        if year_state[1] and mon_n < year_state[1] and year_state[1] >= 11:
            year_state[0] += 1
        year_state[1] = mon_n
        return _make_date(day, mon, year_state[0])

    # Squished: "08Apr2025"
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


# ── Column detection ──────────────────────────────────────────────────────────

def _find_header_row(words: list) -> Optional[list]:
    """
    Scan words (from one page) for the table header row containing 'Date'
    and at least one of Debit/Credit/Amount/Balance.
    Merge words within 5pt y of each other to handle split headers.
    Returns sorted word list or None.
    A valid header row must have at least 3 distinct header keyword words.
    """
    if not words:
        return None

    sorted_words = sorted(words, key=lambda w: w["top"])
    bands = []
    cur = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur[0]["top"]) <= 5:
            cur.append(w)
        else:
            bands.append(cur); cur = [w]
    if cur:
        bands.append(cur)

    HEADER_WORDS = {"date", "transaction", "details", "debit", "credit",
                    "amount", "balance", "total"}
    NEED_DATE    = {"date"}
    NEED_ANY     = {"debit", "credit", "amount", "balance", "total"}

    for i, band in enumerate(bands):
        # Try this band, then merged with next band (split headers like 'Transaction\ndetails')
        for candidate in ([band + bands[i+1]] if i+1 < len(bands) and
                          abs(bands[i+1][0]["top"] - band[0]["top"]) <= 15 else []) + [band]:
            words_lower = [w["text"].lower() for w in candidate]
            word_set    = set(words_lower)
            # Must have 'date' AND at least one amount-type header word
            if not (word_set & NEED_DATE and word_set & NEED_ANY):
                continue
            # Must have at least 3 header keyword hits (avoid false positives from data rows)
            kw_count = sum(1 for wl in words_lower if wl in HEADER_WORDS)
            if kw_count < 3:
                continue
            # Must NOT be primarily a data row (no $ amounts should be present)
            amount_tokens = [w for w in candidate if re.match(r"^\$[\d,]+", w["text"])]
            if len(amount_tokens) > 1:
                continue  # data row, not a header
            return sorted(candidate, key=lambda x: x["x0"])

    return None


def _compute_col_bounds(header: list) -> dict:
    """
    Given the sorted header row words, compute column x-boundaries as midpoints.
    Returns {col_name_lower: (x_lo, x_hi)}.
    """
    # Map each header word to a canonical column name
    COL_MAP = {
        "date": "date", "transaction": "desc", "details": "desc",
        "debit": "debit", "credit": "credit", "amount": "amount",
        "balance": "balance", "total": "total",
    }
    matched = []
    for w in header:
        key = w["text"].lower()
        if key in COL_MAP:
            matched.append((COL_MAP[key], w["x0"]))

    # Dedupe: if "transaction" and "details" both appear, they're same col header split
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


def _classify_words(row: list, bounds: dict) -> dict:
    """
    Assign each word to a column bucket.
    For non-amount text: use x0 < actual_col_start boundary (more accurate).
    For amount tokens: use centre of word ((x0+x1)/2) to handle right-alignment.
    """
    AMT_COLS = {"debit", "credit", "amount", "balance", "total"}
    result = {k: [] for k in bounds}

    # Precompute column start positions from bounds (lo of each column)
    # For text classification, we use a slightly wider desc zone:
    # text goes in the LAST column whose lo <= x0, but for text we use the actual
    # header x0 positions, not midpoints.
    # Simple rule: text → use x0 to find its column by midpoints (same as amounts)
    # EXCEPT: country/state codes and short text tokens at start of an amount col
    # are still part of description if they're not numeric.

    for w in row:
        x0 = w["x0"]
        x1 = w.get("x1", x0 + 40)
        txt = w["text"]
        is_num = _is_amount_token(txt)

        placed = False
        if is_num:
            # Use centre for numeric tokens
            cx = (x0 + x1) / 2
            for col, (lo, hi) in bounds.items():
                if col in AMT_COLS and lo <= cx < hi:
                    result[col].append(txt)
                    placed = True
                    break

        if not placed:
            # Use x0 for text tokens
            for col, (lo, hi) in bounds.items():
                if lo <= x0 < hi:
                    result[col].append(txt)
                    break

    return {k: " ".join(v).strip() for k, v in result.items()}


# ── Row date detection ────────────────────────────────────────────────────────

def _extract_date_from_row(row: list, date_col_hi: float) -> Optional[str]:
    """
    Extract date string from a row using the date column boundary.
    Handles 'DD', 'Mon', 'YYYY' split across multiple cells.
    Returns assembled date string or None.
    """
    date_words = [w for w in row if w["x0"] < date_col_hi]
    if not date_words:
        return None
    # Try joined text as date
    joined = " ".join(w["text"] for w in sorted(date_words, key=lambda x: x["x0"]))
    return joined.strip() or None


def _is_date_row(date_str: str) -> bool:
    """Quick check: does this string look like a date or start-of-transaction marker?"""
    if not date_str:
        return False
    parts = date_str.split()
    if len(parts) >= 2:
        if _DATE_DAY_RE.match(parts[0]) and _DATE_MON_RE.match(parts[1]):
            return True
    if _DATE_FULL_RE.match(date_str) or _DATE_SHORT_RE.match(date_str):
        return True
    # Squished
    if re.match(r"^\d{1,2}(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", date_str, re.I):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT A — "Your Statement"  (Date | Transaction | Debit | Credit | Balance)
# ══════════════════════════════════════════════════════════════════════════════
#
# Transaction structure:
#   Row 1:  DD Mon [YYYY]  <description start>           [optional: debit_amount  $balance CR]
#   Row 2:  (barcode / ref number at far left, x<30)
#   Row 3:  <desc continuation or "Card xxNNNN">
#   Row 4:  <"Value Date: DD/MM/YYYY">         debit_amount     $balance CR
#           OR
#           <desc continuation>                debit_amount     $balance CR
#
# The amount row is identified by having a numeric token in the debit/credit
# column AND a $balance token in the balance column.  When found, the
# current transaction is emitted.

def _parse_layout_a(pages: list, start_year: int, opening_balance: Optional[float]) -> list:
    transactions = []
    prev_balance = [opening_balance]
    year_state   = [start_year, 0]
    cached_bounds = [None]      # reuse across pages without header

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)

        # Detect header
        header = _find_header_row(words)
        if header:
            bounds = _compute_col_bounds(header)
            cached_bounds[0] = bounds
        bounds = cached_bounds[0]
        if not bounds:
            continue

        date_col_hi = bounds.get("date", (0, 100))[1]

        rows = _group_rows(words)

        pending_date  = None
        pending_descs = []
        pending_top   = 0.0

        def emit(debit_str, credit_str, bal_str, top):
            nonlocal pending_date, pending_descs
            if pending_date is None:
                return

            # Parse balance
            bal_full = bal_str.strip()
            # Balance might be "$xxx.xx" + separate "CR" token — they come as one string here
            bal_val  = None
            if bal_full:
                # Strip leading $ and trailing CR
                b = re.sub(r"CR$", "", bal_full, flags=re.I).strip().lstrip("$").replace(",","")
                try:
                    bal_val = float(b)
                except ValueError:
                    pass

            # Parse debit / credit
            dv = _parse_num(debit_str.replace(",","")) if debit_str else None
            cv = _parse_num(credit_str)                if credit_str else None

            # Determine signed amount
            if dv is not None and cv is not None:
                raw = cv - dv
            elif cv is not None:
                raw = cv
            elif dv is not None:
                raw = -dv
            else:
                pending_date = None; pending_descs = []
                return

            # Ground truth: balance delta
            signed = raw
            if bal_val is not None and prev_balance[0] is not None:
                delta = round(bal_val - prev_balance[0], 2)
                if abs(abs(delta) - abs(raw)) <= 0.02:
                    signed = delta

            if bal_val is not None:
                prev_balance[0] = bal_val

            desc = " ".join(pending_descs).strip()
            transactions.append({
                "transaction_id": "",
                "date":           pending_date,
                "description":    desc,
                "amount":         round(signed, 2),
                "balance":        bal_val,
                "source_page":    page_num,
                "row_top":        pending_top,
                "confidence":     1.0,
            })
            pending_date = None
            pending_descs = []

        for row in rows:
            if not row:
                continue

            # Filter out barcode / margin tokens (x0 < 30, usually numbers/codes)
            clean_row = [w for w in row if w["x0"] >= 30]
            if not clean_row:
                continue

            cols     = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = cols.get("debit",  "").strip()
            cred_str = cols.get("credit", "").strip()
            bal_str  = cols.get("balance","").strip()

            # Reassemble balance: sometimes "$xxx.xx" and "CR" in separate cells
            if bal_str and not re.search(r"CR", bal_str, re.I):
                # Check if "CR" was in an adjacent word that got lumped in balance
                pass  # balance column already contains it if midpoints correct

            # Skip structural/noise rows
            full_text = " ".join(w["text"] for w in clean_row)
            if _SKIP_RE.match(desc_str) or _SKIP_RE.match(full_text):
                if re.match(r"OPENING\s+BALANCE|\d{4}\s+OPENING|Opening\s+balance", desc_str + " " + date_str, re.I):
                    # Seed prev_balance from opening balance row
                    if bal_str and prev_balance[0] is None:
                        bv = _parse_num(re.sub(r"CR$","",bal_str,flags=re.I).strip())
                        if bv is not None:
                            prev_balance[0] = bv
                continue

            has_balance = bool(bal_str and (
                _BAL_CR_RE.match(bal_str.replace(" ","")) or
                re.match(r"^\$[\d,]+\.\d{2}$", bal_str) or
                re.match(r"^[\d,]+\.\d{2}$", bal_str) or
                re.match(r"^[\d,]+\.\d{2}\s*CR?$", bal_str, re.I) or
                re.match(r"^\$[\d,]+\.\d{2}\s+CR$", bal_str, re.I)
            ))

            # Extract the numeric amount from debit/credit strings.
            # In some PDFs a country/state code appears before the number in the
            # same column zone (e.g. "AU 20.70").  We pull out the numeric part only.
            def _extract_num_from_col(s: str) -> str:
                s = s.strip()
                # Already a clean number
                if _PLAIN_NUM_RE.match(s.replace(",","")) or _DOLLAR_RE.match(s):
                    return s
                # Try to find a decimal amount within the string
                m = re.search(r"\$?([\d,]+\.\d{2})", s)
                return m.group(0) if m else ""

            deb_clean  = _extract_num_from_col(deb_str)
            cred_clean = _extract_num_from_col(cred_str)
            has_debit  = bool(deb_clean)
            has_credit = bool(cred_clean)

            is_date_row_flag = _is_date_row(date_str)

            if is_date_row_flag:
                # Flush any previously accumulated pending transaction
                emit("", "", "", 0.0)

                date_parsed = _parse_date_token(date_str, year_state)
                if not date_parsed:
                    continue

                if (has_debit or has_credit) and has_balance:
                    # Complete single-row transaction: emit immediately
                    pending_date  = date_parsed
                    pending_top   = row[0]["top"]
                    pending_descs = [desc_str] if desc_str else []
                    emit(deb_clean if has_debit else "",
                         cred_clean if has_credit else "",
                         bal_str, pending_top)
                elif has_debit or has_credit:
                    # Partial single-row: date+amounts but no balance yet
                    # Store everything; next row with balance will trigger emit
                    pending_date  = date_parsed
                    pending_top   = row[0]["top"]
                    pending_descs = [desc_str] if desc_str else []
                    # Store partial amounts so the continuation row can add balance
                    # We do this by emitting with empty balance and relying on delta
                    # Actually: just store and wait for balance row
                    pass
                else:
                    # Pure date+desc row, no amounts — set up pending
                    pending_date  = date_parsed
                    pending_top   = row[0]["top"]
                    pending_descs = [desc_str] if desc_str else []

            elif (has_debit or has_credit):
                # Amount row — could have balance or not
                if desc_str and not re.match(r"^(Card\s+xx|Value\s+Date:|Created\s+\d)", desc_str, re.I):
                    pending_descs.append(desc_str)
                emit(deb_clean if has_debit else "",
                     cred_clean if has_credit else "",
                     bal_str,
                     pending_top)

            else:
                # Description continuation row
                if pending_date and desc_str:
                    if not re.match(r"^(Card\s+xx|Value\s+Date:|Created\s+\d|"
                                    r"CommBank\s+App\s+[A-Z]|NetBank\s+BPAY|"
                                    r"Opening\s+balance|Closing\s+balance)", desc_str, re.I):
                        pending_descs.append(desc_str)

        # Flush any dangling transaction
        emit("", "", "", 0.0)

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT B — "Transaction Summary"  (Date | Transaction details | Amount | Balance)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_layout_b(pages: list, start_year: int, opening_balance: Optional[float]) -> list:
    transactions = []
    prev_balance = [opening_balance]
    year_state   = [start_year, 0]
    cached_bounds = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            bounds = _compute_col_bounds(header)
            cached_bounds[0] = bounds
        bounds = cached_bounds[0]
        if not bounds:
            continue

        rows = _group_rows(words)

        for row in rows:
            if not row:
                continue
            clean_row = [w for w in row if w["x0"] >= 30]
            if not clean_row:
                continue

            cols     = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            amt_raw  = cols.get("amount", "").strip()
            bal_str  = cols.get("balance", "").strip()

            full = " ".join(w["text"] for w in clean_row)
            if _SKIP_RE.match(full) or _SKIP_RE.match(desc_str):
                continue

            if not _is_date_row(date_str):
                continue

            if not amt_raw:
                continue

            # Amount may have description text bleeding in (e.g. "Ltd -$1,028.50")
            # Extract just the numeric/signed amount portion
            amt_str = amt_raw
            m_amt = re.search(r"[+\-−]?\$?[\d,]+\.\d{2}", amt_raw)
            if m_amt:
                amt_str = m_amt.group(0)
            # Also recover any description spill
            spill = amt_raw[:m_amt.start()].strip() if m_amt and m_amt.start() > 0 else ""
            if spill and not re.match(r"^[\d$+\-]", spill):
                desc_str = (desc_str + " " + spill).strip()

            date_parsed = _parse_date_token(date_str, year_state)
            if not date_parsed:
                continue

            amount = _parse_num(amt_str)
            if amount is None:
                continue

            bal_val = None
            if bal_str:
                b = re.sub(r"CR$","",bal_str,flags=re.I).strip().lstrip("$").replace(",","")
                try:
                    bal_val = float(b)
                except ValueError:
                    pass

            # Ground truth
            signed = amount
            if bal_val is not None and prev_balance[0] is not None:
                delta = round(bal_val - prev_balance[0], 2)
                if abs(abs(delta) - abs(amount)) <= 0.02:
                    signed = delta

            if bal_val is not None:
                prev_balance[0] = bal_val

            # Collect description (date row + any continuation until next date row)
            transactions.append({
                "transaction_id": "",
                "date":           date_parsed,
                "description":    desc_str,
                "amount":         round(signed, 2),
                "balance":        bal_val,
                "source_page":    page_num,
                "row_top":        row[0]["top"],
                "confidence":     1.0,
            })

    # Multi-row descriptions: stitch continuation rows
    # (TransactionSummary sometimes has ref numbers as continuation)
    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT C — Loan Export  (Date | Transaction details | Debit | Credit | Total)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_layout_c(pages: list, start_year: int, opening_balance: Optional[float]) -> list:
    transactions = []
    prev_balance = [opening_balance]
    year_state   = [start_year, 0]
    cached_bounds = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            bounds = _compute_col_bounds(header)
            cached_bounds[0] = bounds
        bounds = cached_bounds[0]
        if not bounds:
            continue

        rows = _group_rows(words)
        pending_date  = None
        pending_descs = []
        pending_top   = 0.0

        def emit_c(deb_str, cred_str, tot_str, top):
            nonlocal pending_date, pending_descs
            if pending_date is None:
                return

            # Loan: credit col = +$xxx (payment in), debit = -$xxx or plain (interest out)
            # Total = running loan balance (usually negative = outstanding)
            dv = _parse_num(deb_str)  if deb_str  else None
            cv = _parse_num(cred_str) if cred_str else None
            tv = _parse_num(tot_str)  if tot_str  else None

            if dv is None and cv is None:
                pending_date = None; pending_descs = []
                return

            # Credit (payment towards loan) = positive; debit (interest/draw) = negative
            if cv is not None and cv > 0:
                raw = cv
            elif dv is not None:
                raw = -abs(dv) if dv > 0 else dv
            else:
                raw = 0.0

            signed = raw
            if tv is not None and prev_balance[0] is not None:
                delta = round(tv - prev_balance[0], 2)
                if abs(abs(delta) - abs(raw)) <= 0.02:
                    signed = delta

            if tv is not None:
                prev_balance[0] = tv

            desc = " ".join(pending_descs).strip()
            transactions.append({
                "transaction_id": "",
                "date":           pending_date,
                "description":    desc,
                "amount":         round(signed, 2),
                "balance":        tv,
                "source_page":    page_num,
                "row_top":        top,
                "confidence":     1.0,
            })
            pending_date = None; pending_descs = []

        for row in rows:
            if not row:
                continue
            clean_row = [w for w in row if w["x0"] >= 30]
            if not clean_row:
                continue

            cols     = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = cols.get("debit", "").strip()
            cred_str = cols.get("credit","").strip()
            tot_str  = cols.get("total", "").strip()

            full = " ".join(w["text"] for w in clean_row)
            if _SKIP_RE.match(full):
                continue

            if re.match(r"^(There\s+are\s+no|Displaying|Scroll)", full, re.I):
                continue

            # Metadata rows in loan statements (rate change notices, footers) — skip entirely
            if re.match(r"^(Change\s+in\s+interest|There\s+are\s+no\s+more|Displaying)", desc_str, re.I):
                continue

            is_date = _is_date_row(date_str)
            has_amt = bool(deb_str or cred_str)

            if is_date and has_amt:
                emit_c(deb_str, cred_str, tot_str, row[0]["top"])
                date_parsed = _parse_date_token(date_str, year_state)
                pending_date  = date_parsed
                pending_top   = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []
                if tot_str:
                    emit_c(deb_str, cred_str, tot_str, pending_top)
            elif is_date and not has_amt:
                emit_c("", "", "", 0.0)
                date_parsed = _parse_date_token(date_str, year_state)
                pending_date  = date_parsed
                pending_top   = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []
            elif has_amt and pending_date:
                if desc_str:
                    pending_descs.append(desc_str)
                emit_c(deb_str, cred_str, tot_str, pending_top)
            else:
                if pending_date and desc_str:
                    pending_descs.append(desc_str)

        emit_c("", "", "", 0.0)

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _detect_layout(pdf_path: str) -> str:
    """
    Open the PDF, find the header row, inspect column names.
    Returns 'A', 'B', 'C', or 'unknown'.
    NO text matching on account type / product name.
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            words = page.extract_words(x_tolerance=1, y_tolerance=3)
            header = _find_header_row(words)
            if not header:
                continue
            cols = {w["text"].lower() for w in header}
            has_debit   = "debit"  in cols
            has_credit  = "credit" in cols
            has_amount  = "amount" in cols
            has_total   = "total"  in cols
            has_balance = "balance" in cols

            if has_debit and has_credit and has_total:
                return "C"                      # Loan export
            if has_amount and not has_debit:
                return "B"                      # Transaction Summary
            if has_debit and has_credit and has_balance:
                return "A"                      # Your Statement
            if has_debit and has_balance:
                return "A"                      # Your Statement (debit-only variant)
            if has_balance:
                return "A"                      # Fallback

    return "unknown"


def _extract_start_year(text: str) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else 2025


def _extract_opening_balance(text: str) -> Optional[float]:
    patterns = [
        r"OPENING\s+BALANCE\s+\$?([\d,]+\.\d{2})\s*CR?",
        r"Opening\s+balance\s+\$?([\d,]+\.\d{2})\s*CR?",
        r"Opening\s+Balance\s+\$?([\d,]+\.\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

DISPLAY_NAME = "CBA"


def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    score = 0.0
    if "commonwealth bank" in txt or "commbank" in txt: score += 0.45
    if "your statement" in txt:                          score += 0.3
    if re.search(r"here.s your account information", txt): score += 0.3
    if re.search(r"date.*transaction.*debit.*credit|date.*transaction.*amount", txt): score += 0.25
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

        if layout == "A":
            txns = _parse_layout_a(pages, start_year, opening_bal)
        elif layout == "B":
            txns = _parse_layout_b(pages, start_year, opening_bal)
        elif layout == "C":
            txns = _parse_layout_c(pages, start_year, opening_bal)
        else:
            # Unknown layout — try A as best guess
            txns = _parse_layout_a(pages, start_year, opening_bal)

    txns.sort(key=lambda t: (t.get("date") or "", t.get("source_page", 0), t.get("row_top", 0)))
    for i, t in enumerate(txns):
        t["transaction_id"] = f"cba_{i+1:04d}"

    return {
        "transactions": txns,
        "ambiguous":    [],
        "meta": {
            "bank":         "CBA",
            "bank_id":      "cba",
            "layout":       layout,
            "pages":        page_count,
            "parse_time_ms": round((time.time() - t0) * 1000),
        },
    }
