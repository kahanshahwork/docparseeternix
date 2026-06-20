"""
column_detect.py
Find table header row and compute column boundaries dynamically.
Handles multi-row headers where words may be at slightly different y positions.
"""
import re


def find_header_row(words: list,
                    required_any: list,
                    required_all: list = None,
                    y_tol: float = 5.0) -> list | None:
    """
    Find a header row by looking for required keyword(s).
    Groups words within y_tol of each other into candidate rows.
    Returns the merged sorted word list of the best matching row-band.
    """
    if not words:
        return None

    req_any = [r.lower() for r in required_any]
    req_all = [r.lower() for r in (required_all or [])]

    # Group into row-bands
    sorted_words = sorted(words, key=lambda w: w["top"])
    bands = []
    cur_band = [sorted_words[0]]
    for w in sorted_words[1:]:
        if abs(w["top"] - cur_band[0]["top"]) <= y_tol:
            cur_band.append(w)
        else:
            bands.append(cur_band)
            cur_band = [w]
    bands.append(cur_band)

    # Merge adjacent bands that together form the header
    # (some headers span 2 rows like "Withdrawal" on row1 and "Deposit" on row2)
    for i, band in enumerate(bands):
        # Try merging with next band too
        candidates = [band]
        if i + 1 < len(bands) and abs(bands[i+1][0]["top"] - band[0]["top"]) <= y_tol * 3:
            candidates = [band + bands[i+1]]
        candidates.append(band)

        for candidate in candidates:
            texts = " ".join(w["text"] for w in candidate).lower()
            has_any = any(r in texts for r in req_any)
            has_all = all(r in texts for r in req_all)
            if has_any and has_all:
                return sorted(candidate, key=lambda x: x["x0"])

    return None


def midpoints(header_row: list, col_names: list) -> dict:
    """
    Match header words to col_names, compute midpoint-based x boundaries.
    Returns {col_name: (x_start, x_end)}.
    """
    matched = []
    for name in col_names:
        best = None
        for w in header_row:
            if name.lower() in w["text"].lower():
                if best is None or w["x0"] < best["x0"]:
                    best = w
        if best:
            matched.append((name, best["x0"], best.get("x1", best["x0"] + 60)))

    matched.sort(key=lambda x: x[1])

    bounds = {}
    for i, (name, x0, x1_header) in enumerate(matched):
        lo = 0.0 if i == 0 else (matched[i-1][1] + x0) / 2
        hi = 9999.0 if i == len(matched)-1 else (x0 + matched[i+1][1]) / 2
        bounds[name] = (lo, hi)

    return bounds


def classify_row_by_bounds_with_x1(row: list, bounds: dict,
                                    amount_cols: list = None) -> dict:
    """
    Classify words using x0 normally, but x1 (right edge) for numeric tokens
    in amount_cols — handles right-aligned numbers.
    """
    NUM_RE = re.compile(r"^[\d,]+(?:\.\d{2})?-?$")
    amount_cols_set = set(amount_cols or [])

    result = {name: [] for name in bounds}

    for w in row:
        x0 = w["x0"]
        x1 = w.get("x1", x0 + 50)
        txt = w["text"]
        txt_clean = txt.replace(",", "").rstrip("-").lstrip("$")
        is_num = bool(NUM_RE.match(txt_clean)) and txt_clean

        placed = False

        # Try x1 first for numeric tokens in amount columns
        if is_num and amount_cols_set:
            for name in amount_cols_set:
                if name in bounds:
                    lo, hi = bounds[name]
                    # Use centre of number (average of x0 and x1) for robustness
                    centre = (x0 + x1) / 2
                    if lo <= centre < hi:
                        result[name].append(txt)
                        placed = True
                        break

        if not placed:
            for name, (lo, hi) in bounds.items():
                if lo <= x0 < hi:
                    result[name].append(txt)
                    break

    return {name: " ".join(words).strip() for name, words in result.items()}
