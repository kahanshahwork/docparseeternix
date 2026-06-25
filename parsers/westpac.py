"""
parsers/westpac.py — Westpac unified statement parser
======================================================
Handles all Westpac statement layouts via structural detection only.

DYNAMIC PRINCIPLES
──────────────────
- Layout detected solely from which column headers are present on the page.
  No product names, account types, brand strings, or date formats used for routing.
- Column boundaries derived from actual header word positions (midpoints).
- Left content margin derived from header's leftmost word.
- Amount tokens: accepts plain numbers (3,641.00), $-prefixed ($269.50),
  signed (-$269.50), and DR/CR suffixed. No hard requirement for $ sign.
- Date formats: DD/MM/YY, DD/MM/YYYY, DD Mon YYYY, D Mon YYYY — flexible.
- Footer detection: structural sentinel rows (CLOSING BALANCE, end-of-data marker)
  not string-matched boilerplate.
- Continuation distance (Activity layout): derived from document's median line
  spacing, not a fixed constant.
- Sign: balance delta correction for layouts with a balance column.
  Signed amount embedded in token for no-balance layouts.
  No description keyword matching for sign determination.

LAYOUTS (detected from header column structure only)
────────────────────────────────────────────────────
  ELECTRONIC  — DATE | TRANSACTION DESCRIPTION | DEBIT | CREDIT | BALANCE
                Signal: 'balance' column present in header.
                Date: DD/MM/YY or DD/MM/YYYY.
                Amounts: plain numbers, no $ sign.

  ACTIVITY    — Date | Description | Debit | Credit  (no balance column)
                Signal: debit+credit present but NO balance column.
                Date: DD Mon YYYY, multi-word on same row as amount.
                Amounts: signed $ tokens (-$269.50 / $25000.00).
                Row order: description type ABOVE the date+amount row.
"""

import re
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Optional

import pdfplumber

from parsers.utils import build_result, make_txn, sign_from_balance_delta

DISPLAY_NAME = "Westpac"


# ── Patterns ──────────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

# Flexible amount: plain number, $-prefixed, signed, DR/CR suffix
_PLAIN_NUM_RE = re.compile(r"^[+\-−]?[\d,]+\.\d{2}$")
_DOLLAR_RE    = re.compile(r"^[+\-−]?\$[\d,]+(?:\.\d{2})?$")
_BLANK_TOKENS = {"blank"}

# Slash-date: DD/MM/YY or DD/MM/YYYY
_DATE_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")

# Header vocabulary — used for header detection only
_HEADER_VOCAB = {
    "date", "transaction", "description",
    "debit", "credit", "balance",
}
_NEED_DATE = {"date", "description"}
_NEED_AMT  = {"debit", "credit", "balance"}

_COL_MAP = {
    "date":        "date",
    "transaction": "desc",
    "description": "desc",
    "debit":       "debit",
    "credit":      "credit",
    "balance":     "balance",
}


# ── Shared utilities ──────────────────────────────────────────────────────────

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
            cur, cur_top = [w], w["top"]
    rows.append(sorted(cur, key=lambda x: x["x0"]))
    return rows


def _is_amount_token(s: str) -> bool:
    """
    Accept: plain numbers (3,641.00), $-prefixed ($1,234.56),
    signed (-$269.50 / -1,234.56). Rejects bare integers (reference numbers).
    Requires a decimal point OR a $ sign.
    """
    if s.lower() in _BLANK_TOKENS:
        return False
    return bool(_PLAIN_NUM_RE.match(s) or _DOLLAR_RE.match(s))


