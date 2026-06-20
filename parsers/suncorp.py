"""
parsers/suncorp.py - Suncorp Bank Statement Parser (Ultimate v2.0 Base)

STATEMENT FORMATS: Everyday Options | Business Everyday | Business Premium | Sub-Account | Term Deposit
  - Column boundaries are AUTO-DETECTED from the header row on each page.
  - Numbers are right-aligned — classified by x1 (right edge).
  - Integrates advanced OCR cleaning for comma-decimals ("736,91"), double dots ("9.239.77"),
    and split numeric fragments ("1" + ",643.00").
  - Silently intercepts in-table Opening Balances to preserve ground-truth math.
"""

import re
import time
import pdfplumber
from typing import Optional, Tuple

# OCR fallback for scanned/image-only Suncorp statements (no embedded text layer).
# Optional: if pytesseract/pdf2image aren't installed, OCR fallback is simply skipped
# and such pages parse to zero transactions, same as previous behaviour.
try:
    import pytesseract
    from pdf2image import convert_from_path
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

_OCR_DPI = 300

# ── Patterns ─────────────────────────────────────────────────────────────────

# Year is made optional to handle dates physically split across multiple lines
DATE_RE = re.compile(
    r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s+(\d{2,4}))?$",
    re.I
)

# Sub-lines to exclude from the final transaction description
# CRITICAL: 'FROM', 'TO', and 'REFERENCE' are intentionally excluded from this list 
# so important transaction data isn't destroyed.
SUBLINE_RE = re.compile(
    r"^("
    r"EFFECTIVE\s+DATE|"
    r"BALANCE\s+(BROUGHT|CARRIED)\s+FORWARD|"
    r"CLOSING\s+BALANCE|OPENING\s+BALANCE|"
    r"Statement\s+No|Page\s+\d+\s+of|"
    r"Suncorp\s+Bank|The\s+SUNCORP|Details\s+are\s+continued|"
    r"part\s+of\s+the\s+Suncorp|"
    r"Account\s+Transactions|Date\s+Transaction|Withdrawal\s+Deposit|"
    r"Summary\s+of\s+Interest|Interest\s+(Paid|Charged)|"
    r"Bank\s+Fees|Government(\s+Duties)?|Withholding|ATM\s+Operator|"
    r"Fees\s+and\s+Charges\s+on|This\s+Period|1\s+(July|April|Jan)\s+to|"
    r"Notes:|Please\s+(retain|check)|Should\s+you\s+have|"
    r"Complaints|If\s+we\s+didn|For\s+information\s+on\s+our|"
    r"complaint\s+processes|Australian\s+Financial|"
    r"www\.|calling\s+\d|emailing\s+|sending\s+a\s+letter|"
    r"this\s+statement\s+for|all\s+entries\s+on|please\s+call|"
    r"meet\s+your\s+expectations|and\s+Feedback|lodge\s+a\s+complaint|"
    r"\$[\d,]+|"       
    r"\d{8,}"          
    r")",
    re.I
)

NOISE_FULL_RE = re.compile(
    r"^("
    r"\d{4}|"                                        
    r"BALANCE\s+(BROUGHT|CARRIED)\s+FORWARD|"
    r"CLOSING\s+BALANCE|OPENING\s+BALANCE|"
    r"Account\s+Transactions(\s+Continued)?|"
    r"Date\s+Transaction\s+Details|"
    r"Withdrawal\s+Deposit\s+Balance|"
    r"\d{2}\s+\d{2}\s+\d{2}|"                       
    r"suncorpbank\.com\.au|"
    r"Suncorp\s+Bank|The\s+SUNCORP"
    r")$",
    re.I
)

FOOTER_SENTINEL_RE = re.compile(
    r"^(Statement\s+No|Details\s+are\s+continued|"
    r"Suncorp\s+Bank\s+\(Norfina|"
    r"Summary\s+of\s+Interest|"
    r"Notes:|Complaints\s+and\s+Feedback)",
    re.I
)


# ── Robust OCR Number Handlers ────────────────────────────────────────────────

def _clean_num(s: str) -> str:
    s = s.strip().rstrip("-").rstrip(".")
    # Strip dot-leader fill characters (used in Term Deposit / dotted-fill
    # layouts, e.g. "9,746.63.................." or "....9,746.63")
    s = re.sub(r"^\.{2,}", "", s)
    s = re.sub(r"\.{2,}$", "", s)
    return s

