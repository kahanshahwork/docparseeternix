import re
import time
import pdfplumber
from datetime import datetime
from typing import Optional


# ── Patterns ─────────────────────────────────────────────────────────────────

_DATE_SHORT_RE = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?$", re.I)
_DATE_FULL_RE  = re.compile(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{4})$", re.I)

# KEY FIX: amount tokens MUST have a decimal point OR a $ sign.
# Pure integers (reference numbers like 657224, 989804) are NOT amounts.
_PLAIN_NUM_RE  = re.compile(r"^[+\-−]?[\d,]+\.\d{2}$")          # e.g. 1,234.56  -1,234.56
_DOLLAR_RE     = re.compile(r"^\$[\d,]+(?:\.\d{2})?$")           # e.g. $1,234.56  $1,234
_SIGNED_RE     = re.compile(r"^[+\-−]\$[\d,]+(?:\.\d{2})?$")    # e.g. -$1,234.56

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

# Words that ANZ prints literally as placeholders for empty debit/credit cells
_BLANK_TOKENS = {"blank"}

_SKIP_RE = re.compile(
    r"^(OPENING\s+BALANCE|CLOSING\s+BALANCE|TOTALS\s+AT\s+END|"
    r"Please\s+retain\s+this|This\s+Statement\s+Includes|Interest\s+earned|"
    r"PLEASE\s+CHECK\s+THE\s+ENTRIES|All\s+entries\s+generated|"
    r"Further\s+information\s+in\s+relation|If\s+you\s+have\s+a\s+complaint|"
    r"customer\s+complaints\s+guide|General\s+enquiries|Write\s+ANZ|"
    r"If\s+an\s+issue\s+has\s+not|AFCA\s+provides|Call:|Online:|Web:|to:|"
    r"Australia\s+and\s+New\s+Zealand|Credit\s+Licence|Page\s+\d+\s+of|"
    r"Date\s+Transaction\s+Details|Description\s+Date|Summary\s+of\s+ANZ|"
    r"MONTHLY\s+ACCOUNT|Total\s+Account|Please\s+note:|Fee\s+Summary|"
    r"Fees\s+Charged|Summary\s+of\s+ANZ\s+Transaction|Service\s+Fees|"
    r"Total\s+Account\s+Service|Total\s+Bank\s+Account)",
    re.I,
)


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
    """
    Returns True only if the token looks like a monetary amount.
    Requires a decimal point (XX.XX) OR a dollar sign ($XX).
    This deliberately excludes bare integers like 657224 (reference numbers).
    """
    if s.lower() in _BLANK_TOKENS:
        return False
    return bool(_PLAIN_NUM_RE.match(s) or _DOLLAR_RE.match(s) or _SIGNED_RE.match(s))

def _extract_num_from_col(s: str) -> str:
    """Extract a valid monetary amount from a column string. Returns '' if not a real amount."""
    s = s.strip()
    if not s or s.lower() in _BLANK_TOKENS:
        return ""
    # Must contain a decimal point to be a monetary amount
    if _PLAIN_NUM_RE.match(s.replace(",", "")) or _DOLLAR_RE.match(s):
        return s
    # Fallback: search for an amount, but require it ends at a word boundary
    # (prevents matching '6.57' inside '6.57%' which is an interest rate, not an amount)
    m = re.search(r"[+\-−]?\$?[\d,]+\.\d{2}(?![%\d])", s)
    return m.group(0) if m else ""


def _negate_dr_balance(bal_str: str) -> Optional[float]:
    """
    Parse a balance that may have a DR suffix (loan/mortgage accounts).
    DR means the account is in debit — i.e. the customer owes this amount.
    We negate it so balance-delta sign verification works correctly:
      prev=-329926.50, curr=-331767.49 → delta=-1840.99 → debit (interest) ✓
    Returns the signed float, or None if unparseable.
    """
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
    mon_n = _MONTH_MAP.get(mon.lower()[:3], 1)
    try:
        return datetime(year, mon_n, int(day)).strftime("%d-%b-%Y")
    except ValueError:
        return f"{day}-{mon[:3].capitalize()}-{year}"

