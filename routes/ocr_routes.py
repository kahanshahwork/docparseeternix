"""
routes/ocr_routes.py — OCR Tool: pure image-to-text extractor.

Zero bank logic. Zero transaction awareness.
Accepts image/PDF uploads and returns extracted text.

Endpoints:
  POST /tools/ocr/extract   — returns extracted text
  GET  /tools/ocr/status    — checks if Tesseract is installed and working
"""

import os
import io
import tempfile

from flask import Blueprint, request, jsonify

ocr_bp = Blueprint("ocr", __name__, url_prefix="/tools/ocr")

# ── Tesseract path — auto-detected, can be overridden via env var ──────────
# Set TESSERACT_PATH in your .env file if needed, e.g.:
#   TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
_TESSERACT_ENV = os.environ.get("TESSERACT_PATH", "")

# Common Windows install locations to try automatically
_WINDOWS_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\%s\AppData\Local\Tesseract-OCR\tesseract.exe" % os.environ.get("USERNAME", ""),
]


def _configure_tesseract():
    """
    Set pytesseract.tesseract_cmd to the correct path.
    Priority: env var → Windows known paths → hope it's on PATH.
    Returns (ok: bool, message: str).
    """
    try:
        import pytesseract

        # 1. Explicit env var wins
        if _TESSERACT_ENV and os.path.isfile(_TESSERACT_ENV):
            pytesseract.pytesseract.tesseract_cmd = _TESSERACT_ENV
            return True, f"Using TESSERACT_PATH: {_TESSERACT_ENV}"

        # 2. Try common Windows locations
        for p in _WINDOWS_PATHS:
            if os.path.isfile(p):
                pytesseract.pytesseract.tesseract_cmd = p
                return True, f"Auto-detected: {p}"

        # 3. Fall back to whatever is on PATH (works on Linux/Mac, sometimes Windows)
        pytesseract.get_tesseract_version()  # raises if not found
        return True, "Found on system PATH"

    except Exception as e:
        return False, str(e)


def _extract_from_image(path: str) -> str:
    """Run Tesseract OCR on an image file."""
    import pytesseract
    from PIL import Image

    ok, msg = _configure_tesseract()
    if not ok:
        raise RuntimeError(
            "Tesseract is not found on this machine.\n\n"
            "HOW TO FIX:\n"
            "1. Download from: https://github.com/UB-Mannheim/tesseract/wiki\n"
            "   (get tesseract-ocr-w64-setup-5.x.x.exe for Windows 64-bit)\n"
            "2. Install it — default path: C:\\Program Files\\Tesseract-OCR\\\n"
            "3. Either check 'Add to PATH' during install, OR add this to your .env:\n"
            "   TESSERACT_PATH=C:\\Program Files\\Tesseract-OCR\\tesseract.exe\n"
            "4. Restart Flask.\n\n"
            f"Diagnostic: {msg}"
        )

    img = Image.open(path)
    # --psm 6: assume uniform block of text (good for documents)
    # --oem 3: use LSTM + legacy engine (best accuracy)
    return pytesseract.image_to_string(img, config="--psm 6 --oem 3")


def _extract_from_pdf(path: str) -> str:
    """
    Extract text from a PDF.
    Strategy:
      - Pages WITH a text layer → pdfplumber (fast, perfect)
      - Pages WITHOUT a text layer (scanned) → render to image → Tesseract OCR
    """
    import pdfplumber

    pages_text = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()

            if text:
                # Has a text layer — use it directly
                pages_text.append(f"--- Page {i} ---\n{text}")
            else:
                # Scanned page — render to image and OCR it
                try:
                    img = page.to_image(resolution=200).original
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    buf.seek(0)

                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.write(buf.read())
                    tmp.close()

                    try:
                        ocr_text = _extract_from_image(tmp.name)
                        pages_text.append(f"--- Page {i} (OCR) ---\n{ocr_text}")
                    finally:
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass

                except Exception as e:
                    pages_text.append(f"--- Page {i} (OCR failed: {e}) ---")

    return "\n\n".join(pages_text)


# ── Routes ─────────────────────────────────────────────────────────────────

@ocr_bp.route("/status", methods=["GET"])
def status():
    """Health check — tells you if Tesseract is reachable."""
    ok, msg = _configure_tesseract()
    if ok:
        try:
            import pytesseract
            version = str(pytesseract.get_tesseract_version())
        except Exception as e:
            version = f"unknown ({e})"
        return jsonify({
            "tesseract_available": True,
            "version": version,
            "message": msg,
        })
    else:
        return jsonify({
            "tesseract_available": False,
            "message": msg,
            "fix": (
                "Download from https://github.com/UB-Mannheim/tesseract/wiki "
                "then set TESSERACT_PATH in your .env file, e.g.: "
                "TESSERACT_PATH=C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
            )
        }), 503


@ocr_bp.route("/extract", methods=["POST"])
def extract():
    """
    Accept an uploaded image or PDF, return extracted text.
    Form field: file (required)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Send field name 'file'."}), 400

    f = request.files["file"]
    fname = (f.filename or "").lower()
    ext = os.path.splitext(fname)[1]

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    PDF_EXTS   = {".pdf"}

    if ext not in IMAGE_EXTS and ext not in PDF_EXTS:
        mime = (f.mimetype or "").lower()
        if "image" in mime:
            ext = ".png"
        elif "pdf" in mime:
            ext = ".pdf"
        else:
            return jsonify({"error": f"Unsupported file type '{ext}'. Upload PNG, JPG, TIFF, or PDF."}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    f.save(tmp.name)
    tmp.close()

    try:
        if ext in IMAGE_EXTS:
            text = _extract_from_image(tmp.name)
            source_type = "image"
        else:
            text = _extract_from_pdf(tmp.name)
            source_type = "pdf"

        lines = [l for l in text.splitlines() if l.strip()]

        return jsonify({
            "text": text,
            "source_type": source_type,
            "filename": f.filename,
            "line_count": len(lines),
            "word_count": len(text.split()),
            "char_count": len(text),
        })

    except RuntimeError as e:
        # Our own clean error (e.g. Tesseract not found)
        return jsonify({"error": str(e)}), 503

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