def _is_amount(s: str) -> bool:
    """Validates amounts, absorbing comma-decimals (736,91) and double-dots (9.239.77)"""
    c = _clean_num(s)
    if len(c) >= 3 and c[-3] in [',', '.']:
        main = c[:-3].replace(",", "").replace(".", "")
        dec = c[-2:]
        return (main.isdigit() or main == "") and dec.isdigit()
    return False

def _is_balance_str(s: str) -> bool:
    return _is_amount(s)

def _parse_amount(s: str) -> Optional[float]:
    if not s: return None
    c = _clean_num(s)
    if len(c) >= 3 and c[-3] in [',', '.']:
        main = c[:-3].replace(",", "").replace(".", "")
        dec = c[-2:]
        try:
            return round(float(f"{main}.{dec}"), 2)
        except ValueError:
            return None
    return None

def _parse_balance(s: str) -> Optional[float]:
    if not s: return None
    s_str = s.strip()
    overdrawn = s_str.endswith("-")
    c = _clean_num(s_str)
    if len(c) >= 3 and c[-3] in [',', '.']:
        main = c[:-3].replace(",", "").replace(".", "")
        dec = c[-2:]
        try:
            val = round(float(f"{main}.{dec}"), 2)
            return -val if overdrawn else val
        except ValueError:
            return None
    return None

def _is_subline(text: str) -> bool:
    t = text.strip()
    return not t or bool(SUBLINE_RE.match(t))

def _is_noise(text: str) -> bool:
    t = text.strip()
    return not t or bool(NOISE_FULL_RE.match(t))


# ── Adaptive column detector ──────────────────────────────────────────────────

def _detect_columns(words: list) -> Optional[dict]:
    rows = _group_words_by_row(words, y_tol=2.0)
    date_x0 = desc_x0 = with_x0 = dep_x0 = bal_x0 = None
    header_top = None

    for row in rows:
        texts = [w["text"] for w in row]
        if "Withdrawal" in texts and "Deposit" in texts and "Balance" in texts:
            for w in row:
                t = w["text"]
                if t == "Date":
                    date_x0 = w["x0"]
                    header_top = w["top"]
                elif t == "Transaction":
                    desc_x0 = w["x0"]
                elif t == "Withdrawal":
                    with_x0 = w["x0"]
                elif t == "Deposit":
                    dep_x0 = w["x0"]
                elif t == "Balance":
                    bal_x0 = w["x0"]
            break

    if with_x0 is None or dep_x0 is None or bal_x0 is None:
        return None

    footer_top = 9999.0
    for w in words:
        if FOOTER_SENTINEL_RE.match(w["text"].strip()):
            if w["top"] < footer_top:
                footer_top = w["top"]
    if footer_top == 9999.0:
        footer_top = 780.0

    return {
        "date_x0":    date_x0   or 0,
        "desc_x0":    desc_x0   or (date_x0 + 60 if date_x0 else 100),
        "with_x0":    with_x0,
        "dep_x0":     dep_x0,
        "bal_x0":     bal_x0,
        "header_top": header_top or 0,
        "footer_top": footer_top,
    }


# ── Row grouping & Fixing ─────────────────────────────────────────────────────

