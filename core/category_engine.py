"""
core/category_engine.py

Two-stage transaction categorization engine.

Stage 1 (deterministic, free, instant):
    1a. Vendor memory exact match (core/vendor_memory.py) — per-client
        learned mapping, e.g. "UBER EATS" -> "Meals & Entertainment"
    1b. Semantic bucket keyword match (core/vendor_memory.py SEMANTIC_BUCKETS)
        — generic keyword groupings as a category SUGGESTION input, not the
        final grouping key (per the architecture decision already locked in)

Stage 2 (AI fallback, only runs if Stage 1 fails to resolve confidently):
    Calls OpenRouter's free-tier meta-llama/llama-3.3-70b-instruct endpoint.
    The AI NEVER auto-applies — it always returns a suggestion + confidence,
    which the workflow layer surfaces for human approval. This matches the
    project's standing constraint: "AI suggestions are never auto-applied."

Every returned suggestion also carries the resolved GST treatment for the
client's business_type, via core/business_types.get_gst_treatment().

This module is intentionally the ONLY place that talks to the AI provider.
Swapping providers/models later = editing this one file.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

import requests

from core.business_types import get_gst_treatment, is_valid_business_type
from core import vendor_memory


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

# OpenRouter recommends these headers for attribution / free-tier routing.
# Update OR_SITE_URL / OR_APP_NAME to your real values once deployed.
OR_SITE_URL = os.environ.get("OR_SITE_URL", "https://varcrm.vercel.app")
OR_APP_NAME = os.environ.get("OR_APP_NAME", "Vardhman BAS Workflow")

AI_TIMEOUT_SECONDS = 30


@dataclass
class CategorySuggestion:
    category: str
    confidence: float          # 0.0 - 1.0
    source: str                # "vendor_memory" | "semantic_bucket" | "ai" | "unresolved"
    gst_applicable: bool
    gst_rate: float
    input_taxed: bool
    gst_note: Optional[str] = None
    raw_ai_response: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 1: Deterministic
# ---------------------------------------------------------------------------

def _try_deterministic(
    client_id: str,
    description: str,
    category_master: dict,
) -> Optional[tuple[str, float, str]]:
    """
    Returns (category_name, confidence, source) or None if unresolved.
    category_master: dict of {category_name: {...}} from core/category_master.py
    """
    normalized = vendor_memory.normalize_description(description)

    # 1a. Exact vendor memory match for this specific client
    remembered = vendor_memory.lookup_vendor_memory(client_id, normalized)
    if remembered and remembered in category_master:
        return remembered, 0.95, "vendor_memory"

    # 1b. Semantic bucket keyword match -> category suggestion
    bucket_category = vendor_memory.suggest_category_from_bucket(normalized)
    if bucket_category and bucket_category in category_master:
        return bucket_category, 0.70, "semantic_bucket"

    return None


# ---------------------------------------------------------------------------
# Stage 2: AI fallback
# ---------------------------------------------------------------------------

def _build_ai_prompt(
    description: str,
    amount: float,
    direction: str,                       # "debit" or "credit"
    business_type_label: str,
    category_master: dict,
    historical_examples: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Builds the OpenRouter chat messages. Forces strict JSON output and
    constrains the model to ONLY the categories that exist in the Category
    Master, so it can never invent a new category.
    """
    category_list_str = "\n".join(
        f"- {name} (P&L Group: {meta.get('pnl_group', 'Unknown')})"
        for name, meta in category_master.items()
    )

    examples_block = ""
    if historical_examples:
        lines = []
        for ex in historical_examples[:8]:  # cap few-shot count
            lines.append(
                f'  description: "{ex["description"]}" -> category: "{ex["category"]}"'
            )
        examples_block = (
            "\n\nThis client's own previously approved categorizations "
            "(use these as the strongest signal for similar transactions):\n"
            + "\n".join(lines)
        )

    system_prompt = (
        "You are a bookkeeping categorization assistant for an Australian "
        "accounting firm. You must choose exactly ONE category from the "
        "provided Category Master list for the given bank transaction. "
        "Never invent a category that is not in the list. "
        "Respond with ONLY a JSON object, no preamble, no markdown fences, "
        "in this exact shape:\n"
        '{"category": "<exact category name from the list>", '
        '"confidence": <float 0.0-1.0>}'
    )

    user_prompt = (
        f"Business type: {business_type_label}\n\n"
        f"Category Master (choose exactly one category name from this list):\n"
        f"{category_list_str}\n"
        f"{examples_block}\n\n"
        f"Transaction to categorize:\n"
        f'  description: "{description}"\n'
        f"  amount: {amount}\n"
        f"  direction: {direction}\n\n"
        "Return only the JSON object."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _call_openrouter(messages: list[dict]) -> Optional[str]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to your .env file."
        )

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OR_SITE_URL,
        "X-Title": OR_APP_NAME,
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.1,   # low temperature: we want consistent classification, not creativity
        "max_tokens": 200,
    }

    try:
        resp = requests.post(
            OPENROUTER_URL, headers=headers, json=payload, timeout=AI_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        # Network/API failure: caller treats this as "AI unavailable", falls
        # back to unresolved rather than crashing the categorize workflow.
        print(f"[category_engine] OpenRouter request failed: {e}")
        return None
    except (KeyError, IndexError) as e:
        print(f"[category_engine] Unexpected OpenRouter response shape: {e}")
        return None


def _parse_ai_json(raw: str) -> Optional[dict]:
    """Strips markdown fences defensively, then parses JSON."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        if "category" in parsed and "confidence" in parsed:
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _try_ai(
    description: str,
    amount: float,
    direction: str,
    business_type_label: str,
    category_master: dict,
    historical_examples: Optional[list[dict]] = None,
) -> Optional[tuple[str, float, str]]:
    messages = _build_ai_prompt(
        description, amount, direction, business_type_label,
        category_master, historical_examples,
    )
    raw = _call_openrouter(messages)
    if raw is None:
        return None

    parsed = _parse_ai_json(raw)
    if parsed is None:
        print(f"[category_engine] Could not parse AI response as JSON: {raw!r}")
        return None

    category = parsed["category"]
    confidence = float(parsed["confidence"])

    if category not in category_master:
        # Model hallucinated a category outside the allowed list -- treat as
        # unresolved rather than silently accepting an invalid category.
        print(f"[category_engine] AI returned unknown category: {category!r}")
        return None

    # Cap AI-sourced confidence below deterministic matches, since AI
    # suggestions always need human review per project policy.
    confidence = min(confidence, 0.85)

    return category, confidence, "ai", raw


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def categorize_transaction(
    client_id: str,
    description: str,
    amount: float,
    direction: str,
    business_type_code: str,
    category_master: dict,
    historical_examples: Optional[list[dict]] = None,
) -> CategorySuggestion:
    """
    Main entry point used by routes/workflow_routes.py during the
    Categorize step.

    category_master: the full {category_name: {gst_applicable, gst_rate,
        pnl_group, ...}} dict loaded from core/category_master.py

    Returns a CategorySuggestion. Never raises for "no category found" --
    returns source="unresolved" instead, so the UI can show a manual-pick
    state without crashing the categorize page.
    """
    if not is_valid_business_type(business_type_code):
        raise ValueError(f"Unknown business_type_code: {business_type_code!r}")

    # --- Stage 1 ---
    deterministic = _try_deterministic(client_id, description, category_master)
    if deterministic:
        category, confidence, source = deterministic
        raw_ai = None
    else:
        # --- Stage 2 ---
        business_type_label = next(
            b["label"] for b in __import__(
                "core.business_types", fromlist=["BUSINESS_TYPES"]
            ).BUSINESS_TYPES if b["code"] == business_type_code
        )
        ai_result = _try_ai(
            description, amount, direction, business_type_label,
            category_master, historical_examples,
        )
        if ai_result:
            category, confidence, source, raw_ai = ai_result
        else:
            # Fully unresolved -- caller/UI should prompt for manual category pick.
            default_cat = next(iter(category_master.keys()))  # placeholder only
            return CategorySuggestion(
                category="",
                confidence=0.0,
                source="unresolved",
                gst_applicable=False,
                gst_rate=0.0,
                input_taxed=False,
                gst_note="No deterministic or AI match found -- manual selection required.",
            )

    # --- GST resolution (business-type aware) ---
    cat_meta = category_master[category]
    gst = get_gst_treatment(
        category_name=category,
        business_type_code=business_type_code,
        default_gst_applicable=cat_meta.get("gst_applicable", False),
        default_gst_rate=cat_meta.get("gst_rate", 0.0),
    )

    return CategorySuggestion(
        category=category,
        confidence=confidence,
        source=source,
        gst_applicable=gst["gst_applicable"],
        gst_rate=gst["gst_rate"],
        input_taxed=gst["input_taxed"],
        gst_note=gst["note"],
        raw_ai_response=raw_ai if source == "ai" else None,
    )
