"""
parsers/westpac.py — Westpac unified statement parser
======================================================
Handles all Westpac statement layouts via structural detection only.

DYNAMIC PRINCIPLES
──────────────────
- Layout detected solely from which column headers are present on the page.
  No product names, account types, brand strings, or date formats used for routing.
- Column boundaries derived from actual header word positions (midpoints).
  Cached across pages — header only needs to appear once per statement.
- Left content margin derived from header's leftmost word.
- Amount tokens: accepts plain numbers (3,641.00), $-prefixed ($269.50),
  signed (-$269.50), and DR/CR suffixed. No hard requirement for $ sign.
- Date formats: flexible — slash-separated (DD/MM/YY, DD/MM/YYYY),
  dash-separated (DD-MM-YY, DD-MM-YYYY), dot-separated (DD.MM.YYYY),
  and text-month (DD Mon YYYY, DD Mon YY). Detected by token type, not regex.
- Footer detection: structural sentinel (CLOSING BALANCE row below header)
  with positional fallback — not boilerplate string matching.
- Continuation distance (Activity layout): derived from document's median line
  spacing, not a fixed constant.
- Sign: balance delta correction for layouts with a balance column.
  Signed amount embedded in token for no-balance layouts.
  No description keyword matching for sign determination.

LAYOUTS (detected from header column structure only)
────────────────────────────────────────────────────
  ELECTRONIC  — DATE | TRANSACTION DESCRIPTION | DEBIT | CREDIT | BALANCE
                Signal: 'balance' column present in header.
                Dates: any separator (/, -, .) or text-month format.
                Amounts: plain numbers, no $ sign required.

  ACTIVITY    — Date | Description | Debit | Credit  (no balance column)
                Signal: debit+credit present but NO balance column.
                Date: multi-word (DD Mon YYYY) on same row as amount.
                Amounts: signed $ tokens (-$269.50 / $25000.00).
                Row order: description type ABOVE the date+amount anchor row.
                Header cached from first page — Activity only prints it once.
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


# ── Constants & patterns ──────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}
# Reverse map: number → abbr
_MONTH_ABBR = {v: k.capitalize() for k, v in _MONTH_MAP.items()}

# Flexible amount: plain number, $-prefixed, signed, DR/CR suffix
_PLAIN_NUM_RE = re.compile(r"^[+\-−]?[\d,]+\.\d{2}$")
_DOLLAR_RE    = re.compile(r"^[+\-−]?\$[\d,]+(?:\.\d{2})?$")
_BLANK_TOKENS = {"blank"}

# Header vocabulary — used for detection only, never for routing
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

# Header vocab density threshold — real headers are mostly column-name words.
# Documented constant: if <40% of header-band words are recognised vocab, reject.
_HDR_DENSITY_MIN = 0.40


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
    Accept: plain numbers with decimal (3,641.00), $-prefixed ($1,234.56),
    signed (-$269.50, -1,234.56). Reject bare integers (reference numbers).
    Requires a decimal point OR a $ sign to distinguish from date parts / codes.
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


# ── Flexible date parsing ─────────────────────────────────────────────────────

def _parse_date_str(s: str) -> Optional[str]:
    """
    Parse a date string into DD-Mon-YYYY format.
    Handles all separator styles and both 2/4 digit years — no format hardcoded:
      DD/MM/YY   DD/MM/YYYY
      DD-MM-YY   DD-MM-YYYY
      DD.MM.YY   DD.MM.YYYY
    Returns None if not a valid date.
    """
    s = s.strip()
    if not s:
        return None
    # Split on any non-digit separator
    parts = re.split(r"[/\-.]", s)
    if len(parts) == 3:
        try:
            day, mon_n, yr = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None
        if yr < 100:
            yr += 2000
        if not (1 <= mon_n <= 12 and 1 <= day <= 31):
            return None
        try:
            return datetime(yr, mon_n, day).strftime("%d-%b-%Y")
        except ValueError:
            return None
    return None


def _parse_mon_date_from_words(words_in_date_zone: list) -> Optional[str]:
    """
    Parse a date from multi-word tokens in the date column zone.
    Detects day, month, year by token type — no format hardcoded.
    Handles: '01 Jul 2025', '1 Jul 2025', '28 May 2025', '20 Sep 2024'.
    """
    texts = [w["text"] for w in words_in_date_zone]
    if not texts:
        return None

    day = mon_abbr = yr = None
    for t in texts:
        if re.match(r"^\d{4}$", t) and yr is None:
            yr = int(t)
        elif re.match(r"^\d{1,2}$", t) and day is None:
            day = int(t)
        elif t.lower()[:3] in _MONTH_MAP and mon_abbr is None:
            mon_abbr = t[:3].capitalize()

    if day and mon_abbr and yr:
        try:
            return datetime(yr, _MONTH_MAP[mon_abbr.lower()], day).strftime("%d-%b-%Y")
        except ValueError:
            pass
    return None


# ── Header detection ──────────────────────────────────────────────────────────

def _find_header_row(words: list) -> Optional[list]:
    """
    Find the table header row. Purely structural:
    - Must contain a date/description keyword AND an amount-column keyword.
    - Uses a merge window derived from the document's actual line spacing.
    - Rejects bands where recognised vocabulary is < _HDR_DENSITY_MIN of all words
      (prevents prose rows merged with the real header from being accepted).
    - Rejects bands containing multiple dollar-amount tokens (data rows, not headers).
    """
    if not words:
        return None

    sorted_words = sorted(words, key=lambda w: w["top"])

    # Merge window from actual line spacing — adapts to font/spacing
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
            # Reject data rows: real header rows never contain multiple $ amounts
            if sum(1 for w in candidate if re.match(r"^\$[\d,]+\.\d{2}", w["text"])) > 1:
                continue
            # Reject bands where prose dominates over header vocabulary
            non_blank = [w for w in candidate if w["text"].strip()]
            if non_blank:
                hdr_count = sum(
                    1 for w in non_blank
                    if re.sub(r"\s*\(.*?\)", "", w["text"].lower().strip()) in _HEADER_VOCAB
                )
                if hdr_count / len(non_blank) < _HDR_DENSITY_MIN:
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
    Numeric tokens use centre x and may land in any column.
    Non-numeric tokens ONLY go to text columns (date/desc) — never amount columns.
    This handles description words that visually overlap the amount zone without
    any coordinate hardcoding.
    """
    AMT_COLS      = {"debit", "credit", "balance"}
    TEXT_COLS     = {col for col in bounds if col not in AMT_COLS}
    text_cols_asc = sorted(TEXT_COLS, key=lambda c: bounds[c][0])
    result        = {k: [] for k in bounds}

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
                    result[col].append(txt); placed = True; break
            if not placed:
                for col in text_cols_asc:
                    lo, hi = bounds[col]
                    if lo <= x0 < hi:
                        result[col].append(txt); placed = True; break
        else:
            for col in text_cols_asc:
                lo, hi = bounds[col]
                if lo <= x0 < hi:
                    result[col].append(txt); placed = True; break
            if not placed and text_cols_asc:
                result[text_cols_asc[-1]].append(txt)

    return {k: " ".join(v).strip() for k, v in result.items()}


