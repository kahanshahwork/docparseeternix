"""
parsers/nab.py  –  NAB Unified Statement Parser
=======================================================================
Handles two distinct NAB layouts:

LAYOUT A — NAB Business Everyday / Classic Banking (portrait, multi-page)
  Header: Date | Particulars | Debits | Credits | Balance
  Column x positions (approximate):
    Date:        x0 ~39–80   (only on first sub-row of each transaction)
    Particulars: x0 ~102–365 (ALL sub-rows)
    Debits:      x0 ~355–430 (money going OUT, positive number)
    Credits:     x0 ~430–495 (money coming IN, positive number)
    Balance:     x0 ~495+    (running balance, suffixed with Cr/Dr)
  A transaction spans 1–N visual rows; date only appears on the FIRST.
  Multiple transactions can share the same date block.

LAYOUT B — NAB Credit Card (landscape, single Amount A$ column)
  Page width > height → landscape
  Columns: Date | Amount A$ | Details | ...
  Date: "DD Mon YYYY" at x0 ~48-80
  Amount: "$XX.XX" at x0 ~118-184 (negative = payment, prefixed CR in desc)
"""

import re
import time
import pdfplumber
from datetime import datetime
from typing import Optional


# ── Patterns ─────────────────────────────────────────────────────────────────

_DATE_SHORT_RE = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?$", re.I)
_DATE_FULL_RE  = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{2,4})$", re.I)
_DATE_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")

# Require decimal point to avoid treating reference numbers as amounts
_PLAIN_NUM_RE = re.compile(r"^[+\-−]?[\d,]+\.\d{2}(?:\s*(CR|DR))?$", re.I)
_DOLLAR_RE    = re.compile(r"^\$[+\-−]?[\d,]+(?:\.\d{2})?(?:\s*(CR|DR))?$", re.I)
_SIGNED_RE    = re.compile(r"^[+\-−]\$[\d,]+(?:\.\d{2})?(?:\s*(CR|DR))?$", re.I)

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

_DOT_RE = re.compile(r"^\.{3,}$")   # long dot-fill sequences

_SKIP_ROW_RE = re.compile(
    r"(TOTALS|Please\s+check|Page\s+\d+\s+of|Statement\s+number|"
    r"National\s+Australia\s+Bank|Transaction\s+Details|Account\s+Details|"
    r"BSB\s+Number|Account\s+Balance|Opening\s+balance|"
    r"Total\s+credits|Total\s+debits|Closing\s+balance|Statement\s+starts|"
    r"Statement\s+ends|For\s+further\s+information|"
    r"TRANSACTION\s+SUMMARY|Banker\s+Assisted|Transaction\s+Fees|"
    r"Account\s+Service\s+Fee|Total\s+Fees|Less\s+Fee\s+Rebate)",
    re.I,
)

# Column zones for NAB Business Everyday — DEFAULTS only.
# These are overridden per-page by _calibrate_columns() which reads
# the actual "Debits / Credits / Balance" header row positions.
# This means the parser survives margin changes, scaling, or layout drift
# across different NAB statement generations without any code changes.
_DATE_X_MAX  = 100   # date words: day/month/year tokens
_PART_X_MIN  = 100   # particulars column starts
_PART_X_MAX  = 360   # particulars column ends
_DEB_X_MIN   = 355   # debits column fallback
_DEB_X_MAX   = 430   # debits column fallback end
_CRED_X_MIN  = 430   # credits column fallback
_CRED_X_MAX  = 495   # credits column fallback end
_BAL_X_MIN   = 495   # balance column fallback


