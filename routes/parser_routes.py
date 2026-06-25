"""
routes/parser_routes.py — Module 1: Statement Processing Engine (HTTP layer).

Supported upload formats:
  .pdf / .zip  — passed straight through
  .png / .jpg / .jpeg — converted to A4 PDF before detection/parsing
  Files with no extension — detected by MIME type + magic bytes fallback
"""

import os, time, uuid, base64, tempfile, threading, io
import pdfplumber
from flask import Blueprint, request, jsonify, send_from_directory, redirect
from detector import registry

parser_bp = Blueprint("parser", __name__)

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()
_TTL = 1800  # 30 min

_IMAGE_EXTS   = {".png", ".jpg", ".jpeg"}
_PDF_EXTS     = {".pdf", ".zip"}
_ALL_EXTS     = _PDF_EXTS | _IMAGE_EXTS

_IMAGE_MIMES  = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/tiff"}

_MAGIC = [
    (b"\x89\x50\x4e\x47", "image"),   # PNG
    (b"\xff\xd8\xff",      "image"),   # JPEG
    (b"\x47\x49\x46\x38", "image"),   # GIF
    (b"\x52\x49\x46\x46", "image"),   # WebP
    (b"\x25\x50\x44\x46", "pdf"),     # PDF
    (b"\x50\x4b\x03\x04", "zip"),     # ZIP
]


def _save(path: str) -> str:
    token = uuid.uuid4().hex
    with _LOCK:
        _STORE[token] = {"path": path, "expires": time.time() + _TTL}
    return token


def _load(token: str) -> str | None:
    with _LOCK:
        e = _STORE.get(token)
        if not e or time.time() > e["expires"]:
            if e:
                del _STORE[token]
            return None
        return e["path"]


def _cleanup():
    now = time.time()
    with _LOCK:
        dead = [k for k, v in _STORE.items() if now > v["expires"]]
        for k in dead:
            try:
                os.unlink(_STORE[k]["path"])
            except OSError:
                pass
            del _STORE[k]


def _ext(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1]


def _sniff_file_type(f) -> str:
    ext = _ext(f.filename or "")
    if ext in _IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".zip":
        return "zip"

    mime = (f.mimetype or f.content_type or "").lower().split(";")[0].strip()
    if mime in _IMAGE_MIMES:
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime in ("application/zip", "application/x-zip-compressed"):
        return "zip"

    header = f.stream.read(8)
    f.stream.seek(0)
    for magic, ftype in _MAGIC:
        if header[:len(magic)] == magic:
            return ftype

    return "unknown"


def _image_to_pdf(image_path: str) -> str:
    pdf_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf_tmp.close()
    try:
        import img2pdf
        a4 = (img2pdf.in_to_pt(8.27), img2pdf.in_to_pt(11.69))
        layout = img2pdf.get_layout_fun(a4)
        with open(image_path, "rb") as img_f, open(pdf_tmp.name, "wb") as out_f:
            out_f.write(img2pdf.convert(img_f, layout_fun=layout))
    except Exception:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        a4_px = (int(8.27 * 300), int(11.69 * 300))
        img = img.resize(a4_px, Image.LANCZOS)
        img.save(pdf_tmp.name, format="PDF", resolution=300)
    return pdf_tmp.name


def _save_upload(f) -> tuple[str, bool]:
    ftype = _sniff_file_type(f)

    if ftype == "unknown":
        raise ValueError("Unsupported file type. Upload a PDF, ZIP, PNG, or JPG.")

    if ftype == "image":
        ext = _ext(f.filename or "") or ".png"
        img_tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        f.save(img_tmp.name)
        img_tmp.close()
        try:
            pdf_path = _image_to_pdf(img_tmp.name)
        finally:
            try:
                os.unlink(img_tmp.name)
            except OSError:
                pass
        return pdf_path, True

    suffix = ".zip" if ftype == "zip" else ".pdf"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.save(tmp.name)
    tmp.close()
    return tmp.name, False


# ── Routes ─────────────────────────────────────────────────────────────────

@parser_bp.route("/")
def index():
    """Landing page."""
    return send_from_directory(APP_ROOT, "landing.html")


@parser_bp.route("/app")
def app_page():
    """Main application."""
    return send_from_directory(APP_ROOT, "index.html")


@parser_bp.route("/split-view")
def split_view():
    """Split window view for side-by-side statement comparison."""
    return send_from_directory(APP_ROOT, "split.html")


@parser_bp.route("/workflow")
def workflow_page():
    return redirect("/app")


@parser_bp.route("/parsers")
def list_parsers():
    return jsonify(registry.list_parsers())


@parser_bp.route("/detect", methods=["POST"])
def detect():
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["pdf"]
    try:
        tmp_path, is_image = _save_upload(f)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Could not process file: {e}"}), 500

    result = registry.detect(tmp_path)
    result["tmp_token"] = _save(tmp_path)
    if is_image:
        result["source_type"] = "image"
    return jsonify(result)


