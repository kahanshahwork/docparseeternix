"""
parsers/ocr_parser.py – Generic OCR parser for image-format bank statements.

Handles any PDF where pages contain raster images instead of selectable text
(scanned documents, phone photos saved as PDF, image-only exports).

What it does:
  1. Detects image pages (pdfplumber extract_words() returns [])
  2. Rasterises each image page at 300 DPI using pdf2image
  3. Runs Tesseract OCR to get word-level text + bounding boxes
  4. Attempts to find Date | Description | Amount column structure
  5. Returns raw transactions with confidence=0.6 (OCR quality indicator)

What it does NOT do:
  - Bank-specific column detection or format routing
  - Balance-delta verification (no balance column assumed for now)
  - Any post-processing — transactions go straight to the Approve screen

The goal of this parser is simply: does the file upload work, does the bank
get detected, and do raw transactions appear on screen? Bank-specific OCR
parsers (e.g. westpac_ocr.py) can be added later once the pipeline is proven.

Dependencies:
    pip install pdf2image pytesseract Pillow
    System: tesseract-ocr  poppler-utils
"""

import re
import time
from collections import defaultdict

import pdfplumber

from parsers.utils import build_result, make_txn

DISPLAY_NAME = "OCR (Image PDF)"

# ─────────────────────────── Availability check ───────────────────────────────

def _ocr_available() -> bool:
    try:
        import pytesseract
        from pdf2image import convert_from_path  # noqa: F401
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# ─────────────────────────── Detection ───────────────────────────────────────

def can_parse(first_page_text: str, page_count: int) -> float:
    """
    Score high ONLY when the first page has no extractable text (image page).
    Score 0 when text is present — a text-based parser should handle it instead.
    """
    if first_page_text.strip():
        return 0.0          # text PDF → let the bank-specific parser handle it
    if not _ocr_available():
        return 0.0          # OCR stack not installed
    return 0.55             # image page with OCR available → moderate confidence


# ─────────────────────────── OCR helpers ──────────────────────────────────────

_DATE_PATTERNS = [
    # DD Mon YYYY  →  01 Jul 2025
    re.compile(
        r"^(\d{2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})$",
        re.I,
    ),
    # DD/MM/YYYY or DD/MM/YY
    re.compile(r"^(\d{2})/(\d{2})/(\d{2,4})$"),
    # DD-MM-YYYY
    re.compile(r"^(\d{2})-(\d{2})-(\d{2,4})$"),
]

_AMOUNT_RE = re.compile(r"^-?\$?[\d,]+\.\d{2}$")

_MONTH_MAP = {
    "jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
    "may": "May", "jun": "Jun", "jul": "Jul", "aug": "Aug",
    "sep": "Sep", "oct": "Oct", "nov": "Nov", "dec": "Dec",
}


def _parse_date_token(text: str) -> str | None:
    """Try to parse a single text token as a date. Returns DD-Mon-YYYY or None."""
    t = text.strip()
    for pat in _DATE_PATTERNS:
        m = pat.match(t)
        if m:
            g = m.groups()
            # DD Mon YYYY
            if len(g) == 3 and re.match(r"[A-Za-z]", g[1]):
                return f"{g[0].zfill(2)}-{g[1].capitalize()}-{g[2]}"
            # Numeric: DD/MM/YY or DD/MM/YYYY
            if len(g) == 3:
                dd, mm, yy = g
                yr = f"20{yy}" if len(yy) == 2 else yy
                try:
                    from datetime import datetime
                    dt = datetime(int(yr), int(mm), int(dd))
                    return dt.strftime("%d-%b-%Y")
                except ValueError:
                    pass
    return None


def _parse_amount_token(text: str) -> float | None:
    """Parse a monetary amount token. Returns signed float or None."""
    t = text.strip().replace(",", "").replace("$", "")
    try:
        return float(t)
    except ValueError:
        return None


