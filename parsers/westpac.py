"""
parsers/westpac.py – Westpac Bank statement parser
Handles TWO distinct Westpac PDF formats:

  FORMAT A – "Business One Plus / Personal" (existing)
    • Date format:  DD/MM/YY
    • Columns:  Date | Description | Withdrawal | Deposit | Balance
    • Delivered as PDF or as a ZIP of .txt page files
    • Detected by: "Westpac Business One Plus" or "opening balance" in text

  FORMAT B – "Account Activity" (new)
    • Date format:  DD Mon YYYY  (e.g. "01 Jul 2025")
    • Columns:  Date | Description | Debit | Credit   (NO Balance column)
    • Amounts include $ sign and optional leading minus: -$269.50 / $25000.00
    • Description spans multiple lines; date row may be 1-2px offset from amount row
    • Detected by: "Account activity" header + Westpac branding
    • Verification:  sum(all amounts) == closing balance shown on page 1
"""

import re
import time
import zipfile
from collections import defaultdict

import pdfplumber

from parsers.utils import build_result, make_txn

DISPLAY_NAME = "Westpac"

# ─────────────────────────── FORMAT A (legacy) ────────────────────────────────

_DATE_RE_A  = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$")
_NUMBER_RE  = re.compile(r"-?[\d,]+\.\d{2}")
_SKIP_RE    = re.compile(r"^(STATEMENT OPENING BALANCE|CLOSING BALANCE|OPENING BALANCE)", re.I)

_CREDIT_KW  = re.compile(r"^(Deposit|ATM Deposit|Promotional Fee Rebate|Debit Card Refund)", re.I)
_DEBIT_KW   = re.compile(
    r"^(Withdrawal|Debit Card Purchase|Eftpos Debit|Payment By Authority|"
    r"Monthly Plan Fee|Overdrawn Fee|ATM Operator Fee|Withdrawal At|Withdrawal Mobile)", re.I
)

_NOISE_A = [re.compile(p, re.I) for p in [
    r"^westpac business one plus", r"^westpac banking corporation",
    r"^electronic statement", r"^statement no\.",
    r"^please check all entries", r"^date\s+transaction description",
    r"^opening balance", r"^total credits", r"^total debits",
    r"^closing balance", r"^statement period", r"^account name",
    r"^customer id", r"^bsb account number", r"^transactions$",
    r"^we wish to advise", r"^your statement continues",
    r"^convenience at your fingertips", r"^transaction fee summary",
    r"^fee\(s\) charged", r"^this account provides", r"^to reconcile",
    r"^further information", r"^\+61", r"^\$[\d,]+\.\d{2}$",
    r"^\d{2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}$",
]]