def _parse_date_token(s: str, year_state: list, is_reverse: bool = False) -> Optional[str]:
    s = s.strip()

    # Standalone Year
    if re.match(r"^\d{4}$", s):
        year_state[0] = int(s)
        return None

    # Standalone Month name (e.g. "July 2024" row header in loan statements)
    m_my = re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$", s, re.I)
    if m_my:
        year_state[0] = int(m_my.group(2))
        year_state[1] = _MONTH_MAP[m_my.group(1)[:3].lower()]
        return None

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
        if year_state[1]:
            if not is_reverse:
                # Forward: year increments when month wraps Dec → Jan
                if mon_n < 3 and year_state[1] > 10:
                    year_state[0] += 1
            else:
                # Reverse: year decrements when month wraps Jan → Dec
                if mon_n > 10 and year_state[1] < 3:
                    year_state[0] -= 1
        year_state[1] = mon_n
        return _make_date(day, mon, year_state[0])

    return None


# ── Column detection ──────────────────────────────────────────────────────────

def _find_header_row(words: list) -> Optional[list]:
    if not words:
        return None

    sorted_words = sorted(words, key=lambda w: w["top"])
    bands = []
    cur = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur[0]["top"]) <= 8:
            cur.append(w)
        else:
            bands.append(cur); cur = [w]
    if cur: bands.append(cur)

    HEADER_WORDS = {"date", "transaction", "details", "description", "debit", "credit",
                    "debits", "credits", "withdrawals", "deposits", "balance"}
    NEED_DATE = {"date", "description"}
    NEED_ANY  = {"debit", "credit", "debits", "credits", "withdrawals", "deposits", "balance"}

    for i, band in enumerate(bands):
        for candidate in ([band + bands[i+1]] if i+1 < len(bands) and abs(bands[i+1][0]["top"] - band[0]["top"]) <= 18 else []) + [band]:
            words_lower = [w["text"].lower() for w in candidate]
            word_set = set()
            for wl in words_lower:
                # Strip parenthetical units like ($) or ($AUD)
                clean = re.sub(r"\(.*?\)", "", wl).strip()
                word_set.add(clean)

            if not (word_set & NEED_DATE and word_set & NEED_ANY):
                continue

            kw_count = sum(1 for w in word_set if w in HEADER_WORDS)
            if kw_count < 3:
                continue

            # Reject rows that contain actual amount values (not header rows)
            amount_tokens = [w for w in candidate if re.match(r"^\$[\d,]+\.\d{2}", w["text"])]
            if len(amount_tokens) > 1:
                continue

            return sorted(candidate, key=lambda x: x["x0"])
    return None

def _compute_col_bounds(header: list) -> dict:
    COL_MAP = {
        "date": "date",
        "transaction": "desc",
        "details": "desc",
        "description": "desc",
        "debit": "debit",
        "debits": "debit",      # e.g. ANZ Loan Statement header
        "withdrawals": "debit",
        "debit($aud)": "debit",
        "credit": "credit",
        "credits": "credit",    # e.g. ANZ Loan Statement header
        "deposits": "credit",
        "credit($aud)": "credit",
        "balance": "balance",
    }

    matched = []
    for w in header:
        # Normalize: lowercase, collapse internal spaces, strip parens
        raw = w["text"].lower().strip()
        # Match "Debit($AUD)" or "Withdrawals ($)" etc. by stripping parens and their contents
        clean_key = re.sub(r"\s*\(.*?\)", "", raw).strip()
        # Also try the full lowercased version (e.g. "debit($aud)")
        full_key = re.sub(r"\s+", "", raw)  # remove spaces: "debit($aud)"
        col = COL_MAP.get(clean_key) or COL_MAP.get(full_key)
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
        lo = 0.0 if i == 0 else (deduped[i-1][1] + x0) / 2
        hi = 9999.0 if i == len(deduped)-1 else (x0 + deduped[i+1][1]) / 2
        bounds[name] = (lo, hi)
    return bounds

