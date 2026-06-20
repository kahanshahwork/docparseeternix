"""
detector.py – Registry that auto-discovers and runs parsers.

Each parser module must expose:
  can_parse(first_page_text: str, page_count: int) -> float  # 0.0-1.0
  parse(pdf_path: str) -> dict                               # {transactions, ambiguous, meta}

Drop a new file in parsers/ → it's auto-registered on next startup.
"""

import os
import importlib
import pdfplumber
from typing import Any

CONFIDENCE_THRESHOLD = 0.35


class Registry:
    def __init__(self):
        self._parsers: list[dict] = []   # [{bank_id, display_name, module}, ...]

    def auto_register(self, parsers_dir: str = None):
        if parsers_dir is None:
            parsers_dir = os.path.join(os.path.dirname(__file__), "parsers")

        for fname in sorted(os.listdir(parsers_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            modname = fname[:-3]
            if modname == "utils":
                continue
            try:
                mod = importlib.import_module(f"parsers.{modname}")
                if not (hasattr(mod, "can_parse") and hasattr(mod, "parse")):
                    continue
                # Get display name from module-level DISPLAY_NAME or capitalise modname
                display = getattr(mod, "DISPLAY_NAME", modname.upper())
                self._parsers.append({
                    "bank_id":      modname,
                    "display_name": display,
                    "module":       mod,
                })
            except Exception as e:
                print(f"[registry] Could not load parsers.{modname}: {e}")

    def detect(self, pdf_path: str) -> dict:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page_count = len(pdf.pages)
                first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
        except Exception as e:
            return {"error": str(e), "bank_id": None, "confidence": 0.0,
                    "all_scores": [], "page_count": 0}

        scores = []
        for p in self._parsers:
            try:
                score = p["module"].can_parse(first_text, page_count)
            except Exception:
                score = 0.0
            scores.append({**p, "confidence": round(score, 3)})

        scores.sort(key=lambda x: x["confidence"], reverse=True)
        best = scores[0] if scores else None

        all_scores = [
            {"bank_id": s["bank_id"], "display_name": s["display_name"],
             "confidence": s["confidence"]}
            for s in scores
        ]

        if best and best["confidence"] >= CONFIDENCE_THRESHOLD:
            return {
                "bank_id":      best["bank_id"],
                "display_name": best["display_name"],
                "confidence":   best["confidence"],
                "all_scores":   all_scores,
                "page_count":   page_count,
            }
        return {
            "bank_id":      None,
            "display_name": "Unknown",
            "confidence":   best["confidence"] if best else 0.0,
            "all_scores":   all_scores,
            "page_count":   page_count,
        }

    def parse(self, pdf_path: str, bank_id: str) -> dict:
        for p in self._parsers:
            if p["bank_id"] == bank_id:
                return p["module"].parse(pdf_path)
        raise ValueError(f"No parser found for bank_id='{bank_id}'")

    def list_parsers(self) -> list[dict]:
        return [{"bank_id": p["bank_id"], "display_name": p["display_name"]}
                for p in self._parsers]


registry = Registry()
