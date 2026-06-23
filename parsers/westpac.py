"""
parsers/westpac.py – Westpac Bank statement parser
Handles TWO layout formats, detected by column structure (not account name):

  FORMAT A – Balance-column layout (legacy)
    • Columns: Date | Description | Withdrawal | Deposit | Balance
    • Date:    DD/MM/YY
    • Delivered as PDF or ZIP of .txt page files
    • Key signal: extract_text() contains "opening balance" / "closing balance"
      and dates match DD/MM/YY

  FORMAT B – Debit/Credit layout (no balance column)
    • Columns: Date | Description | Debit | Credit
    • Date:    DD Mon YYYY  (e.g. "01 Jul 2025")
    • Amount tokens include $ sign: -$269.50 / $25000.00
    • Key signal: "debit" and "credit" in header row, no "balance" column,
      dates match DD Mon YYYY pattern

Image/scanned PDFs are NOT handled here.
Use parsers/ocr_parser.py for image-format statements of any bank.
"""

import re
import time
import zipfile
from collections import defaultdict

import pdfplumber

from parsers.utils import build_result, make_txn

DISPLAY_NAME = "Westpac"


# ─────────────────────────── FORMAT B ─────────────────────────────────────────
# Debit/Credit layout: no balance column, DD Mon YYYY dates, $ amounts

_DATE_RE_B   = re.compile(
    r"^(\d{2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$",
    re.I,
)
_AMOUNT_RE_B = re.compile(r"^-?\$[\d,]+\.\d{2}$")

_NOISE_B = [re.compile(p, re.I) for p in [
    r"^westpac\b",
    r"^account activity$",
    r"^\d{3}-\d{3}\s+\d+$",
    r"^\$\d[\d,]*\.\d{2}$",
    r"^transactions$",
    r"^date$", r"^description$", r"^debit$", r"^credit$",
    r"^date\s+description",
    r"^copyright", r"^abn\s+\d",
    r"^things you should know", r"^the pdf report", r"^©",
    r"^banking corporation",
]]


def _is_noise_b(text: str) -> bool:
    s = text.strip()
    return not s or any(p.search(s) for p in _NOISE_B)


def _parse_amount_b(text: str) -> float | None:
    s = text.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_page_b(page, page_num: int) -> list[dict]:
    """
    Three-pass word-coordinate parser for Format B pages.

    Pass 1 — find anchor rows: rows with a valid DD Mon YYYY date (x0<100 pt)
              paired with a signed dollar amount (x0>330 pt). Handles 1-3 pt
              y-split between date token and amount token.
    Pass 2 — assign every other description row to its nearest anchor within
              30 pt.
    Pass 3 — assemble pre-desc + anchor-extra + post-desc → transaction.
    """
    DATE_MAX_X   = 100
    AMT_MIN_X    = 330
    MAX_DIST     = 30

    words = page.extract_words(x_tolerance=1, y_tolerance=3)
    rows: dict[int, list] = defaultdict(list)
    for w in words:
        rows[round(w["top"])].append(w)

    sorted_tops = sorted(rows.keys())

    # Pass 1
    anchors: dict[int, tuple] = {}
    absorbed: set[int] = set()

    for idx, top in enumerate(sorted_tops):
        rw       = sorted(rows[top], key=lambda w: w["x0"])
        date_ws  = [w for w in rw if w["x0"] < DATE_MAX_X]
        amt_ws   = [w for w in rw if w["x0"] > AMT_MIN_X
                    and _AMOUNT_RE_B.match(w["text"])]
        desc_ws  = [w for w in rw if DATE_MAX_X <= w["x0"] <= AMT_MIN_X]

        date_str = " ".join(w["text"] for w in date_ws).strip()
        dm = _DATE_RE_B.match(date_str)
        if not dm:
            continue

        if not amt_ws:
            for j in range(idx + 1, min(idx + 5, len(sorted_tops))):
                nt = sorted_tops[j]
                if nt - top > 3:
                    break
                next_amts = [w for w in sorted(rows[nt], key=lambda w: w["x0"])
                             if w["x0"] > AMT_MIN_X and _AMOUNT_RE_B.match(w["text"])]
                if next_amts:
                    amt_ws = next_amts
                    absorbed.add(nt)
                    break

        if not amt_ws:
            continue

        fmt_date   = f"{dm.group(1)}-{dm.group(2).capitalize()}-{dm.group(3)}"
        amount     = _parse_amount_b(amt_ws[0]["text"])
        extra_desc = [w["text"] for w in desc_ws]
        anchors[top] = (fmt_date, amount, extra_desc)

    if not anchors:
        return []

    anchor_tops = sorted(anchors.keys())

    # Pass 2
    txn_descs: dict[int, list] = defaultdict(list)

    for top in sorted_tops:
        if top in anchors or top in absorbed:
            continue
        rw       = sorted(rows[top], key=lambda w: w["x0"])
        row_text = " ".join(w["text"] for w in rw).strip()

        if _is_noise_b(row_text):
            continue
        if (all(w["x0"] > AMT_MIN_X for w in rw)
                and any(_AMOUNT_RE_B.match(w["text"]) for w in rw)):
            continue
        if all(w["x0"] < DATE_MAX_X for w in rw):
            if abs(min(anchor_tops, key=lambda a: abs(a - top)) - top) <= 3:
                continue

        desc_ws = [w for w in rw if w["x0"] >= DATE_MAX_X]
        if not desc_ws:
            continue

        nearest = min(anchor_tops, key=lambda a: abs(a - top))
        if abs(nearest - top) <= MAX_DIST:
            txn_descs[nearest].append((top, [w["text"] for w in desc_ws]))

    # Pass 3
    transactions = []
    for anchor_top in anchor_tops:
        fmt_date, amount, extra = anchors[anchor_top]
        desc_rows = sorted(txn_descs[anchor_top], key=lambda x: x[0])
        pre = []; post = []
        for row_top, wlist in desc_rows:
            if row_top < anchor_top:
                pre.extend(wlist)
            else:
                post.extend(wlist)

        desc = re.sub(r"\s+", " ", " ".join(pre + extra + post)).strip()
        if amount is None:
            continue
        transactions.append(
            make_txn("", fmt_date, desc, round(amount, 2), None, page_num,
                     float(anchor_top), confidence=1.0)
        )
    return transactions