def _calibrate_columns(words: list) -> dict:
    """
    Derive Debit / Credit / Balance column boundaries dynamically by
    finding the transaction table header row on the page.

    Strategy:
      1. Group words by y-position (5pt buckets to handle sub-pixel drift).
      2. Find the row that contains both "Debits" AND "Credits" — this is
         the transaction table header, not the summary section.
      3. Use the midpoint between adjacent header word centres as zone splits.
      4. Fall back to hardcoded defaults if the header is not found.

    This makes the parser immune to:
      - Margin changes (page shifted left/right)
      - Font scaling (header words wider/narrower)
      - Layout drift between NAB statement generations
      - Large amounts like 100,000.00 whose x0 drifts left of the zone edge
    """
    from collections import defaultdict

    # Group words into horizontal rows using 5pt y-buckets
    by_y = defaultdict(list)
    for w in words:
        by_y[round(w["top"] / 5) * 5].append(w)

    # Find the row that has both "Debits" and "Credits" — that's the table header
    header_row = None
    for y_bucket in sorted(by_y.keys()):
        row = by_y[y_bucket]
        texts = {w["text"].lower() for w in row}
        if "debits" in texts and "credits" in texts:
            header_row = row
            break

    if header_row is None:
        # Header not on this page — return hardcoded defaults
        return {
            "deb_min":  _DEB_X_MIN,
            "deb_max":  _DEB_X_MAX,
            "cred_min": _CRED_X_MIN,
            "cred_max": _CRED_X_MAX,
            "bal_min":  _BAL_X_MIN,
        }

    # Extract the key header words
    header = {}
    for w in header_row:
        key = w["text"].lower()
        if key in ("debits", "credits", "balance", "particulars"):
            header[key] = w

    def mid(w):
        return (w["x0"] + w["x1"]) / 2

    deb_mid  = mid(header["debits"])
    cred_mid = mid(header["credits"])
    bal_mid  = mid(header["balance"]) if "balance" in header else cred_mid + 70

    # Zone split = midpoint between adjacent column header centres (no padding)
    split_deb_cred = (deb_mid + cred_mid) / 2
    split_cred_bal = (cred_mid + bal_mid)  / 2

    return {
        "deb_min":  _DATE_X_MAX,
        "deb_max":  split_deb_cred,
        "cred_min": split_deb_cred,
        "cred_max": split_cred_bal,
        "bal_min":  split_cred_bal,
    }

# Credit card column zones
_CC_DATE_X_MAX   = 100
_CC_AMT_X_MIN    = 100
_CC_AMT_X_MAX    = 184
_CC_DESC_X_MIN   = 184


# ── Utilities ─────────────────────────────────────────────────────────────────

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

