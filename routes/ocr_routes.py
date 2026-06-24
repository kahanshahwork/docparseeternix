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


# ══════════════════════════════════════════════════════════════════════════
# NVIDIA AI VISION — Nemotron-3-Nano-Omni multimodal playground
# ══════════════════════════════════════════════════════════════════════════

import base64
import json

_NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_MODEL    = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


@ocr_bp.route("/nvidia/chat", methods=["POST"])
def nvidia_chat():
    """
    Proxy endpoint for NVIDIA Nemotron-3-Nano-Omni multimodal chat.
    Body (JSON):
      messages  — full conversation history (OpenAI format)
      image_b64 — optional base64 image data URI for the latest user turn
      image_mime— optional mime type e.g. "image/png"
    Returns streaming SSE text or JSON on error.
    """
    if not _NVIDIA_API_KEY:
        return jsonify({"error": "NVIDIA_API_KEY not set in .env"}), 500

    body = request.get_json(force=True) or {}
    messages = body.get("messages", [])
    image_b64  = body.get("image_b64")   # data-URI base64 string
    image_mime = body.get("image_mime", "image/png")

    # If an image is attached to the latest user message, convert it
    # to the OpenAI vision content block format
    if image_b64 and messages and messages[-1]["role"] == "user":
        last = messages[-1]
        text_content = last["content"] if isinstance(last["content"], str) else ""
        # Ensure it's a data URI
        if not image_b64.startswith("data:"):
            image_b64 = f"data:{image_mime};base64,{image_b64}"

        # Log image size for debugging
        img_kb = len(image_b64) * 3 // 4 // 1024
        print(f"[NVIDIA] Image size: ~{img_kb} KB, mime: {image_mime}", flush=True)

        # Guard: if image is still too large, reject early with clear message
        if img_kb > 4096:
            return jsonify({"error": f"Image too large ({img_kb} KB). Please use a smaller or lower-resolution image (max ~4 MB)."}), 413

        # Build multimodal content — text FIRST, then image (NVIDIA requires this order)
        messages[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {"url": image_b64}},
            ]
        }
        print(f"[NVIDIA] Sending multimodal message, text: {text_content[:80]!r}", flush=True)

    payload = {
        "model": _NVIDIA_MODEL,
        "messages": messages,
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 32768,   # must be high — model uses budget for reasoning first
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": True},
        "stream_options": {"include_usage": False},
    }

    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        f"{_NVIDIA_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_NVIDIA_API_KEY}",
            "NVCF-POLL-SECONDS": "300",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return jsonify({"error": f"NVIDIA API error {e.code}: {err_body}"}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def generate():
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    yield "data: [DONE]\n\n"
                    break
                if not line.startswith("data: "):
                    continue
                raw_json = line[6:].strip()
                try:
                    chunk = json.loads(raw_json)
                    choice = chunk.get("choices", [{}])[0]
                    delta  = choice.get("delta", {})
                    # Emit reasoning_content as a special prefixed token
                    # so the frontend can render a collapsible thinking section
                    rc = delta.get("reasoning_content") or ""
                    tc = delta.get("content") or ""
                    if rc:
                        # Wrap reasoning so frontend can detect and style it
                        patched = dict(chunk)
                        patched["choices"] = [{
                            **choice,
                            "delta": {"content": "\u200b" + rc, "reasoning": True}
                        }]
                        yield "data: " + json.dumps(patched) + "\n\n"
                    if tc:
                        yield line + "\n\n"
                except Exception:
                    # Pass raw line through if unparseable
                    yield line + "\n\n"
        finally:
            resp.close()

    from flask import Response
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
