"""
routes/parser_routes.py — Module 1: Statement Processing Engine (HTTP layer).

This is the ONLY file that should change if you're touching upload/detect/
parse/pdf-preview behavior. It does not know about categories, GST, or P&L —
those live in routes/workflow_routes.py. Bank-specific logic stays inside
parsers/<bank>.py, untouched by this router.
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


@parser_bp.route("/")
def index():
    return send_from_directory(APP_ROOT, "index.html")


@parser_bp.route("/workflow")
def workflow_page():
    """Old standalone workflow.html is gone — the workflow is now pages inside the
    single-page app at '/'. Redirect so any saved bookmark still lands somewhere useful."""
    return redirect("/")


@parser_bp.route("/parsers")
def list_parsers():
    return jsonify(registry.list_parsers())


@parser_bp.route("/detect", methods=["POST"])
def detect():
    """Identifies WHICH BANK only. Nothing about account type/layout happens here —
    that's each parser's own internal business once /parse calls it."""
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["pdf"]
    if not f.filename.lower().endswith((".pdf", ".zip")):
        return jsonify({"error": "Only PDF/ZIP files supported"}), 400

    suffix = ".zip" if f.filename.lower().endswith(".zip") else ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    result = registry.detect(tmp_path)
    result["tmp_token"] = _save(tmp_path)
    return jsonify(result)


@parser_bp.route("/parse", methods=["POST"])
def parse():
    if request.content_type and "application/json" in request.content_type:
        body = request.json or {}
        tmp_token = body.get("tmp_token")
        bank_id = body.get("bank_id")
    else:
        tmp_token = request.form.get("tmp_token")
        bank_id = request.form.get("bank_id")

    tmp_path = None
    owns_file = False

    if tmp_token:
        tmp_path = _load(tmp_token)
        if not tmp_path:
            return jsonify({"error": "Session expired — please re-upload the file"}), 400
    elif "pdf" in request.files:
        f = request.files["pdf"]
        suffix = ".zip" if f.filename.lower().endswith(".zip") else ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
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
        result["bank_id"] = bank_id
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
    """Renders a page image. If `highlights` (plural, JSON array of tops) is given,
    draws ALL of them (item 3); `highlight` (singular) still works for the
    currently-selected row drawn in a stronger color on top."""
    token = request.args.get("tmp_token", "")
    page_num = int(request.args.get("page", "1"))
    highlight = request.args.get("highlight", "")       # selected row (strong)
    highlights = request.args.get("highlights", "")      # all rows on this page (faint)
    tmp_path = _load(token)
    if not tmp_path:
        return jsonify({"error": "Session expired"}), 400

    try:
        with pdfplumber.open(tmp_path) as pdf:
            total = len(pdf.pages)
            if page_num < 1 or page_num > total:
                return jsonify({"error": f"Page {page_num} out of range (1-{total})"}), 400
            page = pdf.pages[page_num - 1]
            img = page.to_image(resolution=150)

            from PIL import Image, ImageDraw
            base = img.original.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            scale = 150 / 72

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