def _is_noise_a(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    return any(p.search(s) for p in _NOISE_A)


def _parse_float_a(s: str):
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _keyword_sign(desc: str, raw: float) -> float:
    if _CREDIT_KW.match(desc):
        return raw
    if _DEBIT_KW.match(desc):
        return -abs(raw)
    return -abs(raw)


def _parse_txn_a(date_str: str, full_desc: str, page_num: int,
                 prev_balance: list, top: float) -> dict | None:
    from parsers.utils import sign_from_balance_delta
    full_desc = full_desc.strip()
    if _SKIP_RE.match(full_desc):
        return None

    matches = list(_NUMBER_RE.finditer(full_desc))
    if len(matches) < 2:
        return None

    balance = _parse_float_a(matches[-1].group())
    txn_raw = _parse_float_a(matches[-2].group())
    if txn_raw is None or balance is None:
        return None

    desc = full_desc[:matches[-2].start()].strip()
    desc = re.sub(r"\s+", " ", desc)
    desc = re.sub(
        r"\s+\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*$",
        "", desc, flags=re.I
    ).strip()

    signed = sign_from_balance_delta(balance, prev_balance[0], txn_raw)
    if abs(abs(signed) - abs(txn_raw)) > 0.02:
        signed = _keyword_sign(desc, txn_raw)

    try:
        from datetime import datetime as _dt
        parsed_date = _dt.strptime(date_str, "%d/%m/%y").strftime("%d-%b-%Y")
    except ValueError:
        parsed_date = date_str

    return make_txn("", parsed_date, desc, round(signed, 2), balance, page_num, top)


def _parse_page_text_a(text: str, page_num: int, prev_balance: list) -> list:
    lines   = [l.rstrip("\r") for l in text.replace("\r\n", "\n").split("\n")]
    results = []
    cur_date = cur_lines = None
    cur_top  = 0.0

    def flush():
        if cur_date and cur_lines:
            txn = _parse_txn_a(cur_date, " ".join(cur_lines), page_num, prev_balance, cur_top)
            if txn:
                prev_balance[0] = txn["balance"]
                results.append(txn)

    for line in lines:
        s = line.strip()
        if not s or _is_noise_a(s):
            continue
        m = _DATE_RE_A.match(s)
        if m:
            remainder    = m.group(2).strip()
            purely_numeric = bool(re.match(r"^[-\d,.\s]+$", remainder))
            if purely_numeric and cur_date:
                cur_lines.append(remainder)
            else:
                flush()
                cur_date  = m.group(1)
                cur_lines = [remainder]
        else:
            if cur_date:
                cur_lines.append(s)
    flush()
    return results


def _extract_opening_balance_a(text: str):
    for line in text.splitlines():
        m = re.search(r"Opening Balance\s*\+?\s*\$?([\d,]+\.\d{2})", line, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


# ─────────────────────────── FORMAT B (Account Activity) ──────────────────────

# Date: "01 Jul 2025" – full month name, 4-digit year
_DATE_RE_B   = re.compile(
    r"^(\d{2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$",
    re.I,
)
# Amount: optional leading minus, dollar sign, digits, decimal
_AMOUNT_RE_B = re.compile(r"^-?\$[\d,]+\.\d{2}$")

# Noise patterns for Format B – things to completely ignore
_NOISE_B = [re.compile(p, re.I) for p in [
    r"^westpac\b",
    r"^account activity$",
    r"^\d{3}-\d{3}\s+\d+$",       # BSB + account number line
    r"^\$\d[\d,]*\.\d{2}$",       # standalone balance amount on cover
    r"^transactions$",
    r"^date$",
    r"^description$",
    r"^debit$",
    r"^credit$",
    r"^date\s+description",        # header row
    r"^copyright\s+©",
    r"^abn\s+\d",
    r"^things you should know",
    r"^the pdf report",
    r"^©",
    r"^banking corporation",
]]

# How far (in PDF points) a desc row can be from an anchor and still belong to it
_MAX_ASSIGN_DIST = 30.0


def _is_noise_b(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    return any(p.search(s) for p in _NOISE_B)


def _parse_amount_b(text: str) -> float | None:
    """Parse '-$1,234.56' or '$1,234.56' → signed float. Negative = debit."""
    s = text.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_page_b(page: object, page_num: int) -> list:
    """
    Parse one page of a Format B Westpac Account Activity PDF.

    Layout (all tops in PDF points):
        [desc_line_1 top=N, x0~133]          ← first description line
        [date+amount  top=N+4..9, x0~47/344/420] ← anchor row
        [desc_line_2  top=N+9,   x0~133]     ← continuation (optional)
        ...repeat...

    Strategy:
      Pass 1 – find all "anchor" rows: rows containing a valid date (x0<100)
                paired with an amount (x0>330). The date and amount may appear
                on rows 1-3 px apart (PDF rendering artefact); handle by scanning
                the 3 rows immediately below a date-only row for an amount.
      Pass 2 – assign every remaining desc row to its nearest anchor within
                _MAX_ASSIGN_DIST points, building pre- and post-desc lists.
      Pass 3 – assemble transactions in top order.
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=3)

    # Group words into rows keyed by rounded top (integer)
    rows: dict[int, list] = defaultdict(list)
    for w in words:
        rows[round(w["top"])].append(w)

    sorted_tops = sorted(rows.keys())

    # ── Pass 1: detect anchor rows ──────────────────────────────────────────
    anchors: dict[int, tuple] = {}          # top → (date_str, amount, extra_desc_words)
    absorbed_tops: set[int]   = set()       # tops that are the "1-px-split" amount rows

    for idx, top in enumerate(sorted_tops):
        rw = sorted(rows[top], key=lambda w: w["x0"])

        date_words  = [w for w in rw if w["x0"] < 100]
        amount_ws   = [w for w in rw if w["x0"] > 330 and _AMOUNT_RE_B.match(w["text"])]
        desc_ws     = [w for w in rw if 100 <= w["x0"] < 330]

        date_str = " ".join(w["text"] for w in date_words).strip()
        dm       = _DATE_RE_B.match(date_str)

        if not dm:
            continue

        # Date found – look for amount on the same row or ≤3 pts below
        if not amount_ws:
            for next_idx in range(idx + 1, min(idx + 5, len(sorted_tops))):
                next_top = sorted_tops[next_idx]
                if next_top - top > 3:
                    break
                next_rw   = sorted(rows[next_top], key=lambda w: w["x0"])
                next_amts = [w for w in next_rw if w["x0"] > 330
                             and _AMOUNT_RE_B.match(w["text"])]
                if next_amts:
                    amount_ws = next_amts
                    absorbed_tops.add(next_top)
                    break

        if not amount_ws:
            continue  # date row with no amount → skip

        formatted_date = (
            f"{dm.group(1)}-{dm.group(2).capitalize()}-{dm.group(3)}"
        )
        amount     = _parse_amount_b(amount_ws[0]["text"])
        extra_desc = [w["text"] for w in desc_ws]

        anchors[top] = (formatted_date, amount, extra_desc)

    if not anchors:
        return []

    anchor_tops = sorted(anchors.keys())

    # ── Pass 2: assign desc rows to nearest anchor ──────────────────────────
    txn_descs: dict[int, list] = defaultdict(list)   # anchor_top → [(row_top, [words])]

    for top in sorted_tops:
        if top in anchors:
            continue
        if top in absorbed_tops:
            continue

        rw       = sorted(rows[top], key=lambda w: w["x0"])
        row_text = " ".join(w["text"] for w in rw).strip()

        if _is_noise_b(row_text):
            continue

        # Skip rows that consist only of an amount token (absorbed amount rows
        # that weren't caught above, e.g. minor rounding differences)
        if all(w["x0"] > 330 for w in rw) and any(
            _AMOUNT_RE_B.match(w["text"]) for w in rw
        ):
            continue

        # Skip date-only rows extremely close to an anchor (stray y artefacts)
        if all(w["x0"] < 100 for w in rw):
            nearest = min(anchor_tops, key=lambda a: abs(a - top))
            if abs(nearest - top) <= 3:
                continue

        # Only consider rows that have description-zone words (x0 ≥ 100)
        desc_ws = [w for w in rw if w["x0"] >= 100]
        if not desc_ws:
            continue

        # Assign to nearest anchor within threshold
        nearest = min(anchor_tops, key=lambda a: abs(a - top))
        if abs(nearest - top) <= _MAX_ASSIGN_DIST:
            txn_descs[nearest].append((top, [w["text"] for w in desc_ws]))

    # ── Pass 3: assemble transactions ───────────────────────────────────────
    transactions = []
    for anchor_top in anchor_tops:
        formatted_date, amount, extra_anchor_desc = anchors[anchor_top]

        # Sort assigned desc rows by vertical position
        desc_rows = sorted(txn_descs[anchor_top], key=lambda x: x[0])

        pre_words  = []   # desc lines above the anchor (pre-description)
        post_words = []   # desc lines below the anchor (continuation)
        for row_top, word_list in desc_rows:
            if row_top < anchor_top:
                pre_words.extend(word_list)
            else:
                post_words.extend(word_list)

        # Build full description: pre + any words on the anchor row + post
        all_desc_tokens = pre_words + extra_anchor_desc + post_words
        desc = re.sub(r"\s+", " ", " ".join(all_desc_tokens)).strip()

        if amount is None:
            continue

        transactions.append(
            make_txn(
                "",               # transaction_id assigned later
                formatted_date,
                desc,
                round(amount, 2),
                None,             # no balance column in this format
                page_num,
                float(anchor_top),
                confidence=1.0,
            )
        )

    return transactions


def _parse_format_b(pdf_path: str) -> dict:
    """Entry point for Format B (Account Activity) PDFs."""
    t0 = time.time()
    transactions = []

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, 1):
            transactions.extend(_parse_page_b(page, page_num))

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    return build_result(
        transactions,
        [],
        {
            "bank":         "Westpac",
            "bank_id":      "westpac",
            "format":       "Account Activity (Debit/Credit, no balance)",
            "pages":        page_count if "page_count" in dir() else 0,
            "file_format":  "pdf",
            "parse_time_ms": round((time.time() - t0) * 1000),
        },
    )


# ─────────────────────────── FORMAT A helpers (I/O) ───────────────────────────

def _detect_file_format(path: str) -> str:
    try:
        with zipfile.ZipFile(path, "r"):
            return "zip"
    except zipfile.BadZipFile:
        return "pdf"


def _read_pages_zip(path: str) -> list:
    pages = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") or name == "manifest.json":
                continue
            try:
                pnum = int(name.replace(".txt", ""))
            except ValueError:
                continue
            pages.append((pnum, zf.open(name).read().decode("utf-8", errors="replace")))
    return sorted(pages)


def _read_pages_pdf_a(path: str) -> list:
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            pages.append((i, page.extract_text() or ""))
    return pages


def _parse_format_a(pdf_path: str) -> dict:
    """Entry point for Format A (Business One Plus / Personal) PDFs."""
    t0  = time.time()
    fmt = _detect_file_format(pdf_path)

    if fmt == "zip":
        pages = _read_pages_zip(pdf_path)
    else:
        pages = _read_pages_pdf_a(pdf_path)

    opening      = _extract_opening_balance_a(pages[0][1]) if pages else None
    prev_balance = [opening]

    transactions = []
    for page_num, text in pages:
        transactions.extend(_parse_page_text_a(text, page_num, prev_balance))

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    return build_result(
        transactions,
        [],
        {
            "bank":         "Westpac",
            "bank_id":      "westpac",
            "format":       "Business One Plus / Personal",
            "pages":        len(pages),
            "file_format":  fmt,
            "parse_time_ms": round((time.time() - t0) * 1000),
        },
    )


# ─────────────────────────── Public API ───────────────────────────────────────

def _is_format_b(first_page_text: str) -> bool:
    """
    Format B signals:
      • "Account activity" heading (case-insensitive)
      • Columns header containing "Debit" and "Credit" but NOT "Balance"
      • Date lines like "01 Jul 2025" (full month name + 4-digit year)
    """
    txt = first_page_text.lower()
    has_activity_heading = "account activity" in txt
    has_debit_credit     = ("debit" in txt) and ("credit" in txt)
    has_no_balance       = "balance" not in txt
    has_full_year_date   = bool(
        re.search(
            r"\b\d{2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\b",
            txt,
        )
    )
    return has_activity_heading and has_debit_credit and has_no_balance and has_full_year_date


def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()

    # Must contain Westpac branding
    if "westpac" not in txt:
        return 0.0

    score = 0.5  # base for having "westpac"

    if _is_format_b(first_page_text):
        # Format B specific signals
        score += 0.35
        if "westpac banking corporation" in txt or "westpac business one" in txt:
            score += 0.1
    else:
        # Format A signals
        if "business one plus" in txt:
            score += 0.3
        if "westpac banking corporation" in txt:
            score += 0.2
        if re.search(r"opening balance|closing balance", txt):
            score += 0.1

    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    """
    Auto-detect format and delegate to the correct sub-parser.
    Format B (Account Activity) → _parse_format_b()
    Format A (Business One Plus / Personal / ZIP) → _parse_format_a()
    """
    # Peek at first page text for format detection
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    except Exception:
        first_text = ""

    if _is_format_b(first_text):
        return _parse_format_b(pdf_path)
    return _parse_format_a(pdf_path)