def _parse_format_b(pdf_path: str) -> dict:
    t0 = time.time()
    transactions = []
    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, 1):
            transactions.extend(_parse_page_b(page, page_num))

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    return build_result(transactions, [], {
        "bank":         "Westpac",
        "bank_id":      "westpac",
        "format":       "Debit/Credit (no balance column)",
        "pages":        page_count,
        "file_format":  "pdf",
        "parse_time_ms": round((time.time() - t0) * 1000),
    })


# ─────────────────────────── FORMAT A ─────────────────────────────────────────
# Balance-column layout: DD/MM/YY dates, Balance column present

_DATE_RE_A  = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$")
_NUMBER_RE  = re.compile(r"-?[\d,]+\.\d{2}")
_SKIP_RE    = re.compile(
    r"^(STATEMENT OPENING BALANCE|CLOSING BALANCE|OPENING BALANCE)", re.I
)

_CREDIT_KW = re.compile(
    r"^(Deposit|ATM Deposit|Promotional Fee Rebate|Debit Card Refund)", re.I
)
_DEBIT_KW  = re.compile(
    r"^(Withdrawal|Debit Card Purchase|Eftpos Debit|Payment By Authority|"
    r"Monthly Plan Fee|Overdrawn Fee|ATM Operator Fee|Withdrawal At|Withdrawal Mobile)",
    re.I,
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
    return not s or any(p.search(s) for p in _NOISE_A)


def _pf(s: str) -> float | None:
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _keyword_sign(desc: str, raw: float) -> float:
    if _CREDIT_KW.match(desc): return raw
    if _DEBIT_KW.match(desc):  return -abs(raw)
    return -abs(raw)


def _parse_txn_a(date_str, full_desc, page_num, prev_balance, top):
    from parsers.utils import sign_from_balance_delta
    full_desc = full_desc.strip()
    if _SKIP_RE.match(full_desc):
        return None
    matches = list(_NUMBER_RE.finditer(full_desc))
    if len(matches) < 2:
        return None
    balance = _pf(matches[-1].group())
    txn_raw = _pf(matches[-2].group())
    if txn_raw is None or balance is None:
        return None
    desc = re.sub(r"\s+", " ", full_desc[:matches[-2].start()].strip())
    desc = re.sub(
        r"\s+\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*$",
        "", desc, flags=re.I,
    ).strip()
    signed = sign_from_balance_delta(balance, prev_balance[0], txn_raw)
    if abs(abs(signed) - abs(txn_raw)) > 0.02:
        signed = _keyword_sign(desc, txn_raw)
    try:
        from datetime import datetime as _dt
        pd = _dt.strptime(date_str, "%d/%m/%y").strftime("%d-%b-%Y")
    except ValueError:
        pd = date_str
    return make_txn("", pd, desc, round(signed, 2), balance, page_num, top)


def _parse_page_text_a(text, page_num, prev_balance):
    lines   = [l.rstrip("\r") for l in text.replace("\r\n", "\n").split("\n")]
    results = []
    cur_date = cur_lines = None
    cur_top  = 0.0

    def flush():
        if cur_date and cur_lines:
            txn = _parse_txn_a(cur_date, " ".join(cur_lines), page_num,
                                prev_balance, cur_top)
            if txn:
                prev_balance[0] = txn["balance"]
                results.append(txn)

    for line in lines:
        s = line.strip()
        if not s or _is_noise_a(s):
            continue
        m = _DATE_RE_A.match(s)
        if m:
            remainder = m.group(2).strip()
            if bool(re.match(r"^[-\d,.\s]+$", remainder)) and cur_date:
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


def _extract_opening_balance_a(text):
    for line in text.splitlines():
        m = re.search(r"Opening Balance\s*\+?\s*\$?([\d,]+\.\d{2})", line, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _detect_file_format(path):
    try:
        with zipfile.ZipFile(path, "r"):
            return "zip"
    except zipfile.BadZipFile:
        return "pdf"


def _read_pages_zip(path):
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


def _read_pages_pdf_a(path):
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            pages.append((i, page.extract_text() or ""))
    return pages


def _parse_format_a(pdf_path: str) -> dict:
    t0  = time.time()
    fmt = _detect_file_format(pdf_path)
    pages = _read_pages_zip(pdf_path) if fmt == "zip" else _read_pages_pdf_a(pdf_path)

    opening      = _extract_opening_balance_a(pages[0][1]) if pages else None
    prev_balance = [opening]
    transactions = []

    for page_num, text in pages:
        transactions.extend(_parse_page_text_a(text, page_num, prev_balance))

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    return build_result(transactions, [], {
        "bank":         "Westpac",
        "bank_id":      "westpac",
        "format":       "Balance-column (DD/MM/YY dates)",
        "pages":        len(pages),
        "file_format":  fmt,
        "parse_time_ms": round((time.time() - t0) * 1000),
    })


# ─────────────────────────── Layout detection ─────────────────────────────────

def _detect_layout(first_page_text: str) -> str:
    """
    Detect layout purely from column structure, not account name.

    Format B signals (Debit/Credit layout):
      - Headers 'debit' and 'credit' appear on the page
      - No 'balance' column header
      - Dates follow DD Mon YYYY pattern (full month name, 4-digit year)

    Format A signals (Balance-column layout):
      - 'balance' appears in headers
      - OR dates follow DD/MM/YY pattern (2-digit year)

    Returns 'B', 'A', or 'unknown'.
    """
    txt = first_page_text.lower()

    has_debit_credit = ("debit" in txt) and ("credit" in txt)
    has_balance      = bool(re.search(r"\bbalance\b", txt))
    has_dmy_date     = bool(re.search(r"\b\d{2}/\d{2}/\d{2}\b", txt))
    has_mon_date     = bool(re.search(
        r"\b\d{2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\b", txt
    ))

    if has_debit_credit and not has_balance and has_mon_date:
        return "B"
    if has_balance or has_dmy_date:
        return "A"
    return "unknown"


# ─────────────────────────── Public API ───────────────────────────────────────

def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    if "westpac" not in txt:
        return 0.0

    score  = 0.5
    layout = _detect_layout(first_page_text)

    if layout == "B":
        score += 0.45
    elif layout == "A":
        score += 0.45
    else:
        # Westpac branding but unrecognised layout — low confidence
        score += 0.1

    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    """
    Detect layout from column structure and route to the correct sub-parser.
    Image/scanned PDFs are out of scope here — use ocr_parser.py for those.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    except Exception:
        first_text = ""

    layout = _detect_layout(first_text)

    if layout == "B":
        return _parse_format_b(pdf_path)
    return _parse_format_a(pdf_path)