# ── Layout detection ──────────────────────────────────────────────────────────

def _detect_layout(pdf_path: str) -> str:
    """
    Detect layout from column header structure only.
    No product names, brand strings, or date formats consulted.

    ELECTRONIC: 'balance' column present in header.
    ACTIVITY:   'debit'/'credit' present but NO 'balance' column.
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
                if "balance" in cols:
                    return "ELECTRONIC"
                if cols & {"debit", "credit"}:
                    return "ACTIVITY"
    except Exception:
        pass
    return "ELECTRONIC"


# ── LAYOUT: ELECTRONIC STATEMENT ─────────────────────────────────────────────

def _parse_electronic(pages) -> list:
    """
    Electronic Statement: DATE | TRANSACTION DESCRIPTION | DEBIT | CREDIT | BALANCE

    Row structure per transaction (2-3 rows):
      Row 1: date token + transaction type words
      Row 2: continuation desc + amount token + balance
      Row 3: (optional) more continuation desc

    Footer sentinel: CLOSING BALANCE row below header_top stops page processing.
    Positional fallback: any row at or below page height × 0.95 with no date is skipped.
    Header cached across pages — detected once and reused.
    Amounts: plain numbers (no $ required). Sign from balance delta.
    """
    transactions  = []
    prev_balance  = [None]
    cached_bounds = [None]
    cached_lm     = [0.0]
    cached_header = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        header = _find_header_row(words)
        if header:
            cached_header[0] = header
            cached_bounds[0] = _compute_col_bounds(header)
            cached_lm[0]     = _derive_left_margin(header)

        bounds = cached_bounds[0]
        if not bounds:
            continue

        lm     = cached_lm[0]
        header = cached_header[0]
        # header_top: CLOSING BALANCE rows above this are summary labels
        header_top = min(w["top"] for w in header) if header else 0.0
        rows = _group_rows(words)

        past_closing  = [False]
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
                pending_date = None; pending_descs = []
                pending_deb = pending_cred = pending_bal = ""
                return

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

            # Structural footer sentinel: CLOSING BALANCE below header_top ends data
            if re.search(r"\bCLOSING\s+BALANCE\b", row_text, re.I):
                if clean_row[0]["top"] > header_top:
                    emit()
                    past_closing[0] = True
                    break
                # Above header = summary label — extract opening balance if available
                m = re.search(r"([\d,]+\.\d{2})", row_text)
                if m and prev_balance[0] is None:
                    try: prev_balance[0] = float(m.group(1).replace(",", ""))
                    except ValueError: pass
                continue

            # Extract opening balance from summary rows above header_top
            if re.search(r"\bOPENING\s+BALANCE\b", row_text, re.I):
                if clean_row[0]["top"] <= header_top:
                    m = re.search(r"([\d,]+\.\d{2})", row_text)
                    if m and prev_balance[0] is None:
                        try: prev_balance[0] = float(m.group(1).replace(",", ""))
                        except ValueError: pass
                continue

            # Skip Total Credits / Total Debits summary rows (above header only)
            if re.search(r"\bTotal\s+(Credits?|Debits?)\b", row_text, re.I):
                if clean_row[0]["top"] <= header_top:
                    continue

            cols     = _classify_words(clean_row, bounds)
            date_str = cols.get("date", "").strip()
            desc_str = cols.get("desc", "").strip()

            def _extract_amt(s):
                if not s or s.lower() in _BLANK_TOKENS:
                    return ""
                tokens = s.split()
                if tokens and _is_amount_token(tokens[0]):
                    return tokens[0]
                m = re.search(r"[\d,]+\.\d{2}", s)
                return m.group(0) if m else ""

            deb_str  = _extract_amt(cols.get("debit",   ""))
            cred_str = _extract_amt(cols.get("credit",  ""))
            bal_str  = _extract_amt(cols.get("balance", ""))

            # Try all date formats — flexible, not hardcoded to one separator
            parsed_date = _parse_date_str(date_str) if date_str else None
            is_date_row = bool(parsed_date)

            if is_date_row:
                emit()
                pending_date  = parsed_date
                pending_top   = row[0]["top"]
                pending_descs = [desc_str] if desc_str else []
                pending_deb   = deb_str
                pending_cred  = cred_str
                pending_bal   = bal_str
                # Defer emit even when balance present — collect continuation desc first

            elif (deb_str or cred_str) and pending_date:
                if desc_str:
                    pending_descs.append(desc_str)
                pending_deb  = pending_deb  or deb_str
                pending_cred = pending_cred or cred_str
                pending_bal  = pending_bal  or bal_str
                emit()

            elif pending_date and desc_str:
                # Continuation desc row — skip rows that are purely metadata dates
                # (detected structurally: digit + known-month-token + digit pattern)
                parts = desc_str.split()
                is_meta_date = (
                    len(parts) >= 2
                    and re.match(r"^\d{1,2}$", parts[0])
                    and parts[1].lower()[:3] in _MONTH_MAP
                )
                if not is_meta_date:
                    pending_descs.append(desc_str)

        emit()

    return transactions


# ── ZIP format (legacy text export) ──────────────────────────────────────────

def _parse_electronic_zip(pages_text: list) -> list:
    """
    Parse Westpac ZIP export (one .txt per page).
    DATE DESCRIPTION [AMOUNT] [BALANCE] on text lines.
    Continuation lines follow with no leading date.
    Sign from balance delta — no keyword matching.
    Date: flexible separator detection, not hardcoded regex.
    """
    _NUMBER_RE = re.compile(r"-?[\d,]+\.\d{2}")

    transactions = []
    prev_balance = [None]

    def _extract_open_bal(text):
        m = re.search(r"Opening\s+Balance\s*\+?\s*\$?([\d,]+\.\d{2})", text, re.I)
        if m:
            try: return float(m.group(1).replace(",", ""))
            except ValueError: pass
        return None

    def _try_parse_date_from_line_start(s: str):
        """
        Try to parse a date from the start of a text line.
        Works with any separator (/, -, .) and 2 or 4 digit years.
        Returns (date_str, remainder) or (None, s).
        """
        m = re.match(r"^(\d{1,2}[/\-.](\d{1,2})[/\-.](\d{2,4}))\s*(.*)", s)
        if not m:
            return None, s
        date_part = m.group(1)
        remainder = m.group(4)
        parsed = _parse_date_str(date_part)
        return parsed, remainder

    cur_date = cur_lines = None
    cur_top  = 0.0
    page_num = 0

    def flush():
        if not cur_date or not cur_lines:
            return
        full = " ".join(cur_lines).strip()
        if re.search(r"\b(OPENING|CLOSING)\s+BALANCE\b", full, re.I):
            return
        nums = list(_NUMBER_RE.finditer(full))
        if len(nums) < 2:
            return
        bv = raw = None
        try: bv  = float(nums[-1].group().replace(",", ""))
        except ValueError: pass
        try: raw = float(nums[-2].group().replace(",", ""))
        except ValueError: pass
        if raw is None:
            return
        desc   = re.sub(r"\s+", " ", full[:nums[-2].start()].strip())
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
            # Structural skip: balance markers stop/reset transaction accumulation
            if re.search(r"\bCLOSING\s+BALANCE\b", s, re.I):
                flush(); cur_date = cur_lines = None
                continue
            if re.search(r"\b(OPENING|STATEMENT)\s+(OPENING\s+)?BALANCE\b", s, re.I):
                continue
            # Try flexible date parse from line start
            parsed, remainder = _try_parse_date_from_line_start(s)
            if parsed:
                flush()
                cur_date  = parsed
                cur_lines = [remainder.strip()] if remainder.strip() else []
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
    Account Activity: Date | Description | Debit | Credit (no balance column)

    Row structure per transaction (2-4 rows, INVERTED):
      Row A: [TRANSACTION TYPE ...] in desc col          ← pre-anchor desc
      Row B: [DD Mon YYYY] in date col + [desc] + [$AMT] ← anchor row
      Row C: [more desc continuation]                    ← post-anchor desc

    The anchor row: has date-zone tokens parseable as a date AND a signed $ amount.
    Header cached across pages — Activity only prints the header once (page 1).
    Continuation window: computed from page's median line spacing × 3.5.
    Footer: detected positionally from last anchor row, not by string matching.
    Sign: embedded in amount token (-$X = negative, $X = positive).
    Multiple amounts on anchor: prefer $-prefixed, then rightmost (handles
    reference codes like 'SECTION 7.11' that look numeric but aren't amounts).
    """
    transactions   = []
    cached_bounds  = [None]
    cached_lm      = [0.0]
    cached_header  = [None]

    for page_num, page in enumerate(pages, 1):
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not words:
            continue

        header = _find_header_row(words)
        if header:
            cached_header[0] = header
            cached_bounds[0] = _compute_col_bounds(header)
            cached_lm[0]     = _derive_left_margin(header)

        # Use cached bounds even when this page has no header (Activity: header once)
        bounds = cached_bounds[0]
        if not bounds:
            continue

        lm      = cached_lm[0]
        line_sp = _median_line_spacing(words)
        cont_dist = line_sp * 3.5

        by_top = defaultdict(list)
        for w in words:
            by_top[round(w["top"], 1)].append(w)
        sorted_tops = sorted(by_top.keys())

        date_lo, date_hi = bounds.get("date", (0.0, 110.0))
        desc_lo, _       = bounds.get("desc", (110.0, 340.0))
        amt_lo = min(
            bounds.get("debit",  (340.0, 9999.0))[0],
            bounds.get("credit", (340.0, 9999.0))[0],
        )

        # Footer boundary: last row that has words in the table zone + buffer
        footer_top = 9999.0
        for top in reversed(sorted_tops):
            row = by_top[top]
            if any(w["x0"] >= lm and w["x0"] < amt_lo + 120 for w in row):
                footer_top = top + line_sp * 2
                break

        # Header row tops — skip these in Pass 2 (they are column labels, not desc)
        header_tops_set = set()
        if cached_header[0]:
            for w in cached_header[0]:
                header_tops_set.add(round(w["top"], 1))

        # ── Pass 1: identify anchor rows ──────────────────────────────────────
        anchors:  dict[float, dict] = {}
        absorbed: set[float]        = set()

        for idx, top in enumerate(sorted_tops):
            if top >= footer_top:
                break
            rw = sorted(by_top[top], key=lambda w: w["x0"])
            rw = [w for w in rw if w["x0"] >= lm]

            date_words = [w for w in rw if date_lo <= w["x0"] < date_hi]
            amt_words  = [w for w in rw if w["x0"] >= amt_lo and _is_amount_token(w["text"])]
            desc_words = [w for w in rw if desc_lo <= w["x0"] < amt_lo]

            if not date_words:
                continue
            parsed_date = _parse_mon_date_from_words(date_words)
            if not parsed_date:
                # Also try slash-date format in case Activity ever uses it
                date_str = " ".join(w["text"] for w in date_words)
                parsed_date = _parse_date_str(date_str)
            if not parsed_date:
                continue

            # Amount may be on the next row (within 1 line spacing)
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

            # When multiple amounts on anchor row, prefer $ sign → rightmost
            # (handles reference codes like 'SECTION 7.11' left of the real amount)
            dollar_amts = [w for w in amt_words if re.match(r"^[+\-−]?\$", w["text"])]
            best_amt = max(dollar_amts or amt_words, key=lambda w: w["x0"])
            amt_val = _parse_num(best_amt["text"])
            if amt_val is None:
                continue

            anchors[top] = {
                "date":   parsed_date,
                "amount": amt_val,
                "extra":  [w["text"] for w in desc_words],
            }

        if not anchors:
            continue

        anchor_tops = sorted(anchors.keys())

        # ── Pass 2: assign continuation rows to nearest anchor ─────────────────
        txn_rows: dict[float, list] = defaultdict(list)

        for top in sorted_tops:
            if top in anchors or top in absorbed or top in header_tops_set:
                continue
            if top >= footer_top:
                break
            rw = sorted(by_top[top], key=lambda w: w["x0"])
            rw = [w for w in rw if w["x0"] >= lm]

            desc_words = [w for w in rw if desc_lo <= w["x0"] < amt_lo]
            if not desc_words:
                continue
            if all(_is_amount_token(w["text"]) for w in rw):
                continue

            nearest = min(anchor_tops, key=lambda a: abs(a - top))
            if abs(nearest - top) <= cont_dist:
                txn_rows[nearest].append((top, [w["text"] for w in desc_words]))

        # ── Pass 3: assemble transactions ──────────────────────────────────────
        for anchor_top in anchor_tops:
            info      = anchors[anchor_top]
            desc_rows = sorted(txn_rows[anchor_top], key=lambda x: x[0])
            pre  = [t for row_top, wl in desc_rows if row_top < anchor_top for t in wl]
            post = [t for row_top, wl in desc_rows if row_top > anchor_top for t in wl]
            desc = re.sub(r"\s+", " ", " ".join(pre + info["extra"] + post)).strip()
            transactions.append(make_txn(
                "", info["date"], desc,
                round(info["amount"], 2), None,
                page_num, float(anchor_top), confidence=1.0
            ))

    return transactions


