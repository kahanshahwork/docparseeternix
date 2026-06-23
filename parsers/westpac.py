"""
parsers/westpac.py – Westpac Bank statement parser
Handles THREE distinct Westpac PDF formats:

  FORMAT A – "Business One Plus / Personal" (legacy text PDF)
    • Date format:  DD/MM/YY
    • Columns:  Date | Description | Withdrawal | Deposit | Balance
    • Delivered as PDF or as a ZIP of .txt page files
    • Detected by: "Westpac Business One Plus" or "opening balance" in text

  FORMAT B – "Account Activity" (text PDF, no balance column)
    • Date format:  DD Mon YYYY  (e.g. "01 Jul 2025")
    • Columns:  Date | Description | Debit | Credit  (NO balance column)
    • Amounts: -$NNN.NN for debits, $NNN.NN for credits (dollar-signed)
    • Description spans multiple lines; date row may be 1–3 px offset from amount row
    • Detected by: "Account activity" heading + Debit/Credit columns + no Balance column

  FORMAT B-OCR – "Account Activity" (image / scanned PDF)
    • Same visual layout as Format B but pages are raster images, not text
    • pdfplumber extract_words() returns [] for these pages → OCR fallback
    • Tesseract OCR at 300 DPI + pixel-coordinate parsing (same logic as Format B)
    • OCR date normalisation handles common confusions: O↔0, I/l↔1, month abbreviations
    • Amount tokens stripped of leading OCR quote artefacts: '"$269.50' → -269.50
    • Confidence=0.7 (vs 1.0 for text PDF) to flag reduced certainty
    • Meta includes "ocr": True and "ocr_pages" count for transparency

Dependencies for OCR (installed on first import, soft-fail if absent):
    pip install pdf2image pytesseract Pillow
    System: tesseract-ocr + poppler-utils
"""

import re
import time
import zipfile
from collections import defaultdict, Counter

import pdfplumber

from parsers.utils import build_result, make_txn

DISPLAY_NAME = "Westpac"

# ─────────────────────────── OCR availability ─────────────────────────────────

_OCR_AVAILABLE: bool | None = None   # cached after first check


def _check_ocr() -> bool:
    global _OCR_AVAILABLE
    if _OCR_AVAILABLE is not None:
        return _OCR_AVAILABLE
    try:
        import pytesseract
        from pdf2image import convert_from_path  # noqa: F401
        pytesseract.get_tesseract_version()
        _OCR_AVAILABLE = True
    except Exception:
        _OCR_AVAILABLE = False
    return _OCR_AVAILABLE


# ─────────────────────────── Shared constants ──────────────────────────────────

# Format B (text PDF) – PDF point thresholds
_B_DATE_MAX_X         = 100   # date tokens: x0 < 100 pt
_B_DESC_MIN_X         = 100   # description tokens: x0 ≥ 100 pt
_B_AMT_MIN_X          = 330   # amount tokens: x0 > 330 pt
_B_CREDIT_MIN_X       = 415   # credit (positive) subzone: x0 > 415 pt
_B_MAX_ASSIGN_DIST_PT = 30    # max pt distance to assign desc row to anchor

# Format B-OCR – pixel thresholds at 300 DPI (= PDF pts × DPI/72)
_OCR_DPI              = 300
_OCR_SCALE            = _OCR_DPI / 72            # ≈ 4.167 px/pt
_OCR_DATE_MAX_X       = int(_B_DATE_MAX_X   * _OCR_SCALE)   # ≈ 417 px
_OCR_AMT_MIN_X        = int(_B_AMT_MIN_X    * _OCR_SCALE)   # ≈ 1375 px
_OCR_CREDIT_MIN_X     = int(_B_CREDIT_MIN_X * _OCR_SCALE)   # ≈ 1729 px
_OCR_MAX_ASSIGN_DIST  = int(_B_MAX_ASSIGN_DIST_PT * _OCR_SCALE)  # ≈ 125 px
_OCR_AMT_LOOKAHEAD_PX = 50    # max px gap between date row and offset amount row