def _parse_num(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(",", "").replace("−", "-").replace(" ", "")
    is_dr = s.upper().endswith("DR")
    neg = s.startswith("-") or is_dr
    s = s.lstrip("+-").lstrip("$")
    s = re.sub(r"(CR|DR)$", "", s, flags=re.I).strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None

def _is_amount_token(s: str) -> bool:
    return bool(_PLAIN_NUM_RE.match(s) or _DOLLAR_RE.match(s) or _SIGNED_RE.match(s))

def _is_dot_fill(s: str) -> bool:
    return bool(_DOT_RE.match(s))

def _parse_date_from_words(words_in_date_col: list, year_state: list) -> Optional[str]:
    """Try to parse date from words with x0 < _DATE_X_MAX."""
    if not words_in_date_col:
        return None
    text = " ".join(w["text"] for w in words_in_date_col)
    # Full: "8 Oct 2024"
    m = re.match(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{4})", text, re.I)
    if m:
        day, mon, yr = m.group(1), m.group(2), int(m.group(3))
        year_state[0] = yr
        mon_n = _MONTH_MAP.get(mon.lower()[:3], year_state[1])
        year_state[1] = mon_n
        try:
            return datetime(yr, mon_n, int(day)).strftime("%d-%b-%Y")
        except ValueError:
            pass
    # Short: "8 Oct"
    m = re.match(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?", text, re.I)
    if m:
        day, mon = m.group(1), m.group(2)
        mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
        if year_state[1]:
            if mon_n < 3 and year_state[1] > 10:
                year_state[0] += 1
        year_state[1] = mon_n
        yr = year_state[0]
        try:
            return datetime(yr, mon_n, int(day)).strftime("%d-%b-%Y")
        except ValueError:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT A — NAB Business Everyday / Classic Banking
# ══════════════════════════════════════════════════════════════════════════════

def _is_nab_business_page(words: list) -> bool:
    texts_lower = {w["text"].lower() for w in words}
    return "particulars" in texts_lower and ("debits" in texts_lower or "credits" in texts_lower)


def _parse_nab_business(pages: list, start_year: int) -> tuple:
    transactions = []
    year_state   = [start_year, 0]
    opening_bal  = None

    # These persist ACROSS pages: a transaction page can start mid-date-group
    # (e.g. right after "Brought forward" with no date row of its own — the
    # date carries over from the last date row seen on a previous page).
    current_date  = None
    pending_date  = None
    pending_descs = []
    pending_top   = 0.0
    pending_page  = 1

    def emit(deb_str: str, cred_str: str, bal_str: str):
        nonlocal pending_date, pending_descs
        if not pending_date:
            pending_descs = []
            return
        bal_val = _parse_num(bal_str) if bal_str else None
        dv = _parse_num(deb_str) if deb_str else None
        cv = _parse_num(cred_str) if cred_str else None
        if dv is None and cv is None:
            pending_date = None
            pending_descs = []
            return
        # Debit = money out → negative; Credit = money in → positive
        raw = (cv or 0.0) - (dv or 0.0)
        transactions.append({
            "transaction_id": "",
            "date":          pending_date,
            "description":   " ".join(pending_descs).strip(),
            "amount":        round(raw, 2),
            "balance":       bal_val,
            "source_page":   pending_page,
            "row_top":       pending_top,
            "confidence":    1.0,
        })
        pending_date = None
        pending_descs = []

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)

        # Self-calibrate column boundaries from this page's header row.
        # Falls back to hardcoded defaults if no header found (e.g. cover pages).
        col = _calibrate_columns(words)

        if not _is_nab_business_page(words):
            # Try to pull opening balance from summary text on cover pages
            if opening_bal is None:
                txt = page.extract_text() or ""
                m = re.search(r"Opening\s+balance\s+\$?([\d,]+\.\d{2})", txt, re.I)
                if m:
                    opening_bal = float(m.group(1).replace(",", ""))
            continue

        rows = _group_rows(words)
        pending_page = page_num

        for row in rows:
            # Filter sidebar print codes (x0 < 17) and footer (top > 790)
            visible = [w for w in row if w["x0"] >= 17 and w["top"] < 790]
            if not visible:
                continue

            full_text = " ".join(w["text"] for w in visible)

            # Skip header rows and boilerplate (search, not match — rows often
            # have a date prefix before the boilerplate keyword)
            if _SKIP_ROW_RE.search(full_text):
                continue
            if re.search(r"\b(transaction\s+details|date\s+particulars)\b", full_text, re.I):
                continue
            # Brought forward / Carried forward → extract balance, skip as transaction
            if re.search(r"\b(brought\s+forward|carried\s+forward)\b", full_text, re.I):
                for w in visible:
                    if w["x0"] >= _BAL_X_MIN and _is_amount_token(w["text"]):
                        opening_bal = _parse_num(w["text"])
                        break
                continue

            # Classify words by column zone
            date_words  = [w for w in visible
                           if w["x0"] < _DATE_X_MAX
                           and not _is_amount_token(w["text"])
                           and not _is_dot_fill(w["text"])
                           and w["text"].lower() not in ("cr", "dr")]
            part_words  = [w for w in visible
                           if _PART_X_MIN <= w["x0"] < _PART_X_MAX
                           and not _is_dot_fill(w["text"])
                           and w["text"].lower() not in ("cr", "dr")]
            deb_words   = [w for w in visible
                           if col["deb_min"] <= (w["x0"]+w["x1"])/2 < col["deb_max"]
                           and _is_amount_token(w["text"])]
            cred_words  = [w for w in visible
                           if col["cred_min"] <= (w["x0"]+w["x1"])/2 < col["cred_max"]
                           and _is_amount_token(w["text"])]
            bal_words   = [w for w in visible
                           if (w["x0"]+w["x1"])/2 >= col["bal_min"]
                           and _is_amount_token(w["text"])
                           and w["text"].lower() not in ("cr", "dr")]

            desc_text = " ".join(
                w["text"] for w in part_words
                if not _is_dot_fill(w["text"])
            ).strip()
            deb_str  = deb_words[0]["text"]  if deb_words  else ""
            cred_str = cred_words[0]["text"] if cred_words else ""
            bal_str  = bal_words[0]["text"]  if bal_words  else ""

            # Check for a date on this row
            found_date = _parse_date_from_words(date_words, year_state)

            if found_date:
                # Flush any previous pending transaction
                emit(deb_str if not (deb_str or cred_str) else "", "", "")
                # Update active date
                current_date = found_date
                # Start new pending transaction
                pending_date = found_date
                pending_top  = visible[0]["top"]
                pending_descs = [desc_text] if desc_text else []
                if deb_str or cred_str:
                    # Single-row transaction (date + amount on same row)
                    emit(deb_str, cred_str, bal_str)
                    # After emit, pending_date=None but current_date stays
                    # Next desc-only row will start a new txn with current_date
            elif deb_str or cred_str:
                if pending_date:
                    # This row has an amount — close the current pending transaction
                    if desc_text:
                        pending_descs.append(desc_text)
                    emit(deb_str, cred_str, bal_str)
                elif current_date:
                    # Amount row but no pending (previous txn already emitted)
                    # This means we have an amount-only continuation → open+emit immediately
                    pending_date  = current_date
                    pending_top   = visible[0]["top"]
                    pending_descs = [desc_text] if desc_text else []
                    emit(deb_str, cred_str, bal_str)
            elif desc_text:
                if pending_date:
                    # Continuation description line
                    pending_descs.append(desc_text)
                elif current_date:
                    # Description line for a NEW transaction (same date as previous)
                    pending_date  = current_date
                    pending_top   = visible[0]["top"]
                    pending_descs = [desc_text]

        # End of page: do NOT flush — a pending transaction may continue
        # onto the next transaction page (date carries across page boundary)

    return transactions, opening_bal


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT B — NAB Credit Card (landscape)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_nab_credit_card(pages: list, start_year: int) -> tuple:
    transactions = []
    year_state = [start_year, 0]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not words:
            continue

        rows = _group_rows(words, y_tol=4.0)

        for row in rows:
            visible = [w for w in row if w["x0"] > 30 and w["top"] < 570]
            if not visible:
                continue

            full_text = " ".join(w["text"] for w in visible)
            if _SKIP_ROW_RE.search(full_text):
                continue
            if re.match(r"^(Date|Amount|Details|Explanation|GST|Reference|Transaction\s+record)", full_text, re.I):
                continue
            if re.match(r"^(this\s+period|Totals?)", full_text, re.I):
                continue
            if all(w["text"] == "_" for w in visible):
                continue

            date_words   = [w for w in visible if w["x0"] < _CC_DATE_X_MAX
                            and not w["text"].startswith("$")
                            and re.match(r"^\d{1,2}$|^[A-Za-z]{3}$|^\d{4}$", w["text"])]
            amount_words = [w for w in visible
                            if _CC_AMT_X_MIN <= w["x0"] < _CC_AMT_X_MAX
                            and w["text"].startswith("$")]
            # "CR" suffix can appear just after the $ amount, before the desc zone starts
            cr_words     = [w for w in visible
                            if _CC_AMT_X_MIN <= w["x0"] < _CC_DESC_X_MIN
                            and w["text"].upper() == "CR"]
            desc_words   = [w for w in visible
                            if w["x0"] >= _CC_DESC_X_MIN
                            and w["text"] != "_"
                            and not re.match(r"^_+$", w["text"])]

            if not amount_words:
                continue
            found_date = _parse_date_from_words(date_words, year_state)
            if not found_date:
                continue

            amt_val = _parse_num(amount_words[0]["text"])
            if amt_val is None:
                continue

            desc_text = " ".join(
                w["text"] for w in desc_words
                if not re.match(r"^\d{10,}$", w["text"])
            ).strip()

            # "CR" prefix/suffix near the amount = credit card payment (reduces balance owed)
            is_payment = bool(cr_words) or bool(re.match(r"^CR\b", desc_text, re.I))
            if is_payment:
                desc_text = re.sub(r"^CR\s+", "", desc_text, flags=re.I).strip()
                signed_amt = abs(amt_val)   # positive = payment received
            else:
                signed_amt = -abs(amt_val)  # negative = purchase

            transactions.append({
                "transaction_id": "",
                "date":          found_date,
                "description":   desc_text,
                "amount":        round(signed_amt, 2),
                "balance":       None,
                "source_page":   page_num,
                "row_top":       visible[0]["top"],
                "confidence":    1.0,
            })

    return transactions, None


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT C — NAB Internet Banking "Transaction History" export (loan accounts)
# ══════════════════════════════════════════════════════════════════════════════
#
# Produced by NAB Internet Banking's "Transaction history" / "Search results"
# screen, commonly used for loan accounts. Reverse chronological (newest first).
#
# Each transaction is a vertically-stacked block of 3-4 visual rows:
#   Row 1 (+ optional Row 2): free-form description text, e.g. "TRSF RQST
#     9,555.21 BY MR" / "AMANDEEP"
#   Row N: "DD Mon YYYY   ±$amount   ±$balance*"  (the only row with a date)
#   Row N+1: a short category label, e.g. "Credit Adjustment", "Interest Charged"
#
# Amounts use a Unicode minus "−" (not ASCII "-") and are prefixed with +/- and $.
# When a debit amount is very wide (e.g. a large loan drawdown), the PDF wraps
# the number across two rows: the sign appears alone on the date row's row
# (e.g. "−"), and the rest of the digits appear on the FOLLOWING category-label
# row. In that case we fall back to deriving the amount from the balance delta
# in the 2nd-pass verification step (balance is still captured correctly).

_TXNHIST_AMT_RE = re.compile(r"^[+\-−]\$[\d,]+\.\d{2}\*?$")
_WRAPPED_NUM_RE = re.compile(r"^\$[\d,]+\.\d{2}$")  # bare wrapped continuation, no sign


def _is_txn_history_page(words: list) -> bool:
    texts_lower = {w["text"].lower() for w in words}
    return ("transaction" in texts_lower and "details" in texts_lower
            and "debit" in texts_lower and "credit" in texts_lower
            and "balance*" in texts_lower)


def _parse_amt_token(tok: str) -> Optional[float]:
    """Parse a '+$9,555.21' / '−$2,088,000.00*' style token."""
    if not tok:
        return None
    s = tok.strip().rstrip("*")
    neg = s.startswith("−") or s.startswith("-")
    s = s.lstrip("+-−").lstrip("$").replace(",", "")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _parse_nab_txn_history(pages: list, start_year: int) -> tuple:
    transactions = []
    year_state = [start_year, 0]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not _is_txn_history_page(words):
            continue

        rows = _group_rows(words)

        pending_descs = []
        pending_wrapped_sign = None   # holds a lone "+"/"−" seen on the date row
        just_emitted = False          # True for exactly one row right after a date row —
                                       # that row is ALWAYS the category label, regardless
                                       # of its text content (avoids regex collisions like
                                       # "INTEREST CHARGED" desc vs "Interest Charged" label)

        for row in rows:
            visible = [w for w in row if w["top"] < 790]
            if not visible:
                continue
            full_text = " ".join(w["text"] for w in visible)

            # Skip page furniture / search-criteria header block
            if re.search(r"(Internet\s+Banking|Logout|^Transaction\s+histor|"
                         r"Account:|Date\s+from:|Date\s+to:|Search\s+details:|"
                         r"Transaction\s+type:|Amount\s+from:|Amount\s+to:|"
                         r"Credit\s+balance:|Debit\s+balance:|Net\s+position:|"
                         r"Balances\s+shown|completed\s+and\s+may|End\s+of\s+report|"
                         r"National\s+Australia\s+Bank|©\s*National)",
                         full_text, re.I):
                continue
            if re.match(r"^Date\s+Transaction\s+Details\s+Debit\s+Credit\s+Balance", full_text, re.I):
                continue

            # A genuine date row has day+month+year as the first 3 tokens
            found_date = None
            if len(visible) >= 3:
                t0, t1, t2 = visible[0]["text"], visible[1]["text"], visible[2]["text"]
                if (re.match(r"^\d{1,2}$", t0)
                        and re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", t1, re.I)
                        and re.match(r"^\d{4}$", t2)):
                    found_date = _parse_date_from_words(visible[:3], year_state)

            if found_date:
                # Remaining tokens after the date may include amount(s) AND,
                # occasionally, a stray description word that visually merged
                # into this row (e.g. "AMANDEEP" sits 1.5px above the date
                # baseline and falls inside the row-grouping y-tolerance).
                # Separate them: amount-like tokens vs everything else.
                amt_tokens = []
                stray_desc_words = []
                for w in visible[3:]:
                    tok = w["text"]
                    if tok in ("+", "−", "-") or _TXNHIST_AMT_RE.match(tok) or _WRAPPED_NUM_RE.match(tok):
                        amt_tokens.append(tok)
                    else:
                        stray_desc_words.append(tok)

                parsed_amts = []
                lone_sign = None
                for tok in amt_tokens:
                    if tok in ("+", "−", "-"):
                        lone_sign = tok
                        continue
                    if _TXNHIST_AMT_RE.match(tok):
                        parsed_amts.append(_parse_amt_token(tok))
                    elif _WRAPPED_NUM_RE.match(tok):
                        v = _parse_amt_token(tok)
                        if lone_sign in ("−", "-"):
                            v = -abs(v)
                        parsed_amts.append(v)

                if lone_sign and not parsed_amts:
                    # Amount wraps entirely onto the NEXT row
                    pending_wrapped_sign = lone_sign

                amount_val  = parsed_amts[0] if len(parsed_amts) >= 1 else None
                balance_val = parsed_amts[1] if len(parsed_amts) >= 2 else None

                if amount_val is not None and balance_val is None and len(parsed_amts) == 1:
                    # Single captured value with wrapped amount pending is actually
                    # the BALANCE (always the last token, marked with trailing '*')
                    if amt_tokens and amt_tokens[-1].rstrip().endswith("*"):
                        balance_val = amount_val
                        amount_val = None

                if stray_desc_words:
                    pending_descs.append(" ".join(stray_desc_words))

                desc_text = " ".join(pending_descs).strip()
                transactions.append({
                    "transaction_id": "",
                    "date":          found_date,
                    "description":   desc_text,
                    "amount":        round(amount_val, 2) if amount_val is not None else 0.0,
                    "balance":       round(balance_val, 2) if balance_val is not None else None,
                    "source_page":   page_num,
                    "row_top":       visible[0]["top"],
                    "confidence":    1.0 if amount_val is not None else 0.5,
                })
                pending_descs = []
                just_emitted = True
                continue

            # Non-date row: wrapped-amount continuation takes priority.
            # This row may ALSO contain the category label text alongside the
            # wrapped number (e.g. "Miscellaneous Debit $2,087,639.00") — only
            # the numeric token belongs to the amount; the rest is the category.
            if pending_wrapped_sign is not None:
                wrapped_tok = next((w["text"] for w in visible if _WRAPPED_NUM_RE.match(w["text"])), None)
                if wrapped_tok and transactions:
                    v = _parse_amt_token(wrapped_tok)
                    if pending_wrapped_sign in ("−", "-"):
                        v = -abs(v)
                    transactions[-1]["amount"] = round(v, 2)
                    transactions[-1]["confidence"] = 1.0
                    # Attach the non-numeric remainder of this row as the category
                    label_words = [w["text"] for w in visible if not _WRAPPED_NUM_RE.match(w["text"])]
                    if label_words:
                        transactions[-1]["description"] = (
                            transactions[-1]["description"] + " | " + " ".join(label_words)
                        ).strip(" |")
                pending_wrapped_sign = None
                just_emitted = False  # category already attached above
                continue

            # Positional rule: the row immediately following a date row is
            # ALWAYS the category label — regardless of text content — never
            # a description for the next transaction.
            if just_emitted:
                if transactions:
                    transactions[-1]["description"] = (
                        transactions[-1]["description"] + " | " + full_text
                    ).strip(" |")
                just_emitted = False
                continue

            # Otherwise: plain description line for the UPCOMING transaction.
            # A lone trailing "+"/"−" sign (e.g. "LOAN DRAWDOWN −") is the
            # sign of an amount whose digits are wrapped onto a LATER row —
            # strip it from the description and remember it for that wrap.
            desc_words_list = full_text.split()
            if desc_words_list and desc_words_list[-1] in ("+", "−", "-"):
                pending_wrapped_sign = desc_words_list[-1]
                full_text = " ".join(desc_words_list[:-1])
            pending_descs.append(full_text)

    return transactions, None


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT D — NAB Credit Card (portrait, simple Amount A$ column)
# ══════════════════════════════════════════════════════════════════════════════
#
# A simpler, portrait-orientation NAB credit card statement (as opposed to the
# landscape NAB Qantas Business Signature card in LAYOUT B).
# Header: "Date processed | Date of transaction | Card No | Details | Amount A$"
# Row: DD/MM/YY  DD/MM/YY  V####  DESCRIPTION...  XX.XX [CR]
# No dollar sign on amounts; "CR" suffix marks a payment/credit (reduces balance).

_PCC_DATE1_X_MAX = 100   # "Date processed" column
_PCC_DATE2_X_MIN = 100   # "Date of transaction" column
_PCC_DATE2_X_MAX = 200
_PCC_CARD_X_MIN  = 200   # "Card No" column (e.g. V3977)
_PCC_CARD_X_MAX  = 245
_PCC_DESC_X_MIN  = 245   # "Details" column
_PCC_AMT_X_MIN   = 480   # "Amount A$" column (right-aligned)


def _is_portrait_cc_page(words: list) -> bool:
    texts_lower = {w["text"].lower() for w in words}
    return ("processed" in texts_lower and "transaction" in texts_lower
            and "card" in texts_lower and "details" in texts_lower)


def _parse_nab_portrait_credit_card(pages: list, start_year: int) -> tuple:
    transactions = []
    year_state = [start_year, 0]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not words:
            continue

        rows = _group_rows(words)

        for row in rows:
            visible = [w for w in row if w["top"] < 800]
            if not visible:
                continue
            full_text = " ".join(w["text"] for w in visible)

            if _SKIP_ROW_RE.search(full_text):
                continue
            if re.match(r"^Date\s+processed", full_text, re.I):
                continue
            if re.search(r"(Date\s+of|Card\s+No|Details\s+Amount|How\s+to\s+identify|"
                         r"Unauthorised\s+or\s+unknown|Your\s+balance\s+and\s+interest|"
                         r"transaction\s+type\s+annual|For\s+more\s+information|"
                         r"DIRECT\s+DEBIT|DID\s+YOU\s+KNOW|NAB\s+DEFENCE|"
                         r"IF\s+YOU\s+BECOME)", full_text, re.I):
                continue

            date_words = [w for w in visible if w["x0"] < _PCC_DATE2_X_MAX
                          and re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", w["text"])]
            if not date_words:
                continue

            # First date = date processed (we use this); skip if not present
            proc_date_word = next((w for w in visible
                                    if w["x0"] < _PCC_DATE1_X_MAX
                                    and re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", w["text"])), None)
            if not proc_date_word:
                continue

            found_date = _parse_date_from_words([proc_date_word], year_state)
            # _parse_date_from_words expects "DD Mon" style; build manually for slash dates
            m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", proc_date_word["text"])
            if m:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:
                    y += 2000
                try:
                    found_date = datetime(y, mo, d).strftime("%d-%b-%Y")
                except ValueError:
                    found_date = None
            if not found_date:
                continue

            amt_words = [w for w in visible if w["x0"] >= _PCC_AMT_X_MIN - 40
                         and re.match(r"^[\d,]+\.\d{2}$", w["text"])]
            if not amt_words:
                continue
            amt_val = _parse_num(amt_words[0]["text"])
            if amt_val is None:
                continue

            cr_word = next((w for w in visible
                             if w["text"].upper() == "CR"
                             and w["x0"] > amt_words[0]["x0"]), None)
            is_payment = cr_word is not None

            desc_words = [w for w in visible
                          if w["x0"] >= _PCC_DESC_X_MIN
                          and w not in amt_words
                          and w["text"].upper() != "CR"
                          and w["x0"] < (amt_words[0]["x0"] if amt_words else 9999)]
            desc_text = " ".join(w["text"] for w in desc_words).strip()

            signed_amt = abs(amt_val) if is_payment else -abs(amt_val)

            transactions.append({
                "transaction_id": "",
                "date":          found_date,
                "description":   desc_text,
                "amount":        round(signed_amt, 2),
                "balance":       None,
                "source_page":   page_num,
                "row_top":       visible[0]["top"],
                "confidence":    1.0,
            })

    return transactions, None


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT DETECTION & ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def _detect_layout(pages: list) -> str:
    for page in pages[:3]:
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if _is_txn_history_page(words):
            return "TXN_HISTORY"
        if _is_portrait_cc_page(words):
            return "CREDIT_CARD_PORTRAIT"
        if page.width > page.height:
            return "CREDIT_CARD"
        if _is_nab_business_page(words):
            return "BUSINESS"
    return "BUSINESS"

def _extract_start_year(text: str) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else datetime.today().year

DISPLAY_NAME = "NAB"

def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    score = 0.0
    if "nab" in txt or "national australia bank" in txt:
        score += 0.5
    if "particulars" in txt:
        score += 0.2
    if "debits" in txt or "credits" in txt:
        score += 0.1
    if "qantas" in txt or "commercial cards" in txt:
        score += 0.2
    if "transaction history" in txt:
        score += 0.3
    if "date processed" in txt:
        score += 0.3
    return min(score, 1.0)

def parse(pdf_path: str) -> dict:
    t0 = time.time()

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        page1_text = pdf.pages[0].extract_text() or ""
        start_year = _extract_start_year(page1_text)
        layout     = _detect_layout(pdf.pages)

        if layout == "TXN_HISTORY":
            txns, opening_bal = _parse_nab_txn_history(pdf.pages, start_year)
        elif layout == "CREDIT_CARD_PORTRAIT":
            txns, opening_bal = _parse_nab_portrait_credit_card(pdf.pages, start_year)
        elif layout == "CREDIT_CARD":
            txns, opening_bal = _parse_nab_credit_card(pdf.pages, start_year)
        else:
            txns, opening_bal = _parse_nab_business(pdf.pages, start_year)

    # ── 2nd-pass: Balance-delta sign verification ─────────────────────────────
    if txns and layout in ("BUSINESS", "TXN_HISTORY"):
        is_reverse = False
        valid_dates = []
        for t in txns:
            if t["date"]:
                try:
                    valid_dates.append(datetime.strptime(t["date"], "%d-%b-%Y"))
                except ValueError:
                    pass
        if len(valid_dates) >= 2 and valid_dates[0] > valid_dates[-1]:
            is_reverse = True

        for i in range(len(txns)):
            curr_bal = txns[i]["balance"]
            if curr_bal is None:
                continue
            prev_bal = None
            if is_reverse:
                for j in range(i + 1, len(txns)):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]; break
            else:
                for j in range(i - 1, -1, -1):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]; break
            if prev_bal is None and opening_bal is not None:
                prev_bal = opening_bal
            if prev_bal is not None:
                delta = curr_bal - prev_bal
                # For TXN_HISTORY, also accept recovering a missing (0.0 / low-confidence) amount
                if txns[i]["amount"] == 0.0 or abs(abs(delta) - abs(txns[i]["amount"])) <= 0.05:
                    txns[i]["amount"] = round(delta, 2)

    txns.sort(key=lambda t: (t.get("date") or "", t.get("source_page", 0), t.get("row_top", 0)))
    for i, t in enumerate(txns):
        t["transaction_id"] = f"nab_{i+1:04d}"

    return {
        "transactions": txns,
        "ambiguous":    [],
        "meta": {
            "bank":          "NAB",
            "bank_id":       "nab",
            "layout":        layout,
            "pages":         page_count,
            "parse_time_ms": round((time.time() - t0) * 1000),
        },
    }