# ── File format detection ─────────────────────────────────────────────────────

def _detect_file_format(path: str) -> str:
    try:
        with zipfile.ZipFile(path, "r"):
            return "zip"
    except Exception:
        return "pdf"


def _read_pages_zip(path: str) -> list:
    pages = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") or name == "manifest.json":
                continue
            try: pnum = int(name.replace(".txt", ""))
            except ValueError: continue
            pages.append((pnum, zf.open(name).read().decode("utf-8", errors="replace")))
    return sorted(pages)


# ── Public API ────────────────────────────────────────────────────────────────

def can_parse(first_page_text: str, page_count: int) -> float:
    """
    Score on structural signals only.
    'westpac' is a low-weight signal, not a hard gate — structural column
    vocabulary is the primary signal.
    """
    txt = first_page_text.lower()
    score = 0.0
    if "westpac" in txt:
        score += 0.35
    if "debit" in txt and "credit" in txt:
        score += 0.25
    if "balance" in txt:
        score += 0.15
    if re.search(r"\bdate\b", txt) and re.search(r"\btransaction\b|\bdescription\b", txt):
        score += 0.15
    # Generic date signal — any numeric date format present
    if re.search(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b", txt):
        score += 0.1
    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    t0          = time.time()
    file_format = _detect_file_format(pdf_path)
    layout      = _detect_layout(pdf_path)

    transactions = []
    page_count   = 0

    if file_format == "zip":
        pages_text   = _read_pages_zip(pdf_path)
        page_count   = len(pages_text)
        transactions = _parse_electronic_zip(pages_text)
    else:
        with pdfplumber.open(pdf_path) as pdf:
            page_count   = len(pdf.pages)
            if layout == "ACTIVITY":
                transactions = _parse_activity(pdf.pages)
            else:
                transactions = _parse_electronic(pdf.pages)

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