def _group_words_into_rows(words: list[dict], y_gap: int = 12) -> list[dict]:
    """
    Group OCR word dicts by vertical proximity.
    Returns list of row dicts: {y, words: [...]}.
    """
    if not words:
        return []
    ws = sorted(words, key=lambda w: w["top"])
    rows = [[ws[0]]]
    cur_top = ws[0]["top"]

    for w in ws[1:]:
        if abs(w["top"] - cur_top) <= y_gap:
            rows[-1].append(w)
            tops = sorted(cw["top"] for cw in rows[-1])
            cur_top = tops[len(tops) // 2]
        else:
            rows.append([w])
            cur_top = w["top"]

    result = []
    for rw in rows:
        tops = sorted(w["top"] for w in rw)
        result.append({
            "y":     tops[len(tops) // 2],
            "words": sorted(rw, key=lambda w: w["x0"]),
        })
    return sorted(result, key=lambda r: r["y"])


def _detect_column_zones(rows: list[dict]) -> dict:
    """
    Heuristically detect x-coordinate zones for Date, Description, Amount columns
    by looking for known header keywords (Date, Description, Debit, Credit, Amount).
    Falls back to rough thirds of the page width if no headers found.
    """
    header_row = None
    for row in rows:
        texts = [w["text"].lower() for w in row["words"]]
        joined = " ".join(texts)
        if "date" in texts and any(k in joined for k in ("debit", "credit", "amount", "withdrawal")):
            header_row = row
            break

    if header_row:
        zones = {}
        for w in header_row["words"]:
            t = w["text"].lower()
            if t == "date":
                zones["date_x"] = w["x0"]
            elif t in ("description", "particulars", "details", "narrative"):
                zones["desc_x"] = w["x0"]
            elif t in ("debit", "withdrawal", "amount"):
                zones["amt_x"] = w["x0"]
            elif t in ("credit", "deposit") and "amt_x" not in zones:
                zones["amt_x"] = w["x0"]
        if "date_x" in zones and "amt_x" in zones:
            return zones

    # Fallback: estimate from page width
    all_x = [w["x0"] for row in rows for w in row["words"]]
    if not all_x:
        return {"date_x": 0, "desc_x": 200, "amt_x": 1200}
    max_x = max(all_x)
    return {
        "date_x": 0,
        "desc_x": max_x * 0.15,
        "amt_x":  max_x * 0.72,
    }


def _parse_page_ocr(image, page_num: int) -> list[dict]:
    """
    OCR one rasterised page image and extract transactions.
    Returns list of make_txn dicts with confidence=0.6.
    """
    import pytesseract

    data = pytesseract.image_to_data(
        image,
        output_type=pytesseract.Output.DICT,
        lang="eng",
        config="--oem 3 --psm 6",
    )

    # Build word list, filter very low confidence
    ocr_words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        if not txt or int(data["conf"][i]) < 15:
            continue
        ocr_words.append({
            "text": txt,
            "x0":   data["left"][i],
            "top":  data["top"][i],
            "x1":   data["left"][i] + data["width"][i],
        })

    rows = _group_words_into_rows(ocr_words, y_gap=12)
    zones = _detect_column_zones(rows)

    date_x = zones.get("date_x", 0)
    desc_x = zones.get("desc_x", zones.get("date_x", 0) + 200)
    amt_x  = zones.get("amt_x", desc_x + 800)

    # Tolerance bands
    date_max = desc_x - 5
    desc_max = amt_x - 5

    transactions = []
    cur_date = None
    cur_desc_parts = []
    cur_amount = None
    cur_top = 0.0

    def flush():
        nonlocal cur_date, cur_desc_parts, cur_amount, cur_top
        if cur_date and cur_amount is not None:
            desc = re.sub(r"\s+", " ", " ".join(cur_desc_parts)).strip()
            transactions.append(
                make_txn("", cur_date, desc, round(cur_amount, 2),
                         None, page_num, cur_top / (300 / 72),
                         confidence=0.6)
            )
        cur_date = None
        cur_desc_parts = []
        cur_amount = None
        cur_top = 0.0

    for row in rows:
        date_ws  = [w for w in row["words"] if w["x0"] <= date_max]
        desc_ws  = [w for w in row["words"] if desc_x - 20 <= w["x0"] <= desc_max]
        amt_ws   = [w for w in row["words"] if w["x0"] >= amt_x - 20
                    and _AMOUNT_RE.match(w["text"])]

        # Check for date
        date_candidate = " ".join(w["text"] for w in date_ws).strip()
        parsed_date = _parse_date_token(date_candidate)

        if parsed_date:
            flush()
            cur_date  = parsed_date
            cur_top   = row["y"]
            cur_desc_parts = [w["text"] for w in desc_ws]
            if amt_ws:
                cur_amount = _parse_amount_token(amt_ws[0]["text"])
        elif cur_date:
            # Continuation row
            if desc_ws:
                cur_desc_parts.extend(w["text"] for w in desc_ws)
            if amt_ws and cur_amount is None:
                cur_amount = _parse_amount_token(amt_ws[0]["text"])

    flush()
    return transactions


# ─────────────────────────── Public API ───────────────────────────────────────

def parse(pdf_path: str) -> dict:
    """
    OCR-parse an image-format PDF.
    Processes every page that has no extractable text.
    Text pages are skipped (a real text parser should handle them).
    """
    t0 = time.time()

    if not _ocr_available():
        return build_result([], [], {
            "bank":     "Unknown (OCR)",
            "bank_id":  "ocr_parser",
            "format":   "Image PDF",
            "error":    "OCR not available. Install: pip install pdf2image pytesseract "
                        "and system packages: tesseract-ocr poppler-utils",
        })

    from pdf2image import convert_from_path

    transactions = []
    ocr_pages    = 0

    try:
        images     = convert_from_path(pdf_path, dpi=300)
        page_count = len(images)
    except Exception as e:
        return build_result([], [], {
            "bank":    "Unknown (OCR)",
            "bank_id": "ocr_parser",
            "format":  "Image PDF",
            "error":   str(e),
        })

    # Also check which pages actually have text (skip those)
    text_page_nums = set()
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                if page.extract_words(x_tolerance=1, y_tolerance=3):
                    text_page_nums.add(i)
    except Exception:
        pass

    for page_num, img in enumerate(images, 1):
        if page_num in text_page_nums:
            continue   # text page — let the bank-specific parser handle it
        txns = _parse_page_ocr(img, page_num)
        transactions.extend(txns)
        ocr_pages += 1

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"ocr_{i + 1:04d}"

    return build_result(transactions, [], {
        "bank":         "Unknown (OCR)",
        "bank_id":      "ocr_parser",
        "format":       "Image PDF (OCR)",
        "pages":        page_count,
        "ocr_pages":    ocr_pages,
        "ocr":          True,
        "file_format":  "pdf",
        "parse_time_ms": round((time.time() - t0) * 1000),
    })