def _split_dotted_amounts(words: list) -> list:
    """
    Some Suncorp layouts (Term Deposit Interest Advice) use a dotted fill
    leader between the description and amount, and between adjacent amount
    columns, e.g. a single extracted "word" can be:
        "............9,746.63..................................270,000.00"
    containing TWO separate amounts glued together by a run of dots.
    This splits any such token into multiple word-dicts, each with an
    x0 estimated from its character offset within the original token
    (dot characters are ~3px wide, digit/comma/period chars are ~5px wide
    in the fonts observed — close enough for column classification, since
    column boundaries are not pixel-critical here).
    """
    result = []
    for w in words:
        t = w["text"]
        if "." * 3 not in t:
            result.append(w)
            continue
        # Only attempt splitting if it looks like dot-leader-separated numbers
        parts = re.split(r"\.{3,}", t)
        parts = [p for p in parts if p.strip()]
        if not parts:
            # Pure dot-leader fill with no embedded number — drop it
            continue
        if len(parts) == 1:
            # A single number with dot-leader fill attached (leading and/or
            # trailing) — keep it as one word but with cleaned text, and
            # recompute x1 proportionally so it doesn't appear far wider
            # than it really is (the trailing dots would otherwise push it
            # past the next column's boundary).
            total_len = len(t)
            x0, x1 = w["x0"], w.get("x1", w["x0"] + len(t) * 5)
            span = x1 - x0
            idx = t.find(parts[0])
            frac_start = idx / total_len if total_len else 0
            frac_end = (idx + len(parts[0])) / total_len if total_len else 1
            new_w = dict(w)
            new_w["text"] = parts[0]
            new_w["x0"] = x0 + span * frac_start
            new_w["x1"] = x0 + span * frac_end
            result.append(new_w)
            continue
        # Estimate each part's x position proportionally across the token's
        # width based on character offset (good enough for column bucketing)
        total_len = len(t)
        x0, x1 = w["x0"], w.get("x1", w["x0"] + len(t) * 5)
        span = x1 - x0
        cursor = 0
        for part in parts:
            idx = t.find(part, cursor)
            if idx < 0:
                idx = cursor
            frac_start = idx / total_len if total_len else 0
            new_w = dict(w)
            new_w["text"] = part
            new_w["x0"] = x0 + span * frac_start
            new_w["x1"] = new_w["x0"] + max(len(part) * 5, 10)
            result.append(new_w)
            cursor = idx + len(part)
    return result


_DECORATIVE_GLYPH_RE = re.compile(r"^i+$")  # repeated 'i' glyphs used as a horizontal rule/underline


def _is_decorative_row(row: list) -> bool:
    """True if a row consists entirely of single-character 'i' glyphs spaced
    out as a horizontal rule (a font quirk in some Suncorp Term Deposit PDFs).
    """
    texts = [w["text"] for w in row]
    if not texts:
        return False
    return all(_DECORATIVE_GLYPH_RE.match(t) for t in texts)


def _group_words_by_row(words: list, y_tol: float = 3.5) -> list:
    if not words: return []
    rows, cur_row, cur_top = [], [words[0]], words[0]["top"]
    for w in words[1:]:
        if abs(w["top"] - cur_top) <= y_tol:
            cur_row.append(w)
        else:
            rows.append(sorted(cur_row, key=lambda x: x["x0"]))
            cur_row = [w]
            cur_top = w["top"]
    if cur_row:
        rows.append(sorted(cur_row, key=lambda x: x["x0"]))
    return rows

def _merge_numeric_fragments(row: list, zone_x0: float) -> list:
    """Fixes pdfplumber splitting '1' and ',643.46' into separate tokens"""
    if not row: return row
    result = []
    i = 0
    while i < len(row):
        w = row[i]
        if w["x0"] >= zone_x0 and i + 1 < len(row):
            nxt = row[i + 1]
            gap = nxt["x0"] - w.get("x1", w["x0"] + 10)
            if gap < 12:
                t1 = w["text"].strip()
                t2 = nxt["text"].strip()
                if re.match(r"^[\d,\.\-]+$", t1) and re.match(r"^[\d,\.\-]+$", t2):
                    merged = dict(w)
                    merged["text"] = t1 + t2
                    merged["x1"] = nxt.get("x1", nxt["x0"] + 10)
                    result.append(merged)
                    i += 2
                    continue
        result.append(w)
        i += 1
    return result

def _row_to_columns(row: list, cols: dict) -> dict:
    date_words, desc_words, with_words, dep_words, bal_words = [], [], [], [], []

    with_x0 = cols["with_x0"]
    dep_x0  = cols["dep_x0"]
    bal_x0  = cols["bal_x0"]
    desc_x0 = cols["desc_x0"]
    ref_cutoff = bal_x0 + 100

    # Drop decorative underline glyphs that may have merged into this row
    row = [w for w in row if not _DECORATIVE_GLYPH_RE.match(w["text"])]

    row = _merge_numeric_fragments(row, with_x0 - 20)

    for w in row:
        x0  = w["x0"]
        x1  = w.get("x1", x0 + 40)
        t   = w["text"]

        if x0 >= ref_cutoff: continue

        is_num = _is_amount(t) or _is_balance_str(t)

        if is_num and x0 >= with_x0:
            if x1 >= bal_x0: bal_words.append(t)
            elif x1 > dep_x0: dep_words.append(t)
            else: with_words.append(t)
        elif x0 < desc_x0 - 1.5: date_words.append(t)
        elif x0 < with_x0:
            if not (is_num and x0 >= with_x0) and not re.match(r"^\.{2,}$", t):
                desc_words.append(t)

    return {
        "date":       " ".join(date_words).strip(),
        "desc":       " ".join(desc_words).strip(),
        "withdrawal": " ".join(with_words).strip(),
        "deposit":    " ".join(dep_words).strip(),
        "balance":    " ".join(bal_words).strip(),
    }