@parser_bp.route("/parse", methods=["POST"])
def parse():
    if request.content_type and "application/json" in request.content_type:
        body      = request.json or {}
        tmp_token = body.get("tmp_token")
        bank_id   = body.get("bank_id")
    else:
        tmp_token = request.form.get("tmp_token")
        bank_id   = request.form.get("bank_id")

    tmp_path  = None
    owns_file = False

    if tmp_token:
        tmp_path = _load(tmp_token)
        if not tmp_path:
            return jsonify({"error": "Session expired — please re-upload the file"}), 400
    elif "pdf" in request.files:
        f = request.files["pdf"]
        try:
            tmp_path, _ = _save_upload(f)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Could not process file: {e}"}), 500
        owns_file = True
        tmp_token = _save(tmp_path)
    else:
        return jsonify({"error": "No file or session token"}), 400

    if not bank_id:
        det = registry.detect(tmp_path)
        bank_id = det.get("bank_id")
        if not bank_id:
            return jsonify({
                "error": "Could not detect bank format — please select manually",
                "all_scores": det.get("all_scores", []),
            }), 422

    try:
        result = registry.parse(tmp_path, bank_id)
        result["tmp_token"] = tmp_token
        result["bank_id"]   = bank_id
        _cleanup()
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    finally:
        if owns_file:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@parser_bp.route("/pdf_page")
