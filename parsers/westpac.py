"""
parsers/westpac.py – Westpac Bank statement parser
Handles Westpac Business One Plus / personal statements (PDF and ZIP formats).
"""

import re
import time
import zipfile
import pdfplumber
from parsers.utils import (
    parse_amount, make_date, detect_year_from_text,
    sign_from_balance_delta, build_result, make_txn,
)

_DATE_RE   = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$")
_NUMBER_RE = re.compile(r"-?[\d,]+\.\d{2}")
_SKIP_RE   = re.compile(r"^(STATEMENT OPENING BALANCE|CLOSING BALANCE|OPENING BALANCE)", re.I)

_CREDIT_KW = re.compile(r"^(Deposit|ATM Deposit|Promotional Fee Rebate|Debit Card Refund)", re.I)
_DEBIT_KW  = re.compile(
    r"^(Withdrawal|Debit Card Purchase|Eftpos Debit|Payment By Authority|"
    r"Monthly Plan Fee|Overdrawn Fee|ATM Operator Fee|Withdrawal At|Withdrawal Mobile)", re.I
)

_NOISE = [re.compile(p, re.I) for p in [
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


def _is_noise(line: str) -> bool:
    s = line.strip()
    if not s: return True
    return any(p.search(s) for p in _NOISE)


def _parse_float(s: str):
    try: return float(s.replace(",", "").strip())
    except ValueError: return None


def _keyword_sign(desc: str, raw: float) -> float:
    if _CREDIT_KW.match(desc): return raw
    if _DEBIT_KW.match(desc):  return -abs(raw)
    return -abs(raw)


def _parse_txn(date_str: str, full_desc: str, page_num: int,
               prev_balance: list, top: float) -> dict | None:
    full_desc = full_desc.strip()
    if _SKIP_RE.match(full_desc): return None

    matches = list(_NUMBER_RE.finditer(full_desc))
    if len(matches) < 2: return None

    balance = _parse_float(matches[-1].group())
    txn_raw = _parse_float(matches[-2].group())
    if txn_raw is None or balance is None: return None

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


def _parse_page_text(text: str, page_num: int, prev_balance: list) -> list:
    lines   = [l.rstrip("\r") for l in text.replace("\r\n", "\n").split("\n")]
    results = []
    cur_date = cur_lines = None
    cur_top  = 0.0

    def flush():
        if cur_date and cur_lines:
            txn = _parse_txn(cur_date, " ".join(cur_lines), page_num, prev_balance, cur_top)
            if txn:
                prev_balance[0] = txn["balance"]
                results.append(txn)

    for line in lines:
        s = line.strip()
        if not s or _is_noise(s): continue
        m = _DATE_RE.match(s)
        if m:
            remainder = m.group(2).strip()
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


def _extract_opening_balance(text: str):
    for line in text.splitlines():
        m = re.search(r"Opening Balance\s*\+?\s*\$?([\d,]+\.\d{2})", line, re.I)
        if m:
            try: return float(m.group(1).replace(",", ""))
            except ValueError: pass
    return None


def _detect_format(path: str) -> str:
    try:
        with zipfile.ZipFile(path, "r"): return "zip"
    except zipfile.BadZipFile: return "pdf"


def _read_pages_zip(path: str) -> list:
    pages = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") or name == "manifest.json": continue
            try: pnum = int(name.replace(".txt", ""))
            except ValueError: continue
            pages.append((pnum, zf.open(name).read().decode("utf-8", errors="replace")))
    return sorted(pages)


def _read_pages_pdf(path: str) -> list:
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            pages.append((i, page.extract_text() or ""))
    return pages


def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    score = 0.0
    if "westpac" in txt:
        score += 0.5
    if "business one plus" in txt:
        score += 0.3
    if "westpac banking corporation" in txt:
        score += 0.2
    if re.search(r"opening balance|closing balance", txt):
        score += 0.1
    return min(score, 1.0)


def parse(pdf_path: str) -> dict:
    t0  = time.time()
    fmt = _detect_format(pdf_path)

    if fmt == "zip":
        pages = _read_pages_zip(pdf_path)
    else:
        pages = _read_pages_pdf(pdf_path)

    opening = _extract_opening_balance(pages[0][1]) if pages else None
    prev_balance = [opening]

    transactions = []
    for page_num, text in pages:
        transactions.extend(_parse_page_text(text, page_num, prev_balance))

    for i, t in enumerate(transactions):
        t["transaction_id"] = f"westpac_{i+1:04d}"

    return build_result(transactions, [], {
        "bank": "Westpac", "bank_id": "westpac", "format": "Business One Plus / Personal",
        "pages": len(pages), "file_format": fmt,
        "parse_time_ms": round((time.time() - t0) * 1000),
    })