def _classify_words(row: list, bounds: dict) -> dict:
    AMT_COLS = {"debit", "credit", "balance"}
    result = {k: [] for k in bounds}

    for w in row:
        txt = w["text"]

        # Skip literal placeholder words ANZ uses for empty fields
        if txt.lower() in _BLANK_TOKENS:
            continue

        x0, x1 = w["x0"], w.get("x1", w["x0"] + 40)
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


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT A — Standard 5-Col (Business Advantage, Plus, Save, Everyday)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_standard_5col(pages: list, start_year: int, is_reverse: bool) -> list:
    transactions = []
    year_state   = [start_year, 0]
    cached_bounds = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            cached_bounds[0] = _compute_col_bounds(header)

        bounds = cached_bounds[0]
        if not bounds:
            continue

        rows = _group_rows(words)
        pending_date, pending_descs, pending_top = None, [], 0.0

        def emit(deb_str, cred_str, bal_str, top):
            nonlocal pending_date, pending_descs
            if not pending_date:
                return

            # Use DR-aware balance parsing: loan accounts show balance as "329,926.50 DR"
            # We negate DR balances so sign_from_balance_delta works correctly:
            #   prev=-329926.50, curr=-331767.49 → delta=-1840.99 → debit ✓
            bal_val = _negate_dr_balance(bal_str) if bal_str else None
            dv = _parse_num(deb_str) if deb_str else None
            cv = _parse_num(cred_str) if cred_str else None

            if dv is None and cv is None:
                pending_date = None
                pending_descs = []
                return

            raw = (cv or 0.0) - (dv or 0.0)

            transactions.append({
                "transaction_id": "",
                "date": pending_date,
                "description": " ".join(pending_descs).strip(),
                "amount": round(raw, 2),
                "balance": bal_val,
                "source_page": page_num,
                "row_top": top,
                "confidence": 1.0,
            })
            pending_date, pending_descs = None, []

        for row in rows:
            clean_row = [w for w in row if w["x0"] >= 30]
            if not clean_row:
                continue

            cols = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = _extract_num_from_col(cols.get("debit", ""))
            cred_str = _extract_num_from_col(cols.get("credit", ""))
            # Pass raw balance string so emit() can detect DR suffix for loan accounts
            bal_str  = cols.get("balance", "").strip()

            full_text = " ".join(w["text"] for w in clean_row)
            if _SKIP_RE.match(full_text) or _SKIP_RE.match(desc_str):
                continue

            # Skip account info rows (BSB + account number patterns)
            if re.match(r"^\d{3}\s+\d{3}\s+\d{3}\s+\d{3}", full_text):
                continue

            parsed_date = _parse_date_token(date_str, year_state, is_reverse) if date_str else None
            is_date_row = bool(parsed_date)

            if is_date_row:
                emit("", "", "", 0.0)  # flush previous
                pending_date = parsed_date
                pending_top  = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []
                if deb_str or cred_str or bal_str:
                    emit(deb_str, cred_str, bal_str, pending_top)
            elif (deb_str or cred_str) and pending_date:
                if desc_str:
                    pending_descs.append(desc_str)
                emit(deb_str, cred_str, bal_str, pending_top)
            elif pending_date and desc_str:
                if not re.match(r"^(EFFECTIVE\s+DATE|Card\s+xx)", desc_str, re.I):
                    pending_descs.append(desc_str)

        emit("", "", "", 0.0)
    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT B — Loan / Search-Export 3-Col (Description/Date | Debit | Credit)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_loan_3col(pages: list, start_year: int) -> list:
    """
    Handles the ANZ Search-Export / Loan layout:
      Date col  | Description col | Debit($AUD) col | Credit($AUD) col
    The date column contains either:
      - "DD MON"       → transaction date
      - "Month YYYY"   → month-header row (reverse chron, skip)
      - "Month"        → partial month (year is in desc col)
    """
    transactions = []
    year_state = [start_year, 0]
    cached_bounds = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            cached_bounds[0] = _compute_col_bounds(header)

        bounds = cached_bounds[0]
        if not bounds:
            continue

        rows = _group_rows(words)

        cur_desc, cur_deb, cur_cred, cur_date, cur_top = [], None, None, None, 0.0

        def emit():
            nonlocal cur_desc, cur_deb, cur_cred, cur_date
            if cur_date and (cur_deb is not None or cur_cred is not None):
                raw = (cur_cred or 0.0) - (cur_deb or 0.0)
                transactions.append({
                    "transaction_id": "",
                    "date": cur_date,
                    "description": " ".join(cur_desc).strip(),
                    "amount": round(raw, 2),
                    "balance": None,
                    "source_page": page_num,
                    "row_top": cur_top,
                    "confidence": 1.0,
                })
            cur_desc, cur_deb, cur_cred, cur_date = [], None, None, None

        for row in rows:
            clean_row = [w for w in row if w["x0"] >= 30]
            if not clean_row:
                continue

            cols = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()
            deb_str  = _extract_num_from_col(cols.get("debit", ""))
            cred_str = _extract_num_from_col(cols.get("credit", ""))

            if _SKIP_RE.match(desc_str) or _SKIP_RE.match(date_str):
                continue

            # Case 1: "July 2024" fully in date col → month header, update state
            m_my = re.match(
                r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$",
                date_str, re.I,
            )
            if m_my:
                year_state[0] = int(m_my.group(2))
                year_state[1] = _MONTH_MAP[m_my.group(1)[:3].lower()]
                continue

            # Case 2: "March" in date col and "2024" in desc col → month header split across cols
            if re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$", date_str, re.I) and re.match(r"^\d{4}$", desc_str):
                year_state[0] = int(desc_str)
                year_state[1] = _MONTH_MAP[date_str[:3].lower()]
                continue

            # Case 3: "05 JUL" in date col → transaction row
            found_date = None
            if date_str:
                # Try date col first
                found_date = _parse_date_token(date_str, year_state)
                if not found_date:
                    # Fallback: look for date embedded in desc col (older format)
                    m_date = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3})\b", desc_str)
                    if m_date:
                        found_date = _parse_date_token(m_date.group(1), year_state)
                        desc_str = desc_str.replace(m_date.group(1), "").strip()

            if found_date:
                if cur_deb is not None or cur_cred is not None:
                    emit()
                cur_date = found_date
                cur_top = row[0]["top"]
                cur_desc = [desc_str] if desc_str else []
            elif desc_str and cur_date:
                cur_desc.append(desc_str)

            if deb_str or cred_str:
                if cur_deb is not None or cur_cred is not None:
                    emit()
                cur_deb = _parse_num(deb_str) if deb_str else None
                cur_cred = _parse_num(cred_str) if cred_str else None
                if cur_date:
                    emit()

        emit()

    return transactions


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT DETECTION & ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def _detect_layout(pdf_path: str) -> str:
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

            if "balance" in cols:
                return "STANDARD"
            if "description" in cols and ("debit" in cols or "credit" in cols or "debit($aud)" in cols or "credit($aud)" in cols):
                return "LOAN"

    return "STANDARD"