# ── Page parser ───────────────────────────────────────────────────────────────

def _ocr_extract_words(pil_image, page_height_pts: float, page_width_pts: float) -> list:
    """
    Run Tesseract OCR on a rasterized page image and return a word list in
    the same shape pdfplumber's extract_words() produces (x0/x1/top/text),
    scaled from pixel coordinates back into PDF point space so it can be fed
    through the existing column-detection and row-parsing pipeline unchanged.
    """
    data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
    scale_x = pil_image.width / page_width_pts
    scale_y = pil_image.height / page_height_pts

    words = []
    n = len(data["text"])
    for i in range(n):
        txt = data["text"][i].strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 20:  # discard very low-confidence noise
            continue
        x0 = data["left"][i] / scale_x
        top = data["top"][i] / scale_y
        x1 = x0 + data["width"][i] / scale_x
        bottom = top + data["height"][i] / scale_y
        words.append({"text": txt, "x0": x0, "x1": x1, "top": top, "bottom": bottom})
    return words


def _get_page_words(page, page_num: int, pdf_path: Optional[str] = None,
                     ocr_cache: Optional[dict] = None) -> list:
    """
    Returns the word list for a page, using pdfplumber's native text
    extraction when available, and falling back to OCR for scanned /
    image-only pages (no embedded text layer).
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=3)
    if words:
        return words

    if not _OCR_AVAILABLE or not pdf_path:
        return []

    if ocr_cache is not None and "pages" not in ocr_cache:
        try:
            ocr_cache["pages"] = convert_from_path(pdf_path, dpi=_OCR_DPI)
        except Exception:
            ocr_cache["pages"] = []

    rasterized = ocr_cache.get("pages", []) if ocr_cache else []
    if page_num - 1 >= len(rasterized):
        return []

    pil_image = rasterized[page_num - 1]
    return _ocr_extract_words(pil_image, page.height, page.width)


def _parse_page(page, page_num: int, carry_date: str, carry_desc_parts: list,
                prev_balance: list, fallback_cols: Optional[dict],
                pdf_path: Optional[str] = None, ocr_cache: Optional[dict] = None) -> Tuple[list, str, list, Optional[dict]]:
    words = _get_page_words(page, page_num, pdf_path, ocr_cache)
    if not words:
        return [], carry_date, carry_desc_parts, fallback_cols

    cols = _detect_columns(words)
    if cols is None: cols = fallback_cols
    if cols is None: return [], carry_date, carry_desc_parts, None

    footer_top = cols["footer_top"]
    if footer_top < page.height:
        # Native text layer: crop to drop footer text before re-extracting
        cropped = page.crop((0, 0, page.width, footer_top - 1))
        words = cropped.extract_words(x_tolerance=1, y_tolerance=3)
        if not words and _OCR_AVAILABLE:
            # OCR words were used for column detection but page.crop()/
            # extract_words() on the original (image) page yields nothing —
            # re-use the already-extracted OCR words and filter by footer_top.
            words = [w for w in _get_page_words(page, page_num, pdf_path, ocr_cache)
                     if w["top"] < footer_top - 1]
    words = _split_dotted_amounts(words)
    rows = _group_words_by_row(words)

    results     = []
    last_date   = carry_date
    desc_parts  = carry_desc_parts[:]
    pending_date    = carry_date if carry_desc_parts else ""
    pending_top     = None
    pending_with    = ""
    pending_dep     = ""
    pending_balance = ""

    def flush():
        nonlocal desc_parts, pending_date, pending_top, pending_with, pending_dep, pending_balance
        if not pending_date:
            desc_parts = []; pending_with = pending_dep = pending_balance = ""
            return
            
        desc  = " ".join(p for p in desc_parts if p).strip()
        w_amt = _parse_amount(pending_with)
        d_amt = _parse_amount(pending_dep)
        bal   = _parse_balance(pending_balance)

        if (w_amt is None and d_amt is None) or bal is None:
            desc_parts = []; pending_date = pending_with = pending_dep = pending_balance = ""; pending_top = None
            return

        if w_amt is not None and d_amt is not None:
            signed = round(bal - prev_balance[0], 2) if prev_balance[0] is not None else -w_amt
        elif w_amt is not None:
            signed = -w_amt
        else:
            signed = d_amt

        if prev_balance[0] is not None:
            delta = round(bal - prev_balance[0], 2)
            if abs(delta - signed) > 0.02:
                signed = delta   # trust the ground-truth balance walk

        prev_balance[0] = bal
        if desc:
            results.append({
                "transaction_id": "",
                "date":        pending_date,
                "description": desc,
                "amount":      round(signed, 2),
                "balance":     bal,
                "source_page": page_num,
                "row_top":     pending_top if pending_top is not None else 0,
            })
        desc_parts = []; pending_date = pending_with = pending_dep = pending_balance = ""
        pending_top = None

    for row in rows:
        if cols["header_top"] and row[0]["top"] <= cols["header_top"]:
            continue
        if _is_decorative_row(row):
            continue

        c    = _row_to_columns(row, cols)
        date = c["date"].strip()
        desc = c["desc"].strip()
        w_str = c["withdrawal"].strip()
        d_str = c["deposit"].strip()
        b_str = c["balance"].strip()

        w_valid = _is_amount(w_str)
        d_valid = _is_amount(d_str)
        b_valid = _is_balance_str(b_str)
        has_amount = w_valid or d_valid

        # SILENT INTERCEPT: Grabs embedded opening balances without creating ghost transactions.
        # CRITICAL: flush any pending transaction FIRST — otherwise overwriting
        # prev_balance here corrupts the delta math for the transaction that's
        # still waiting to be flushed (e.g. a CLOSING BALANCE row immediately
        # following the last transaction on a page).
        if re.match(r"^(OPENING\s+BALANCE|BALANCE\s+(BROUGHT|CARRIED)\s+FORWARD|CLOSING\s+BALANCE)", desc, re.I):
            if pending_with or pending_dep:
                flush()
            if b_valid:
                prev_balance[0] = _parse_balance(b_str)

        dm = DATE_RE.match(date) if date else None
        if dm:
            day, month, year = dm.group(1), dm.group(2), dm.group(3)
            if not year:
                # Inherit decoupled year from the last known date
                m_yr = re.search(r"\d{4}", last_date)
                year = m_yr.group(0) if m_yr else "2025"
            date = f"{day} {month.capitalize()} {year}"

        if not date and not desc and not has_amount and not b_valid: continue
        if _is_noise(desc) and not date and not has_amount: continue

        if dm and has_amount and b_valid:
            flush()
            last_date = pending_date = date
            pending_top = row[0]["top"]
            pending_with    = w_str if w_valid else ""
            pending_dep     = d_str if d_valid else ""
            pending_balance = b_str
            desc_parts = [desc] if desc and not _is_subline(desc) and not _is_noise(desc) else []

        elif dm and not has_amount:
            flush() # Vital for handling multi-line transactions safely
            last_date = pending_date = date
            pending_top = row[0]["top"]
            desc_parts = [desc] if desc and not _is_subline(desc) and not _is_noise(desc) else []

        elif has_amount and b_valid and not dm:
            if pending_with or pending_dep:
                flush() 
                pending_date = last_date
                pending_top = row[0]["top"]
                pending_with = w_str if w_valid else ""
                pending_dep  = d_str if d_valid else ""
                pending_balance = b_str
                desc_parts = [desc] if desc and not _is_subline(desc) and not _is_noise(desc) else []
            else:
                pending_date = last_date
                if pending_top is None: pending_top = row[0]["top"]
                if w_valid: pending_with = w_str
                if d_valid: pending_dep = d_str
                if b_valid: pending_balance = b_str
                if desc and not _is_subline(desc) and not _is_noise(desc):
                    desc_parts.append(desc)

        else:
            if desc and not _is_subline(desc) and not _is_noise(desc):
                if pending_date or desc_parts:
                    desc_parts.append(desc)

    if pending_with or pending_dep:
        flush()
        carry_forward = []
    else:
        carry_forward = desc_parts

    return results, last_date, carry_forward, cols


# ── External Interface & Meta ─────────────────────────────────────────────────

def _extract_metadata(text: str) -> dict:
    meta = {
        "bank":              "Suncorp",
        "bank_id":           "suncorp",
        "format":            "Everyday Options Statement",
        "account_name":      None,
        "bsb":               None,
        "account_number":    None,
        "opening_balance":   None,
        "closing_balance":   None,
    }

    m = re.search(r"^(MR|MRS|MS|DR)\s+[A-Z]", text, re.M)
    if m:
        meta["account_name"] = text[m.start():text.find("\n", m.start())].strip()
    else:
        lines = text.split("\n")
        for line in lines[:10]:
            if "PTY" in line.upper() or "LTD" in line.upper():
                meta["account_name"] = line.strip()
                break

    m = re.search(r"BSB(?: Number)?\s+([\d-]+)", text, re.I)
    if m: meta["bsb"] = m.group(1).strip()

    m = re.search(r"Account(?: Number| No:?)\s+(\d+)", text, re.I)
    if m: meta["account_number"] = m.group(1).strip()

    m = re.search(r"Opening\s+Balance\s+\$?([\d,.]+\d{2}-?)", text, re.I)
    if m:
        meta["opening_balance"] = _parse_balance(m.group(1))
    else:
        m = re.search(r"BALANCE\s+BROUGHT\s+FORWARD\s+\$?([\d,.]+\d{2}-?)", text, re.I)
        if m: meta["opening_balance"] = _parse_balance(m.group(1))

    m = re.search(r"Closing\s+Balance\s+\$?([\d,.]+\d{2}-?)", text, re.I)
    if m: meta["closing_balance"] = _parse_balance(m.group(1))
    
    lower_text = text.lower()
    if "business everyday" in lower_text:
        meta["format"] = "Business Everyday Statement"
    elif "business premium" in lower_text:
        meta["format"] = "Business Premium Statement"
    elif "sub-account" in lower_text:
        meta["format"] = "Sub-Account Statement"
    elif "fixed term deposit" in lower_text:
        meta["format"] = "Term Deposit Interest Advice"

    return meta

DISPLAY_NAME = "Suncorp"

def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    score = 0.0
    if "suncorp" in txt: score += 0.55
    if "suncorpbank.com.au" in txt or "suncorp.com.au" in txt: score += 0.25
    if re.search(r"everyday options|business everyday|business premium|sub-account|fixed term deposit", txt): score += 0.2
    return min(score, 1.0)

def parse(file_path: str) -> dict:
    t0 = time.time()
    meta = {}
    transactions = []
    prev_balance = [None]
    carry_date   = ""
    carry_desc   = []
    last_cols    = None
    ocr_cache    = {}

    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)
        page1_text = pdf.pages[0].extract_text() or ""

        if not page1_text.strip() and _OCR_AVAILABLE:
            # Scanned page 1 — OCR it just for metadata extraction (account
            # name, BSB, opening/closing balance, statement format).
            try:
                ocr_cache["pages"] = convert_from_path(file_path, dpi=_OCR_DPI)
                page1_text = pytesseract.image_to_string(ocr_cache["pages"][0])
            except Exception:
                page1_text = ""

        meta = _extract_metadata(page1_text)
        meta["pages"] = page_count

        if meta.get("opening_balance") is not None:
            prev_balance[0] = meta["opening_balance"]

        for i, page in enumerate(pdf.pages, start=1):
            txns, carry_date, carry_desc, last_cols = _parse_page(
                page, i, carry_date, carry_desc, prev_balance, last_cols,
                pdf_path=file_path, ocr_cache=ocr_cache,
            )
            transactions.extend(txns)

    for idx, t in enumerate(transactions):
        t["transaction_id"] = f"suncorp_{idx+1:04d}"

    meta["parse_time_ms"] = round((time.time() - t0) * 1000)

    return {
        "transactions": transactions,
        "ambiguous":    [],
        "meta":         meta
    }