def pdf_page():
    token    = request.args.get("tmp_token", "")
    page_num = int(request.args.get("page", "1"))
    highlight  = request.args.get("highlight", "")
    highlights = request.args.get("highlights", "")

    tmp_path = _load(token)
    if not tmp_path:
        return jsonify({"error": "Session expired"}), 400

    try:
        with pdfplumber.open(tmp_path) as pdf:
            total = len(pdf.pages)
            if page_num < 1 or page_num > total:
                return jsonify({"error": f"Page {page_num} out of range (1-{total})"}), 400

            page = pdf.pages[page_num - 1]
            img  = page.to_image(resolution=150)

            from PIL import Image, ImageDraw
            base    = img.original.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            draw    = ImageDraw.Draw(overlay)
            scale   = 150 / 72

            if highlights:
                try:
                    tops = [float(x) for x in highlights.split(",") if x]
                    for top in tops:
                        y = int(top * scale)
                        draw.rectangle(
                            [(0, max(0, y - 7)), (overlay.width, y + 21)],
                            fill=(37, 99, 235, 40), outline=(37, 99, 235, 200), width=1,
                        )
                except Exception:
                    pass

            if highlight:
                try:
                    tops = [float(x) for x in highlight.split(",") if x]
                    for top in tops:
                        y = int(top * scale)
                        draw.rectangle(
                            [(0, max(0, y - 7)), (overlay.width, y + 21)],
                            fill=(217, 119, 6, 70), outline=(217, 119, 6, 255), width=2,
                        )
                except Exception:
                    pass

            composited = Image.alpha_composite(base, overlay).convert("RGB")
            buf = io.BytesIO()
            composited.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

        return jsonify({"image": b64, "page": page_num, "total_pages": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@parser_bp.route("/statements/import-csv", methods=["POST"])
def import_csv_excel():
    """
    Import transactions from a CSV or Excel file.
    Accepts: .csv, .xlsx, .xls
    Expects columns (case-insensitive, flexible naming):
      Date, Description, and either:
        a) Debit + Credit (separate columns)
        b) Amount (single column, negative = debit)
        c) Debit/Credit (single column)
    Returns: { statement_id, transactions: [...], count }
    """
    import io as _io
    from core.db import get_db
    from datetime import datetime as _dt

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f        = request.files["file"]
    fname    = (f.filename or "").lower()
    name     = request.form.get("name", "").strip() or f.filename or "Imported"
    client_id  = request.form.get("client_id",  type=int)
    quarter_id = request.form.get("quarter_id", type=int)

    raw = f.read()

    # ── Parse file into rows ──────────────────────────────────────────────────
    try:
        if fname.endswith(".csv"):
            import csv
            text = raw.decode("utf-8-sig", errors="replace")
            reader = list(csv.DictReader(_io.StringIO(text)))
            rows = [dict(r) for r in reader]
        elif fname.endswith(".xlsx") or fname.endswith(".xls"):
            try:
                import openpyxl
                wb  = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
                ws  = wb.active
                all_rows = list(ws.iter_rows(values_only=True))
                if not all_rows:
                    return jsonify({"error": "Empty spreadsheet"}), 400
                headers = [str(c).strip() if c is not None else "" for c in all_rows[0]]
                rows    = [dict(zip(headers, [str(v) if v is not None else "" for v in row]))
                           for row in all_rows[1:]]
            except ImportError:
                return jsonify({"error": "openpyxl not installed — cannot read Excel files"}), 500
        else:
            return jsonify({"error": "Unsupported file type. Use .csv, .xlsx or .xls"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    if not rows:
        return jsonify({"error": "File has no data rows"}), 400

    # ── Normalise column names ────────────────────────────────────────────────
    def _find_col(row_keys, *candidates):
        """Case-insensitive column lookup."""
        lkeys = {k.lower().strip(): k for k in row_keys}
        for c in candidates:
            if c.lower() in lkeys:
                return lkeys[c.lower()]
        return None

    sample    = rows[0]
    col_date  = _find_col(sample, "date", "transaction date", "txn date", "value date")
    col_desc  = _find_col(sample, "description", "details", "particulars", "narrative", "memo")
    col_deb   = _find_col(sample, "debit", "withdrawals", "withdrawal", "debit amount")
    col_cred  = _find_col(sample, "credit", "deposits", "deposit", "credit amount")
    col_amt   = _find_col(sample, "amount", "debit/credit", "credit/debit", "net amount", "value")

    if not col_date:
        return jsonify({"error": "Cannot find a Date column. Rename it to 'Date'."}), 400
    if not col_desc:
        return jsonify({"error": "Cannot find a Description column. Rename it to 'Description'."}), 400
    if not col_deb and not col_cred and not col_amt:
        return jsonify({"error": "Cannot find an Amount column. Needs 'Debit'+'Credit' or 'Amount'."}), 400

    # ── Parse amounts & dates ─────────────────────────────────────────────────
    def _parse_money(s):
        if s is None: return None
        s = str(s).strip().replace(",", "").replace("$", "").replace(" ", "")
        if not s or s in ("-", "—", ""): return None
        try: return float(s)
        except ValueError: return None

    def _parse_date(s):
        if not s: return None
        s = str(s).strip()
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y",
                    "%d %b %Y", "%d %b %y", "%d-%b-%Y", "%d-%b-%y",
                    "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d.%m.%y"):
            try: return _dt.strptime(s, fmt).strftime("%d-%b-%Y")
            except ValueError: continue
        return s  # keep raw if unparseable

    transactions = []
    skipped      = 0
    for i, row in enumerate(rows):
        date_raw = row.get(col_date, "")
        desc     = str(row.get(col_desc, "")).strip()
        if not date_raw and not desc:
            skipped += 1
            continue

        parsed_date = _parse_date(date_raw)

        if col_deb and col_cred:
            dv = _parse_money(row.get(col_deb))
            cv = _parse_money(row.get(col_cred))
            dv = -abs(dv) if dv else 0.0
            cv =  abs(cv) if cv else 0.0
            amount = dv + cv or (cv if cv else dv)
        elif col_amt:
            amount = _parse_money(row.get(col_amt)) or 0.0
        else:
            amount = 0.0

        if amount == 0.0 and not desc:
            skipped += 1
            continue

        transactions.append({
            "transaction_id": "",
            "date":           parsed_date or "",
            "description":    desc,
            "amount":         round(amount, 2),
            "balance":        None,
            "source_page":    1,
            "row_top":        float(i),
            "confidence":     1.0,
        })

    if not transactions:
        return jsonify({"error": f"No valid transactions found ({skipped} rows skipped)."}), 400

    # Sort by date then assign IDs
    try:
        transactions.sort(key=lambda t: (_dt.strptime(t["date"], "%d-%b-%Y") if t["date"] else _dt.min, t["row_top"]))
    except Exception:
        pass

    bank_id = "import"
    for i, t in enumerate(transactions):
        t["transaction_id"] = f"import_{i+1:04d}"

    # ── Save to DB via the same path as a parsed statement ────────────────────
    conn = get_db()

    # Determine statement_id
    stmt_id = conn.execute(
        "INSERT INTO statements (quarter_id, statement_name, bank_id, filename, parse_time_ms) VALUES (?,?,?,?,?)",
        (quarter_id, name, bank_id, f.filename or name, 0)
    ).lastrowid
    conn.commit()

    # Insert transactions
    for t in transactions:
        conn.execute(
            """INSERT INTO transactions
               (statement_id, transaction_id, date, description, amount, balance,
                source_page, row_top, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (stmt_id, t["transaction_id"], t["date"], t["description"],
             t["amount"], t["balance"], t["source_page"], t["row_top"], t["confidence"])
        )
    conn.commit()

    # Fetch back with DB ids
    saved = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ? ORDER BY id", (stmt_id,)
    ).fetchall()]

    return jsonify({
        "statement_id": stmt_id,
        "transactions": saved,
        "count":        len(saved),
        "skipped":      skipped,
        "name":         name,
    })