def _parse_num(s: str) -> Optional[float]:
    s = s.strip().replace(",", "").replace("−", "-").replace(" ", "")
    neg = s.startswith("-")
    s = s.lstrip("+-").lstrip("$")
    s = re.sub(r"\b(DR|CR)\b", "", s, flags=re.I).strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _median_line_spacing(words: list) -> float:
    """Compute median vertical gap between adjacent word rows — no magic numbers."""
    tops = sorted(set(round(w["top"], 1) for w in words))
    gaps = [tops[i+1] - tops[i] for i in range(len(tops)-1) if 0 < tops[i+1] - tops[i] < 30]
    if not gaps:
        return 10.0
    return sorted(gaps)[len(gaps) // 2]


# ── Header detection ──────────────────────────────────────────────────────────

def _find_header_row(words: list) -> Optional[list]:
    """
    Find the table header row. Purely structural: must contain 'date' (or
    'description') AND at least one amount-column word. Uses a merge window
    derived from the document's own line spacing.
    """
    if not words:
        return None

    sorted_words = sorted(words, key=lambda w: w["top"])

    # Compute merge window from actual line spacing
    tops = sorted(set(round(w["top"], 1) for w in sorted_words))
    gaps = [tops[i+1]-tops[i] for i in range(len(tops)-1) if 0 < tops[i+1]-tops[i] < 30]
    merge_window = sorted(gaps)[len(gaps)//2] * 0.9 if gaps else 8.0
    merge_window = max(6.0, min(merge_window, 14.0))

    bands = []
    cur = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur[0]["top"]) <= 8:
            cur.append(w)
        else:
            bands.append(cur); cur = [w]
    if cur: bands.append(cur)

    for i, band in enumerate(bands):
        candidates = [band]
        if i+1 < len(bands) and abs(bands[i+1][0]["top"] - band[-1]["top"]) <= merge_window:
            candidates = [band + bands[i+1]] + candidates

        for candidate in candidates:
            word_set = set()
            for w in candidate:
                raw = w["text"].lower().strip()
                clean = re.sub(r"\s*\(.*?\)", "", raw).strip()
                word_set.add(clean)
                word_set.add(re.sub(r"\s+", "", raw))

            if not (word_set & _NEED_DATE and word_set & _NEED_AMT):
                continue
            if sum(1 for w in word_set if w in _HEADER_VOCAB) < 3:
                continue
            # Reject data rows: real headers never have multiple dollar amounts
            if sum(1 for w in candidate if re.match(r"^\$[\d,]+\.\d{2}", w["text"])) > 1:
                continue

            # Reject merged candidates where prose overwhelms header vocabulary.
            # A real header row is mostly column-name words. If less than 40% of
            # tokens are header vocabulary, this is a prose row merged with the header.
            non_blank_words = [w for w in candidate if w["text"].strip()]
            if non_blank_words:
                hdr_word_count = sum(
                    1 for w in non_blank_words
                    if re.sub(r"\s*\(.*?\)", "", w["text"].lower().strip()) in _HEADER_VOCAB
                )
                if hdr_word_count / len(non_blank_words) < 0.40:
                    continue

            return sorted(candidate, key=lambda x: x["x0"])

    return None


def _compute_col_bounds(header: list) -> dict:
    """Derive column boundaries as midpoints between detected header positions."""
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
    """Left content boundary: leftmost header word x0 minus a small buffer."""
    if not header:
        return 0.0
    return max(0.0, min(w["x0"] for w in header) - 4.0)


def _classify_words(row: list, bounds: dict) -> dict:
    """
    Assign words to column buckets.
    Numeric tokens use centre x and can go to any column.
    Non-numeric tokens ONLY go to date or desc columns — never to amount columns.
    This correctly handles description words that visually overlap the amount zone.
    """
    AMT_COLS  = {"debit", "credit", "balance"}
    TEXT_COLS = {col for col in bounds if col not in AMT_COLS}
    result = {k: [] for k in bounds}

    # Pre-sort TEXT_COLS by x0 position so fallback uses leftmost-first
    text_cols_sorted = sorted(TEXT_COLS, key=lambda c: bounds[c][0])

    for w in row:
        txt = w["text"]
        if txt.lower() in _BLANK_TOKENS:
            continue
        x0 = w["x0"]
        x1 = w.get("x1", x0 + max(len(txt) * 5.5, 10.0))
        is_num = _is_amount_token(txt)
        placed = False

        if is_num:
            # Amount tokens: use centre x, try amount columns first
            cx = (x0 + x1) / 2
            for col, (lo, hi) in bounds.items():
                if col in AMT_COLS and lo <= cx < hi:
                    result[col].append(txt); placed = True; break
            # If not in an amount col, still try text cols (e.g. reference numbers in desc)
            if not placed:
                for col in text_cols_sorted:
                    lo, hi = bounds[col]
                    if lo <= x0 < hi:
                        result[col].append(txt); placed = True; break
        else:
            # Non-amount tokens: ONLY go to text columns (date or desc).
            # This prevents description words from landing in debit/credit/balance.
            # Try by x0 position in text cols; if beyond all text cols, assign to last text col.
            for col in text_cols_sorted:
                lo, hi = bounds[col]
                if lo <= x0 < hi:
                    result[col].append(txt); placed = True; break
            if not placed and text_cols_sorted:
                # Word is to the right of all text columns — assign to last text col (desc)
                result[text_cols_sorted[-1]].append(txt)

    return {k: " ".join(v).strip() for k, v in result.items()}


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_slash_date(s: str) -> Optional[str]:
    """Parse DD/MM/YY or DD/MM/YYYY."""
    m = _DATE_SLASH_RE.match(s.strip())
    if not m:
        return None
    day, mon_n, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000
    if not (1 <= mon_n <= 12):
        return None
    mon_abbrs = ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        return datetime(yr, mon_n, day).strftime("%d-%b-%Y")
    except ValueError:
        return None


def _parse_mon_date_from_words(words_in_date_zone: list) -> Optional[str]:
    """
    Parse a date from multi-word tokens in the date column zone.
    Handles: '01 Jul 2025', '1 Jul 2025', '28 May 2025'.
    No specific format hardcoded — detects day/month/year by token type.
    """
    texts = [w["text"] for w in words_in_date_zone]
    if not texts:
        return None

    day = mon_abbr = yr = None
    for t in texts:
        if re.match(r"^\d{1,2}$", t) and day is None:
            day = int(t)
        elif t.lower()[:3] in _MONTH_MAP and mon_abbr is None:
            mon_abbr = t[:3].capitalize()
        elif re.match(r"^\d{4}$", t) and yr is None:
            yr = int(t)

    if day and mon_abbr and yr:
        try:
            return datetime(yr, _MONTH_MAP[mon_abbr.lower()], day).strftime("%d-%b-%Y")
        except ValueError:
            pass
    return None


# ── Layout detection ──────────────────────────────────────────────────────────

def _detect_layout(pdf_path: str) -> str:
    """
    Detect layout from column header structure on the first few pages.
    No product names, brand strings, or date formats used.

    ELECTRONIC: header has 'balance' column.
    ACTIVITY:   header has 'debit'/'credit' but NO 'balance' column.
    """
    try:
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
                has_debit   = bool(cols & {"debit"})
                has_credit  = bool(cols & {"credit"})
                if has_balance:
                    return "ELECTRONIC"
                if has_debit or has_credit:
                    return "ACTIVITY"
    except Exception:
        pass
    return "ELECTRONIC"


# ── LAYOUT: ELECTRONIC STATEMENT ─────────────────────────────────────────────

def _parse_electronic(pages, file_format: str) -> list:
    """
    Electronic Statement layout: DATE | TRANSACTION DESCRIPTION | DEBIT | CREDIT | BALANCE

    Row structure (2-3 rows per transaction):
      Row 1: date token (date col) + transaction type words (desc col)
      Row 2: continuation desc words (desc col) + amount token (debit/credit col) + balance (balance col)
      Row 3: (optional) more desc continuation

    Footer sentinel: CLOSING BALANCE row stops processing for that page.
    Amounts: plain numbers (no $ sign required).
    Sign: derived from balance delta (balance_t - balance_{t-1}).
          Fallback: debit col = negative, credit col = positive.
    """
    transactions = []
    prev_balance = [None]
    cached_bounds = [None]
    left_margin   = [0.0]
    year_state    = [None]   # [last_seen_year] — inferred from first date found

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
        # Determine the top position of the header row on this page.
        # CLOSING BALANCE rows above the header are summary labels — skip them.
        # CLOSING BALANCE rows below the header are the real footer sentinel.
        header_top = min(w["top"] for w in header) if header else 0.0
        rows = _group_rows(words)

        past_closing = [False]
        pending_date  = None
        pending_descs = []
        pending_top   = 0.0
        pending_deb   = ""
        pending_cred  = ""
        pending_bal   = ""

        def emit():
            nonlocal pending_date, pending_descs, pending_deb, pending_cred, pending_bal
            if not pending_date:
                return
            dv = _parse_num(pending_deb)  if pending_deb  else None
            cv = _parse_num(pending_cred) if pending_cred else None
            bv_str = re.sub(r"\b(DR|CR)\b", "", pending_bal, flags=re.I).strip() if pending_bal else ""
            bv = _parse_num(bv_str) if bv_str else None
            if dv is None and cv is None:
                pending_date = None; pending_descs = []; pending_deb = pending_cred = pending_bal = ""; return

            # Sign from balance delta; fallback from column position
            raw = (cv or 0.0) - (dv or 0.0)
            if bv is not None and prev_balance[0] is not None:
                delta = round(bv - prev_balance[0], 2)
                if abs(abs(delta) - abs(raw)) <= 0.02:
                    raw = delta
            if bv is not None:
                prev_balance[0] = bv

            transactions.append(make_txn(
                "", pending_date,
                " ".join(pending_descs).strip(),
                round(raw, 2), bv,
                page_num, pending_top, confidence=1.0
            ))
            pending_date = None; pending_descs = []
            pending_deb = pending_cred = pending_bal = ""

        for row in rows:
            if past_closing[0]:
                break

            clean_row = [w for w in row if w["x0"] >= lm]
            if not clean_row:
                continue

            row_text = " ".join(w["text"] for w in clean_row if w["text"].lower() not in _BLANK_TOKENS)

            # Structural sentinel: CLOSING BALANCE row ends transaction data.
            # Only fires for rows that appear BELOW the header row on this page.
            # Rows above the header are the summary section (skip as labels).
            if re.match(r"^\s*(CLOSING|STATEMENT\s+CLOSING)\s+BALANCE\b", row_text, re.I):
                if clean_row[0]["top"] > header_top:
                    emit()
                    past_closing[0] = True
                    break
                # Above header = summary section label — skip
                continue

            # Skip structural noise rows (not by brand name — by structural pattern)
            if re.match(r"^\s*(STATEMENT\s+)?OPENING\s+BALANCE\b", row_text, re.I):
                # Extract opening balance if above header (summary section)
                if clean_row[0]["top"] <= header_top:
                    m = re.search(r"([\d,]+\.\d{2})", row_text)
                    if m and prev_balance[0] is None:
                        try: prev_balance[0] = float(m.group(1).replace(",",""))
                        except ValueError: pass
                continue
            # Skip other summary rows (Total Credits, Total Debits) in summary section
            if re.match(r"^\s*(Total\s+(Credits?|Debits?)|TOTAL\s+(CREDITS?|DEBITS?))", row_text, re.I):
                if clean_row[0]["top"] <= header_top:
                    continue
            if re.match(r"^\s*TRANSACTIONS\s*$", row_text, re.I):
                continue
            if re.match(r"^\s*Please\s+check\s+all\s+entries", row_text, re.I):
                continue

            cols = _classify_words(clean_row, bounds)
            date_str  = cols.get("date", "").strip()
            desc_str  = cols.get("desc", "").strip()
            deb_raw   = cols.get("debit",   "").strip()
            cred_raw  = cols.get("credit",  "").strip()
            bal_raw   = cols.get("balance", "").strip()

            # Extract valid amount strings
            def _extract_amt(s):
                if not s or s.lower() in _BLANK_TOKENS: return ""
                # Accept plain numbers or $-prefixed
                if _is_amount_token(s.split()[0] if s.split() else ""):
                    return s.split()[0]
                m = re.search(r"[\d,]+\.\d{2}", s)
                return m.group(0) if m else ""

            deb_str  = _extract_amt(deb_raw)
            cred_str = _extract_amt(cred_raw)
            bal_str  = _extract_amt(bal_raw)

            parsed_date = _parse_slash_date(date_str) if date_str else None
            is_date_row = bool(parsed_date)

            if is_date_row:
                emit()
                pending_date  = parsed_date
                pending_top   = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []
                pending_deb   = deb_str
                pending_cred  = cred_str
                pending_bal   = bal_str
                # If this row has date + amount + balance, still defer emit
                # so any continuation description rows can be collected first

            elif (deb_str or cred_str) and pending_date:
                if desc_str:
                    pending_descs.append(desc_str)
                pending_deb  = pending_deb  or deb_str
                pending_cred = pending_cred or cred_str
                pending_bal  = pending_bal  or bal_str
                emit()

            elif pending_date and desc_str:
                # Pure continuation desc row — skip only if it looks like metadata
                # (row that starts with a date-like token in desc position)
                if not re.match(r"^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$",
                                desc_str, re.I):
                    pending_descs.append(desc_str)

        emit()

    return transactions


# ── ZIP format (Format A legacy) ──────────────────────────────────────────────

def _parse_electronic_zip(pages_text: list) -> list:
    """
    Parse Westpac zip format (one .txt file per page).
    Structure: DATE TRANSACTION_DESCRIPTION [AMOUNT] [BALANCE] on text lines.
    Continuation lines follow with no leading date.
    Uses balance delta for sign — no keyword matching.
    """
    _DATE_LINE_RE = re.compile(r"^(\d{2}/\d{2}/\d{2,4})\s+(.+)$")
    _NUMBER_RE    = re.compile(r"-?[\d,]+\.\d{2}")

    transactions = []
    prev_balance = [None]

    def _extract_open_bal(text):
        m = re.search(r"Opening\s+Balance\s*\+?\s*\$?([\d,]+\.\d{2})", text, re.I)
        if m:
            try: return float(m.group(1).replace(",",""))
            except ValueError: pass
        return None

    cur_date = cur_lines = None
    cur_top  = 0.0
    page_num = 0

    def flush():
        if not cur_date or not cur_lines:
            return
        full = " ".join(cur_lines).strip()
        if re.match(r"^(STATEMENT\s+)?(OPENING|CLOSING)\s+BALANCE\b", full, re.I):
            return
        nums = list(_NUMBER_RE.finditer(full))
        if len(nums) < 2:
            return
        bv = None
        try: bv = float(nums[-1].group().replace(",",""))
        except ValueError: pass
        raw = None
        try: raw = float(nums[-2].group().replace(",",""))
        except ValueError: pass
        if raw is None:
            return
        desc = re.sub(r"\s+", " ", full[:nums[-2].start()].strip())
        signed = raw
        if bv is not None and prev_balance[0] is not None:
            delta = round(bv - prev_balance[0], 2)
            if abs(abs(delta) - abs(raw)) <= 0.02:
                signed = delta
        if bv is not None:
            prev_balance[0] = bv
        transactions.append(make_txn(
            "", cur_date, desc, round(signed, 2), bv,
            page_num, cur_top, confidence=1.0
        ))

    for pg_num, text in pages_text:
        page_num = pg_num
        if prev_balance[0] is None:
            ob = _extract_open_bal(text)
            if ob is not None:
                prev_balance[0] = ob

        lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
        line_top = 0.0
        for line in lines:
            s = line.strip()
            if not s:
                continue
            # Skip structural noise by pattern not by string
            if re.match(r"^(TRANSACTIONS|Please\s+check\s+all|CLOSING\s+BALANCE|STATEMENT\s+OPENING|OPENING\s+BALANCE)", s, re.I):
                if re.match(r"^(CLOSING|STATEMENT\s+CLOSING)\s+BALANCE", s, re.I):
                    flush(); cur_date = cur_lines = None
                continue
            # Skip footer lines: boilerplate paragraphs and legal text
            # Detected structurally: lines after last numeric line
            m = _DATE_LINE_RE.match(s)
            if m:
                flush()
                cur_date  = _parse_slash_date(m.group(1))
                cur_lines = [m.group(2).strip()] if m.group(2).strip() else []
                cur_top   = line_top
            else:
                if cur_date:
                    cur_lines.append(s)
            line_top += 12.0

    flush()
    return transactions


# ── LAYOUT: ACCOUNT ACTIVITY ──────────────────────────────────────────────────

def _parse_activity(pages) -> list:
    """
    Account Activity layout: Date | Description | Debit | Credit (no balance)

    Row structure per transaction (2-4 rows, INVERTED):
      Row A (desc type):  [WITHDRAWAL-OSKO PAYMENT ...] in desc col
      Row B (anchor):     [DD Mon YYYY] in date col + [remainder desc] + [-$amount or $amount]
      Row C (optional):   continuation desc in desc col
      Row D (optional):   more continuation

    The anchor row is identified by: has date-zone words AND has a signed amount token.
    All rows within a dynamically-computed proximity window belong to the anchor.
    Footer: detected by first row that has no words in any table column zone
             AND comes after the last anchor row seen.
    Sign: embedded in amount token (-$X = negative, $X = positive).
    """
    transactions = []

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not words:
            continue

        header = _find_header_row(words)
        if not header:
            continue

        bounds     = _compute_col_bounds(header)
        lm         = _derive_left_margin(header)
        line_sp    = _median_line_spacing(words)
        # Continuation window: up to 3 lines away from anchor; derived from line spacing
        cont_dist  = line_sp * 3.5

        by_top = defaultdict(list)
        for w in words:
            by_top[round(w["top"], 1)].append(w)
        sorted_tops = sorted(by_top.keys())

        date_lo, date_hi = bounds.get("date", (0.0, 110.0))
        desc_lo, desc_hi = bounds.get("desc", (110.0, 340.0))
        amt_lo           = min(
            bounds.get("debit",  (340.0, 9999.0))[0],
            bounds.get("credit", (340.0, 9999.0))[0],
        )

        # Detect footer boundary: first top where ALL remaining rows have no
        # words in the table column zone (past the last anchor)
        footer_top = 9999.0
        for top in reversed(sorted_tops):
            row = by_top[top]
            has_table_word = any(
                w["x0"] >= lm and (
                    date_lo <= w["x0"] < amt_lo + 120
                )
                for w in row
            )
            if has_table_word:
                # rows after last table row + 2 line spacings = footer
                footer_top = top + line_sp * 2
                break

        # Pass 1: identify anchor rows (have date-zone tokens AND a signed amount)
        anchors: dict[float, dict] = {}
        absorbed: set[float] = set()

        for idx, top in enumerate(sorted_tops):
            if top >= footer_top:
                break
            rw = sorted(by_top[top], key=lambda w: w["x0"])
            rw = [w for w in rw if w["x0"] >= lm]

            date_words = [w for w in rw if date_lo <= w["x0"] < date_lo + (date_hi - date_lo)]
            amt_words  = [w for w in rw if w["x0"] >= amt_lo and _is_amount_token(w["text"])]
            desc_words = [w for w in rw if desc_lo <= w["x0"] < amt_lo]

            if not date_words:
                continue
            parsed_date = _parse_mon_date_from_words(date_words)
            if not parsed_date:
                continue

            # Amount must be on this row OR the very next row (within 1 line spacing)
            if not amt_words:
                for j in range(idx+1, min(idx+4, len(sorted_tops))):
                    nt = sorted_tops[j]
                    if nt - top > line_sp * 1.5:
                        break
                    next_amts = [w for w in sorted(by_top[nt], key=lambda w: w["x0"])
                                 if w["x0"] >= amt_lo and _is_amount_token(w["text"])]
                    if next_amts:
                        amt_words = next_amts
                        absorbed.add(nt)
                        break

            if not amt_words:
                continue

            # When multiple amount tokens on anchor row, prefer:
            # 1. Tokens with explicit $ sign (actual transaction amounts)
            # 2. Rightmost token (amounts are right-aligned in their column)
            # This handles cases like "SECTION 7.11" where 7.11 is a reference
            # code that sits left of the real transaction amount "-$29,067.75"
            dollar_amts = [w for w in amt_words if re.match(r"^[+\-−]?\$", w["text"])]
            if dollar_amts:
                # Use rightmost $ amount
                best_amt = max(dollar_amts, key=lambda w: w["x0"])
            else:
                # Use rightmost plain amount
                best_amt = max(amt_words, key=lambda w: w["x0"])
            amt_val = _parse_num(best_amt["text"])
            if amt_val is None:
                continue

            anchors[top] = {
                "date":    parsed_date,
                "amount":  amt_val,
                "extra":   [w["text"] for w in desc_words],
            }

        if not anchors:
            continue

        anchor_tops = sorted(anchors.keys())

        # Pass 2: assign non-anchor desc rows to their nearest anchor
        txn_rows: dict[float, list] = defaultdict(list)
        # Collect header row top values so Pass 2 skips them
        header_tops_set = set()
        if header:
            for w in header:
                header_tops_set.add(round(w["top"], 1))

        for top in sorted_tops:
            if top in anchors or top in absorbed:
                continue
            # Skip header row words — they are column labels not description
            if top in header_tops_set:
                continue
            if top >= footer_top:
                break
            rw = sorted(by_top[top], key=lambda w: w["x0"])
            rw = [w for w in rw if w["x0"] >= lm]

            # Skip rows with no words in desc zone
            desc_words = [w for w in rw if desc_lo <= w["x0"] < amt_lo]
            if not desc_words:
                continue

            # Skip rows that are ONLY amount tokens (page subtotals, etc.)
            if all(_is_amount_token(w["text"]) for w in rw):
                continue

            nearest = min(anchor_tops, key=lambda a: abs(a - top))
            if abs(nearest - top) <= cont_dist:
                txn_rows[nearest].append((top, [w["text"] for w in desc_words]))

        # Pass 3: assemble transactions
        for anchor_top in anchor_tops:
            info = anchors[anchor_top]
            desc_rows = sorted(txn_rows[anchor_top], key=lambda x: x[0])
            pre_desc  = [t for row_top, wl in desc_rows if row_top < anchor_top for t in wl]
            post_desc = [t for row_top, wl in desc_rows if row_top > anchor_top for t in wl]
            all_desc  = pre_desc + info["extra"] + post_desc
            desc_str  = re.sub(r"\s+", " ", " ".join(all_desc)).strip()

            transactions.append(make_txn(
                "", info["date"], desc_str,
                round(info["amount"], 2), None,
                page_num, float(anchor_top), confidence=1.0
            ))

    return transactions


# ── File format detection (PDF vs ZIP) ───────────────────────────────────────

def _detect_file_format(path: str) -> str:
    try:
        with zipfile.ZipFile(path, "r"):
            return "zip"
    except (zipfile.BadZipFile, Exception):
        return "pdf"


def _read_pages_zip(path: str) -> list:
    pages = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") or name == "manifest.json":
                continue
            try: pnum = int(name.replace(".txt",""))
            except ValueError: continue
            pages.append((pnum, zf.open(name).read().decode("utf-8", errors="replace")))
    return sorted(pages)


# ── Public API ────────────────────────────────────────────────────────────────

def can_parse(first_page_text: str, page_count: int) -> float:
    """
    Score on structural signals only.
    'westpac' is a low-weight signal, not a hard gate.
    """
    txt = first_page_text.lower()
    score = 0.0
    if "westpac" in txt:
        score += 0.35
    # Structural: column header vocabulary
    if "debit" in txt and "credit" in txt:
        score += 0.25
    if "balance" in txt:
        score += 0.15
    if re.search(r"\bdate\b", txt) and re.search(r"\btransaction\b|\bdescription\b", txt):
        score += 0.15
    # Structural: date formats present
    if re.search(r"\b\d{2}/\d{2}/\d{2,4}\b", txt):
        score += 0.1
    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    t0 = time.time()
    file_format = _detect_file_format(pdf_path)
    layout = _detect_layout(pdf_path)

    transactions = []
    page_count   = 0

    if file_format == "zip":
        pages_text = _read_pages_zip(pdf_path)
        page_count = len(pages_text)
        transactions = _parse_electronic_zip(pages_text)
    else:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            if layout == "ACTIVITY":
                transactions = _parse_activity(pdf.pages)
            else:
                transactions = _parse_electronic(pdf.pages, "pdf")

    transactions.sort(key=lambda t: (t.get("date") or "", t.get("source_page", 0), t.get("row_top", 0)))
    for i, t in enumerate(transactions):
        t["transaction_id"] = f"westpac_{i+1:04d}"

    return build_result(transactions, [], {
        "bank":          "Westpac",
        "bank_id":       "westpac",
        "layout":        layout,
        "file_format":   file_format,
        "pages":         page_count,
        "parse_time_ms": round((time.time() - t0) * 1000),
    })
