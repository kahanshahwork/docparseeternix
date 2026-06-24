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