# ─────────────────────────── Noise filters ────────────────────────────────────

_NOISE_B = [re.compile(p, re.I) for p in [
    r"^westpac\b",
    r"^account activity$",
    r"^\d{3}-\d{3}\s+\d+$",          # BSB + account number
    r"^\$\d[\d,]*\.\d{2}$",          # standalone balance amount on cover
    r"^transactions$",
    r"^date$", r"^description$", r"^debit$", r"^credit$",
    r"^date\s+description",           # column header row
    r"^copyright",
    r"^abn\s+\d",
    r"^things you should know",
    r"^the pdf report",
    r"^©",
    r"^banking corporation",
]]


def _is_noise_b(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    return any(p.search(s) for p in _NOISE_B)


# ─────────────────────────── Amount helpers ───────────────────────────────────

# Strict: matches clean PDF text amounts
_AMOUNT_RE_STRICT = re.compile(r"^-?\$[\d,]+\.\d{2}$")
# Loose: matches OCR-garbled amounts with leading/trailing quote artefacts
_AMOUNT_RE_LOOSE  = re.compile(r'^["\']?(-?\$[\d,]+\.\d{2})["\']?$')


def _parse_amount_b(text: str) -> float | None:
    """Parse '-$1,234.56' or '$1,234.56' → signed float. Negative = debit."""
    s = text.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _clean_ocr_amount(text: str) -> float | None:
    """
    Parse amount from OCR text, handling leading/trailing quote artefacts.
    e.g. '"$269.50' → -269.50  (sign preserved from original $ prefix)
    """
    m = _AMOUNT_RE_LOOSE.match(text.strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _is_amount_token_strict(text: str) -> bool:
    return bool(_AMOUNT_RE_STRICT.match(text))


def _is_amount_token_ocr(text: str) -> bool:
    return _clean_ocr_amount(text) is not None


# ─────────────────────────── Format B – text PDF parsing ─────────────────────

def _parse_page_b(page: object, page_num: int) -> list[dict]:
    """
    Parse one page of a Format B text PDF (Account Activity, no balance column).

    Layout (PDF points):
        [desc_line_1  top=N,   x0≈133]   ← first description line
        [date+amount  top=N+5, x0≈47/344] ← anchor row (date left, amount right)
        [desc_line_2  top=N+9, x0≈133]   ← continuation (optional)

    Three-pass strategy:
      Pass 1 – find anchor rows (date + amount). Handle 1–3 pt y-split between
               date token and amount token (PDF rendering artefact).
      Pass 2 – assign non-anchor desc rows to nearest anchor ≤ _B_MAX_ASSIGN_DIST pt.
      Pass 3 – assemble pre-desc + anchor-extra + post-desc → transaction dict.
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=3)

    rows: dict[int, list] = defaultdict(list)
    for w in words:
        rows[round(w["top"])].append(w)

    sorted_tops = sorted(rows.keys())

    # ── Pass 1 ──────────────────────────────────────────────────────────────
    _DATE_RE_B = re.compile(
        r"^(\d{2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$",
        re.I,
    )

    anchors: dict[int, tuple] = {}
    absorbed: set[int] = set()

    for idx, top in enumerate(sorted_tops):
        rw        = sorted(rows[top], key=lambda w: w["x0"])
        date_ws   = [w for w in rw if w["x0"] < _B_DATE_MAX_X]
        amount_ws = [w for w in rw if w["x0"] > _B_AMT_MIN_X
                     and _is_amount_token_strict(w["text"])]
        desc_ws   = [w for w in rw if _B_DESC_MIN_X <= w["x0"] <= _B_AMT_MIN_X]

        date_str = " ".join(w["text"] for w in date_ws).strip()
        dm = _DATE_RE_B.match(date_str)
        if not dm:
            continue

        if not amount_ws:
            # Amount may be 1–3 pts below on a separate PDF row
            for j in range(idx + 1, min(idx + 5, len(sorted_tops))):
                nt = sorted_tops[j]
                if nt - top > 3:
                    break
                next_amts = [w for w in sorted(rows[nt], key=lambda w: w["x0"])
                             if w["x0"] > _B_AMT_MIN_X
                             and _is_amount_token_strict(w["text"])]
                if next_amts:
                    amount_ws = next_amts
                    absorbed.add(nt)
                    break

        if not amount_ws:
            continue

        formatted_date = (
            f"{dm.group(1)}-{dm.group(2).capitalize()}-{dm.group(3)}"
        )
        amount     = _parse_amount_b(amount_ws[0]["text"])
        extra_desc = [w["text"] for w in desc_ws]
        anchors[top] = (formatted_date, amount, extra_desc)

    if not anchors:
        return []

    anchor_tops = sorted(anchors.keys())

    # ── Pass 2 ──────────────────────────────────────────────────────────────
    txn_descs: dict[int, list] = defaultdict(list)

    for top in sorted_tops:
        if top in anchors or top in absorbed:
            continue
        rw        = sorted(rows[top], key=lambda w: w["x0"])
        row_text  = " ".join(w["text"] for w in rw).strip()

        if _is_noise_b(row_text):
            continue
        # Skip pure-amount rows
        if (all(w["x0"] > _B_AMT_MIN_X for w in rw)
                and any(_is_amount_token_strict(w["text"]) for w in rw)):
            continue
        # Skip stray date-only rows
        if all(w["x0"] < _B_DATE_MAX_X for w in rw):
            nearest = min(anchor_tops, key=lambda a: abs(a - top))
            if abs(nearest - top) <= 3:
                continue

        desc_ws = [w for w in rw if w["x0"] >= _B_DESC_MIN_X]
        if not desc_ws:
            continue

        nearest = min(anchor_tops, key=lambda a: abs(a - top))
        if abs(nearest - top) <= _B_MAX_ASSIGN_DIST_PT:
            txn_descs[nearest].append((top, [w["text"] for w in desc_ws]))

    # ── Pass 3 ──────────────────────────────────────────────────────────────
    transactions = []
    for anchor_top in anchor_tops:
        formatted_date, amount, extra = anchors[anchor_top]
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
            make_txn(
                "",
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
    """Entry point for Format B text PDFs.  Falls back to OCR for image pages."""
    t0 = time.time()
    transactions: list[dict] = []
    ocr_pages = 0

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, 1):
            words = page.extract_words(x_tolerance=1, y_tolerance=3)
            if words:
                # Normal text page
                transactions.extend(_parse_page_b(page, page_num))
            else:
                # Image page → OCR fallback
                ocr_txns = _parse_page_b_ocr_single(page, page_num)
                transactions.extend(ocr_txns)
                if ocr_txns is not None:
                    ocr_pages += 1

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    meta = {
        "bank":         "Westpac",
        "bank_id":      "westpac",
        "format":       "Account Activity (Debit/Credit, no balance)",
        "pages":        page_count,
        "file_format":  "pdf",
        "parse_time_ms": round((time.time() - t0) * 1000),
    }
    if ocr_pages:
        meta["ocr"]       = True
        meta["ocr_pages"] = ocr_pages

    return build_result(transactions, [], meta)


# ─────────────────────────── Format B-OCR – image page parsing ────────────────

# OCR date normalisation helpers
_OCR_MONTHS = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
}
_OCR_MON_ABBR = {m: m.capitalize() for m in _OCR_MONTHS}
_OCR_MON_FIXES = {
    # Uppercase letter confusions (O↔0, I/l↔1, A↔4 etc.)
    "ai": "jul", "al": "jul", "ji": "jul", "jui": "jul",
    "jl": "jul", "jul,": "jul", "juj": "jul",
    "ln": "jun", "lun": "jun", "jur": "jun",
    "jn": "jun", "jun,": "jun",
    "ma": "may", "mai": "may", "mav": "may", "may,": "may",
    "jan,": "jan", "feb,": "feb", "mar,": "mar", "apr,": "apr",
    "aur": "aug", "au": "aug", "aug,": "aug",
    "se": "sep", "sei": "sep", "sep,": "sep",
    "oc": "oct", "oci": "oct", "oct,": "oct",
    "ne": "nov", "nov,": "nov",
    "de": "dec", "dec,": "dec",
}


def _ocr_fix_day(s: str) -> str:
    """Replace O→0, l/I/i→1 in a two-character day token."""
    return re.sub(r"[OolIi]", lambda c: "0" if c.group() in "Oo" else "1", s)


def _ocr_fix_mon(s: str) -> str | None:
    """Normalise OCR noise in a month token; return 3-letter capitalised or None."""
    lo = re.sub(r"[^a-zA-Z]", "", s).lower()
    if lo in _OCR_MON_FIXES:
        lo = _OCR_MON_FIXES[lo]
    if lo[:3] in _OCR_MONTHS:
        return _OCR_MON_ABBR[lo[:3]]
    return None


def _ocr_fix_year(s: str, fallback: str | None = None) -> str | None:
    """Normalise OCR noise in a year token; return 4-digit string or fallback."""
    s2 = re.sub(r"[OolI]", lambda c: "0" if c.group() in "Oo" else "1", s)
    s2 = s2.replace("Z", "2")
    if re.match(r"^20\d{2}$", s2):
        return s2
    digits = re.sub(r"\D", "", s2)
    if len(digits) == 4 and digits.startswith("20"):
        return digits
    return fallback


def _ocr_try_parse_date(date_words: list[dict], fallback_year: str | None) -> str | None:
    """
    Given OCR word dicts from the date zone, attempt to build 'DD-Mon-YYYY'.
    Applies normalisation to each token and falls back to page-level year if
    the year token is unrecognisable.
    """
    texts = [w["text"].strip() for w in date_words if w["text"].strip()]
    if len(texts) < 2:
        return None
    day = _ocr_fix_day(texts[0])
    mon = _ocr_fix_mon(texts[1]) if len(texts) > 1 else None
    yr  = _ocr_fix_year(texts[2], fallback_year) if len(texts) > 2 else fallback_year
    if not (re.match(r"^\d{2}$", day) and mon and yr):
        return None
    return f"{day}-{mon}-{yr}"


def _ocr_extract_year_fallback(ocr_words: list[dict]) -> str | None:
    """Return most common 4-digit '20XX' year seen in the date zone of a page."""
    years = [
        w["text"].strip()
        for w in ocr_words
        if w["x0"] < _OCR_DATE_MAX_X and re.match(r"^20\d{2}$", w["text"].strip())
    ]
    if years:
        return Counter(years).most_common(1)[0][0]
    return None


def _ocr_group_words_into_rows(words: list[dict], y_gap: int = 12) -> list[list[dict]]:
    """
    Group OCR word dicts into visual rows based on proximity.
    y_gap: max pixel gap between consecutive words to be considered the same row.
    Returns list of rows, each sorted by x0.
    """
    if not words:
        return []
    ws = sorted(words, key=lambda w: w["top"])
    rows: list[list[dict]] = [[ws[0]]]
    cur_top = ws[0]["top"]

    for w in ws[1:]:
        if abs(w["top"] - cur_top) <= y_gap:
            rows[-1].append(w)
            # Keep cur_top as median of current row to handle drift
            tops = sorted(cw["top"] for cw in rows[-1])
            cur_top = tops[len(tops) // 2]
        else:
            rows.append([w])
            cur_top = w["top"]

    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _parse_page_b_ocr_single(page: object, page_num: int) -> list[dict]:
    """
    OCR a single image-format page from a Format B PDF.
    Uses pdf2image to rasterise the page, then Tesseract for word-level OCR.
    Returns list of make_txn dicts (confidence=0.7).
    Returns [] silently if OCR toolchain is unavailable.
    """
    if not _check_ocr():
        return []

    try:
        import pytesseract
        from pdf2image import convert_from_path
        from PIL import Image  # noqa: F401
    except ImportError:
        return []

    # Rasterise just this one page from the PDF
    # Note: convert_from_path page indices are 1-based
    try:
        images = convert_from_path(
            page.pdf.stream.name if hasattr(page.pdf, "stream") else page.pdf._path,
            dpi=_OCR_DPI,
            first_page=page_num,
            last_page=page_num,
        )
    except Exception:
        return []

    if not images:
        return []

    img = images[0]
    try:
        data = pytesseract.image_to_data(
            img,
            output_type=pytesseract.Output.DICT,
            lang="eng",
            config="--oem 3 --psm 6",
        )
    except Exception:
        return []

    # Build word list (filter low-confidence noise)
    OCR_CONF_THRESHOLD = 20
    ocr_words = [
        {
            "text": data["text"][i].strip(),
            "x0":   data["left"][i],
            "top":  data["top"][i],
        }
        for i in range(len(data["text"]))
        if data["text"][i].strip() and int(data["conf"][i]) >= OCR_CONF_THRESHOLD
    ]

    fallback_year = _ocr_extract_year_fallback(ocr_words)

    # Group into rows
    raw_rows = _ocr_group_words_into_rows(ocr_words, y_gap=12)
    rows = []
    for rw in raw_rows:
        tops = sorted(w["top"] for w in rw)
        y    = tops[len(tops) // 2]
        rows.append({"y": y, "words": rw})
    rows.sort(key=lambda r: r["y"])

    # ── Pass 1: anchors ──────────────────────────────────────────────────────
    anchors: dict[int, tuple] = {}   # row_index → (date, amount, extra_desc, y)
    absorbed_idxs: set[int]   = set()

    for idx, row in enumerate(rows):
        rw        = row["words"]
        date_ws   = [w for w in rw if w["x0"] < _OCR_DATE_MAX_X]
        amount_ws = [w for w in rw if w["x0"] > _OCR_AMT_MIN_X
                     and _is_amount_token_ocr(w["text"])]
        desc_ws   = [w for w in rw
                     if _OCR_DATE_MAX_X <= w["x0"] <= _OCR_AMT_MIN_X]

        if not date_ws:
            continue
        formatted_date = _ocr_try_parse_date(date_ws, fallback_year)
        if not formatted_date:
            continue

        # Amount may be on a nearby row (OCR y-jitter is larger than PDF pt jitter)
        if not amount_ws:
            for j in range(idx + 1, min(idx + 6, len(rows))):
                if rows[j]["y"] - row["y"] > _OCR_AMT_LOOKAHEAD_PX:
                    break
                next_amts = [
                    w for w in rows[j]["words"]
                    if w["x0"] > _OCR_AMT_MIN_X and _is_amount_token_ocr(w["text"])
                ]
                if next_amts:
                    amount_ws = next_amts
                    absorbed_idxs.add(j)
                    break

        if not amount_ws:
            continue

        amount     = _clean_ocr_amount(amount_ws[0]["text"])
        extra_desc = [w["text"] for w in desc_ws
                      if _clean_ocr_amount(w["text"]) is None]
        anchors[idx] = (formatted_date, amount, extra_desc, row["y"])

    if not anchors:
        return []

    anchor_idxs = sorted(anchors.keys())

    # ── Pass 2: assign desc rows ─────────────────────────────────────────────
    txn_descs: dict[int, list] = defaultdict(list)

    for idx, row in enumerate(rows):
        if idx in anchors or idx in absorbed_idxs:
            continue
        rw        = row["words"]
        row_text  = " ".join(w["text"] for w in rw).strip()

        if _is_noise_b(row_text):
            continue
        # Skip pure-amount rows (even with OCR quote artefacts)
        if (all(w["x0"] > _OCR_AMT_MIN_X for w in rw)
                and any(_is_amount_token_ocr(w["text"]) for w in rw)):
            continue
        # Skip stray date-only rows near an anchor
        if all(w["x0"] < _OCR_DATE_MAX_X for w in rw):
            if any(abs(row["y"] - anchors[a][3]) <= 25 for a in anchor_idxs):
                continue

        desc_ws = [
            w for w in rw
            if w["x0"] >= _OCR_DATE_MAX_X
            and _clean_ocr_amount(w["text"]) is None   # filter stray amount tokens
        ]
        if not desc_ws:
            continue

        nearest_i = min(anchor_idxs, key=lambda a: abs(row["y"] - anchors[a][3]))
        if abs(row["y"] - anchors[nearest_i][3]) <= _OCR_MAX_ASSIGN_DIST:
            txn_descs[nearest_i].append((row["y"], [w["text"] for w in desc_ws]))

    # ── Pass 3: build transactions ───────────────────────────────────────────
    transactions = []
    for anchor_idx in anchor_idxs:
        formatted_date, amount, extra, anchor_y = anchors[anchor_idx]
        desc_rows = sorted(txn_descs[anchor_idx], key=lambda x: x[0])
        pre = []; post = []
        for row_y, wlist in desc_rows:
            if row_y < anchor_y:
                pre.extend(wlist)
            else:
                post.extend(wlist)

        all_tokens = pre + extra + post
        # Final guard: strip any stray amount tokens that slipped through
        clean_tokens = [t for t in all_tokens if _clean_ocr_amount(t) is None]
        desc = re.sub(r"\s+", " ", " ".join(clean_tokens)).strip()

        if amount is None:
            continue
        transactions.append(
            make_txn(
                "",
                formatted_date,
                desc,
                round(amount, 2),
                None,
                page_num,
                round(anchor_y / _OCR_SCALE, 1),  # convert px back to PDF pts
                confidence=0.7,                     # lower confidence for OCR path
            )
        )
    return transactions


def _parse_format_b_ocr_full(pdf_path: str) -> dict:
    """
    Entry point when ALL pages of a Format B PDF are image-format.
    Rasterises all pages at once (more efficient than per-page for multi-page PDFs)
    then parses each page.
    """
    if not _check_ocr():
        return build_result([], [], {
            "bank": "Westpac", "bank_id": "westpac",
            "format": "Account Activity (image) – OCR unavailable",
            "error": "Install pdf2image, pytesseract, and tesseract-ocr for image PDF support.",
        })

    import pytesseract
    from pdf2image import convert_from_path

    t0 = time.time()
    try:
        images = convert_from_path(pdf_path, dpi=_OCR_DPI)
    except Exception as e:
        return build_result([], [], {
            "bank": "Westpac", "bank_id": "westpac",
            "format": "Account Activity (image)",
            "error": str(e),
        })

    transactions: list[dict] = []

    for page_num, img in enumerate(images, 1):
        try:
            data = pytesseract.image_to_data(
                img,
                output_type=pytesseract.Output.DICT,
                lang="eng",
                config="--oem 3 --psm 6",
            )
        except Exception:
            continue

        OCR_CONF_THRESHOLD = 20
        ocr_words = [
            {"text": data["text"][i].strip(), "x0": data["left"][i], "top": data["top"][i]}
            for i in range(len(data["text"]))
            if data["text"][i].strip() and int(data["conf"][i]) >= OCR_CONF_THRESHOLD
        ]

        fallback_year = _ocr_extract_year_fallback(ocr_words)
        raw_rows      = _ocr_group_words_into_rows(ocr_words, y_gap=12)
        rows = []
        for rw in raw_rows:
            tops = sorted(w["top"] for w in rw)
            rows.append({"y": tops[len(tops) // 2], "words": rw})
        rows.sort(key=lambda r: r["y"])

        # ── Pass 1 ──────────────────────────────────────────────────────────
        anchors: dict[int, tuple] = {}
        absorbed: set[int] = set()

        for idx, row in enumerate(rows):
            rw        = row["words"]
            date_ws   = [w for w in rw if w["x0"] < _OCR_DATE_MAX_X]
            amount_ws = [w for w in rw if w["x0"] > _OCR_AMT_MIN_X
                         and _is_amount_token_ocr(w["text"])]
            desc_ws   = [w for w in rw
                         if _OCR_DATE_MAX_X <= w["x0"] <= _OCR_AMT_MIN_X]

            if not date_ws:
                continue
            formatted_date = _ocr_try_parse_date(date_ws, fallback_year)
            if not formatted_date:
                continue

            if not amount_ws:
                for j in range(idx + 1, min(idx + 6, len(rows))):
                    if rows[j]["y"] - row["y"] > _OCR_AMT_LOOKAHEAD_PX:
                        break
                    na = [w for w in rows[j]["words"]
                          if w["x0"] > _OCR_AMT_MIN_X and _is_amount_token_ocr(w["text"])]
                    if na:
                        amount_ws = na
                        absorbed.add(j)
                        break

            if not amount_ws:
                continue

            amount = _clean_ocr_amount(amount_ws[0]["text"])
            extra  = [w["text"] for w in desc_ws if _clean_ocr_amount(w["text"]) is None]
            anchors[idx] = (formatted_date, amount, extra, row["y"])

        if not anchors:
            continue

        anchor_idxs = sorted(anchors.keys())

        # ── Pass 2 ──────────────────────────────────────────────────────────
        txn_descs: dict[int, list] = defaultdict(list)

        for idx, row in enumerate(rows):
            if idx in anchors or idx in absorbed:
                continue
            rw       = row["words"]
            row_text = " ".join(w["text"] for w in rw).strip()
            if _is_noise_b(row_text):
                continue
            if (all(w["x0"] > _OCR_AMT_MIN_X for w in rw)
                    and any(_is_amount_token_ocr(w["text"]) for w in rw)):
                continue
            if all(w["x0"] < _OCR_DATE_MAX_X for w in rw):
                if any(abs(row["y"] - anchors[a][3]) <= 25 for a in anchor_idxs):
                    continue
            desc_ws = [w for w in rw
                       if w["x0"] >= _OCR_DATE_MAX_X
                       and _clean_ocr_amount(w["text"]) is None]
            if not desc_ws:
                continue
            nearest_i = min(anchor_idxs, key=lambda a: abs(row["y"] - anchors[a][3]))
            if abs(row["y"] - anchors[nearest_i][3]) <= _OCR_MAX_ASSIGN_DIST:
                txn_descs[nearest_i].append((row["y"], [w["text"] for w in desc_ws]))

        # ── Pass 3 ──────────────────────────────────────────────────────────
        for anchor_idx in anchor_idxs:
            formatted_date, amount, extra, anchor_y = anchors[anchor_idx]
            desc_rows = sorted(txn_descs[anchor_idx], key=lambda x: x[0])
            pre = []; post = []
            for row_y, wlist in desc_rows:
                if row_y < anchor_y: pre.extend(wlist)
                else:                post.extend(wlist)
            all_tokens   = pre + extra + post
            clean_tokens = [t for t in all_tokens if _clean_ocr_amount(t) is None]
            desc = re.sub(r"\s+", " ", " ".join(clean_tokens)).strip()
            if amount is None:
                continue
            transactions.append(
                make_txn(
                    "",
                    formatted_date,
                    desc,
                    round(amount, 2),
                    None,
                    page_num,
                    round(anchor_y / _OCR_SCALE, 1),
                    confidence=0.7,
                )
            )

    for i, txn in enumerate(transactions):
        txn["transaction_id"] = f"westpac_{i + 1:04d}"

    return build_result(transactions, [], {
        "bank":         "Westpac",
        "bank_id":      "westpac",
        "format":       "Account Activity (image/scanned, OCR)",
        "pages":        len(images),
        "file_format":  "pdf",
        "ocr":          True,
        "ocr_pages":    len(images),
        "parse_time_ms": round((time.time() - t0) * 1000),
    })


# ─────────────────────────── FORMAT A (legacy text/ZIP) ───────────────────────

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
    return not s or any(p.search(s) for p in _NOISE_A)


def _parse_float_a(s: str) -> float | None:
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _keyword_sign(desc: str, raw: float) -> float:
    if _CREDIT_KW.match(desc): return raw
    if _DEBIT_KW.match(desc):  return -abs(raw)
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
            remainder      = m.group(2).strip()
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


def _extract_opening_balance_a(text: str) -> float | None:
    for line in text.splitlines():
        m = re.search(r"Opening Balance\s*\+?\s*\$?([\d,]+\.\d{2})", line, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


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
        "format":       "Business One Plus / Personal",
        "pages":        len(pages),
        "file_format":  fmt,
        "parse_time_ms": round((time.time() - t0) * 1000),
    })


# ─────────────────────────── Format detection ─────────────────────────────────

def _is_format_b_text(first_page_text: str) -> bool:
    """
    Format B (text PDF): "Account activity" heading + Debit/Credit columns
    + no Balance column + DD Mon YYYY dates.
    """
    txt = first_page_text.lower()
    return (
        "account activity" in txt
        and "debit" in txt
        and "credit" in txt
        and "balance" not in txt
        and bool(re.search(
            r"\b\d{2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\b",
            txt,
        ))
    )


def _is_format_b_image(pdf_path: str) -> bool:
    """
    Format B-OCR: first page has no extractable text but PDF metadata or
    visual content suggests an Account Activity statement.
    Heuristic: open first page; if extract_words() is empty, it's an image page.
    We then peek at OCR output to confirm it looks like Account Activity.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return False
            words = pdf.pages[0].extract_words(x_tolerance=1, y_tolerance=3)
            if words:
                return False   # has text → not image format
            # No text → image page; try a quick OCR fingerprint
            if not _check_ocr():
                return False
            text = pdf.pages[0].extract_text() or ""
            # text will be empty for image pages, but let's try OCR snippet
    except Exception:
        return False

    # Try OCR on first page to detect "Account activity" heading
    try:
        from pdf2image import convert_from_path
        import pytesseract
        imgs = convert_from_path(pdf_path, dpi=150, first_page=1, last_page=1)
        if not imgs:
            return False
        # Only OCR top 30% of page to find header quickly
        w, h = imgs[0].size
        header = imgs[0].crop((0, 0, w, int(h * 0.3)))
        ocr_text = pytesseract.image_to_string(header, config="--oem 3 --psm 6")
        lo = ocr_text.lower()
        return "account" in lo and ("activity" in lo or "westpac" in lo)
    except Exception:
        return False


# ─────────────────────────── Public API ───────────────────────────────────────

def can_parse(first_page_text: str, page_count: int) -> float:
    txt = first_page_text.lower()
    if "westpac" not in txt:
        return 0.0

    score = 0.5   # base score for Westpac branding

    if _is_format_b_text(first_page_text):
        score += 0.35
        if "westpac banking corporation" in txt or "westpac business one" in txt:
            score += 0.1
    elif not first_page_text.strip():
        # Possibly image PDF – give moderate score, parse() will confirm
        score += 0.2
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
    Auto-detect Westpac format and route to the correct sub-parser:
      Format B text PDF  → _parse_format_b()          (word-coordinate parsing)
      Format B image PDF → _parse_format_b_ocr_full() (Tesseract OCR at 300 DPI)
      Format A (all else)→ _parse_format_a()           (text-line regex, or ZIP)
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_text  = pdf.pages[0].extract_text() or "" if pdf.pages else ""
            first_words = pdf.pages[0].extract_words(x_tolerance=1, y_tolerance=3) if pdf.pages else []
    except Exception:
        first_text  = ""
        first_words = []

    if _is_format_b_text(first_text):
        return _parse_format_b(pdf_path)

    if not first_words:
        # First page has no extractable text → image PDF
        # Confirm it looks like Account Activity before committing to OCR
        if _is_format_b_image(pdf_path):
            return _parse_format_b_ocr_full(pdf_path)

    return _parse_format_a(pdf_path)