def _extract_start_year(text: str) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else datetime.today().year

def _extract_opening_balance(text: str) -> Optional[float]:
    patterns = [
        r"Opening\s+Balance:?\s+\$?([\d,]+\.\d{2})",
        r"OPENING\s+BALANCE\s+\$?([\d,]+\.\d{2})",
        r"Opening\s+Balance[^\$]{0,80}?\$([\d,]+\.\d{2})",
        r"\$?([\d,]+\.\d{2})\s*Opening\s+Balance",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.DOTALL)
        if m:
            return float(m.group(1).replace(",", ""))
    return None

# ─────────────────────────────────────────────────────────────────────────────

DISPLAY_NAME = "ANZ"

def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    score = 0.0
    if "anz" in txt:
        score += 0.5
    if "business advantage" in txt or "anz plus" in txt or "anz save" in txt or "everyday" in txt:
        score += 0.3
    if "residential investment property loan" in txt or "select option" in txt:
        score += 0.3
    if re.search(r"withdrawals\s*\(\$\)\s*deposits\s*\(\$\)", txt):
        score += 0.2
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

        # Detect reverse chronological (ANZ Plus/Save/Everyday export format)
        is_reverse_guess = any(x in page1_text.lower() for x in ["anz plus", "anz save", "everyday", "search results"])

        if layout == "LOAN":
            txns = _parse_loan_3col(pages, start_year)
        else:
            txns = _parse_standard_5col(pages, start_year, is_reverse_guess)

    # ── 2nd-pass: Balance-delta sign verification ─────────────────────────────
    if layout != "LOAN" and txns:
        # Determine true chronological direction from parsed dates
        is_reverse_true = is_reverse_guess
        valid_dates = []
        for t in txns:
            if t["date"]:
                try:
                    valid_dates.append(datetime.strptime(t["date"], "%d-%b-%Y"))
                except ValueError:
                    pass
        if len(valid_dates) >= 2:
            if valid_dates[0] > valid_dates[-1]:
                is_reverse_true = True
            elif valid_dates[0] < valid_dates[-1]:
                is_reverse_true = False

        for i in range(len(txns)):
            curr_bal = txns[i]["balance"]
            if curr_bal is None:
                continue

            prev_bal = None
            if is_reverse_true:
                for j in range(i + 1, len(txns)):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]
                        break
            else:
                for j in range(i - 1, -1, -1):
                    if txns[j]["balance"] is not None:
                        prev_bal = txns[j]["balance"]
                        break

            if prev_bal is None and opening_bal is not None:
                prev_bal = opening_bal

            if prev_bal is not None:
                delta = curr_bal - prev_bal
                if abs(abs(delta) - abs(txns[i]["amount"])) <= 0.05:
                    txns[i]["amount"] = round(delta, 2)

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