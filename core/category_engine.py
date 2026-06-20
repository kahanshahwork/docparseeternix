"""
core/category_engine.py

Two-stage transaction categorization engine.

Stage 1 (deterministic, free, instant):
    1a. Vendor memory exact match (core/vendor_memory.py suggest_category) —
        per-client learned mapping, returns a category_id directly.
    1b. Semantic bucket keyword match (core/vendor_memory.py semantic_bucket)
        — this returns a vendor GROUPING label (e.g. "Food Delivery"), not a
        category. BUCKET_TO_CATEGORY_HINT below maps that label to a likely
        category NAME, which we then resolve to an id. This is intentionally
        lower-confidence than vendor_memory, since it's a generic guess.

Stage 2 (AI fallback, only runs if Stage 1 fails to resolve confidently):
    Calls Groq's free-tier llama-3.3-70b-versatile endpoint.
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
from dataclasses import dataclass
from typing import Optional

import requests

from core.business_types import get_gst_treatment, is_valid_business_type, BUSINESS_TYPES
from core import vendor_memory
from core.category_master import list_categories, get_category


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

AI_TIMEOUT_SECONDS = 30

# Maps a vendor_memory.py semantic_bucket() label -> the category NAME (as it
# appears in core/category_master.py DEFAULT_CATEGORIES) it most commonly
# implies. This is a lower-confidence Stage 1b hint only -- it never
# overrides a real vendor_memory hit. Extend freely whenever a new bucket is
# added in vendor_memory.py SEMANTIC_BUCKETS; nothing else needs to change.
BUCKET_TO_CATEGORY_HINT = {
    "Food Delivery": "Meals & Entertainment",
    "Ride Share / Taxi": "Travel & Vehicle",
    "Bank Transfers": None,                        # too generic, needs AI/manual judgement
    "Interest": "Interest Income",
    "Subscriptions": "Subscriptions & Software",
    "Merchant Settlement": "Sales / Trading Income",
    "BPAY": None,
    "Direct Debit": None,
    "Salary / Payroll": "Salary & Wages",
    "Bank Fees": "Bank Fees & Charges",
}


@dataclass
class CategorySuggestion:
    category_id: Optional[int]
    category_name: str
    confidence: float          # 0.0 - 1.0
    source: str                # "vendor_memory" | "semantic_bucket" | "ai" | "unresolved"
    gst_applicable: bool
    gst_rate: float
    input_taxed: bool
    gst_note: Optional[str] = None
    raw_ai_response: Optional[str] = None


def _unresolved(note: str) -> CategorySuggestion:
    return CategorySuggestion(
        category_id=None,
        category_name="",
        confidence=0.0,
        source="unresolved",
        gst_applicable=False,
        gst_rate=0.0,
        input_taxed=False,
        gst_note=note,
    )


def _direction_mismatch(direction: str, pnl_group: str) -> bool:
    """
    Sanity check: a debit (money OUT) should never resolve to an Income
    category, and a credit (money IN) should never resolve to an Expense
    category. 'Excluded' (transfers, drawings, loan principal, etc.) is
    valid for either direction. This catches AI mistakes even when the
    prompt instructions are followed imperfectly -- defense in depth,
    matching the project's deterministic-check-first review philosophy.
    """
    direction = (direction or "").lower()
    if direction == "debit" and pnl_group == "Income":
        return True
    if direction == "credit" and pnl_group == "Expense":
        return True
    return False


def _categories_for_direction(direction: str, categories: list[dict]) -> list[dict]:
    """
    Filters the Category Master down to only categories that are valid for
    this transaction's direction, BEFORE the AI ever sees the list. This is
    the primary fix: rather than letting the AI choose from every category
    and catching a wrong-direction pick after the fact (still done as a
    backstop via _direction_mismatch), we remove the invalid options
    entirely so the AI structurally cannot make that mistake.

    debit (money out)  -> Expense + Excluded categories only
    credit (money in)  -> Income + Excluded categories only
    unknown/missing direction -> no filtering (full list, safest fallback)
    """
    direction = (direction or "").lower()
    if direction == "debit":
        return [c for c in categories if c["pnl_group"] in ("Expense", "Excluded")]
    if direction == "credit":
        return [c for c in categories if c["pnl_group"] in ("Income", "Excluded")]
    return categories


# ---------------------------------------------------------------------------
# Stage 1: Deterministic
# ---------------------------------------------------------------------------

def _try_deterministic(client_id: int, description: str, direction: str) -> Optional[tuple[int, float, str]]:
    """Returns (category_id, confidence, source) or None if unresolved."""

    # 1a. Vendor memory exact match -- already returns category_id directly.
    # Not direction-filtered here: it's a trusted learned per-client mapping,
    # and the final _direction_mismatch guardrail in categorize_transaction()
    # still catches it if a vendor's description is reused for both a charge
    # and a refund under the same normalized pattern.
    category_id = vendor_memory.suggest_category(client_id, description)
    if category_id:
        return category_id, 0.95, "vendor_memory"

    # 1b. Semantic bucket -> category name hint -> resolve to id.
    # Direction-filtered: only accept the hint if its P&L group is actually
    # valid for this transaction's direction, otherwise fall through to
    # Stage 2 (AI) rather than returning a structurally wrong suggestion.
    bucket = vendor_memory.semantic_bucket(description)
    if bucket:
        hint_name = BUCKET_TO_CATEGORY_HINT.get(bucket)
        if hint_name:
            valid_categories = _categories_for_direction(direction, list_categories())
            for cat in valid_categories:
                if cat["name"] == hint_name:
                    return cat["id"], 0.65, "semantic_bucket"

    return None


# ---------------------------------------------------------------------------
# Stage 2: AI fallback
# ---------------------------------------------------------------------------

def _build_ai_prompt(
    description: str,
    amount: float,
    direction: str,
    business_type_label: str,
    categories: list[dict],
    historical_examples: Optional[list[dict]] = None,
) -> list[dict]:
    category_list_str = "\n".join(
        f'- "{c["name"]}" (P&L Group: {c["pnl_group"]})' for c in categories
    )

    examples_block = ""
    if historical_examples:
        lines = [
            f'  description: "{ex["description"]}" -> category: "{ex["category_name"]}"'
            for ex in historical_examples[:8]
        ]
        examples_block = (
            "\n\nThis client's own previously approved categorizations "
            "(use these as the strongest signal for similar transactions):\n"
            + "\n".join(lines)
        )

    system_prompt = (
        "You are a bookkeeping categorization assistant for an Australian "
        "accounting firm. You must choose exactly ONE category from the "
        "provided Category Master list for the given bank transaction. "
        "Never invent a category that is not in the list -- copy the "
        "category name EXACTLY as given, including punctuation.\n\n"
        "CRITICAL RULE: pay close attention to the transaction's direction. "
        "A 'debit' means money LEFT the account (an expense/purchase) -- it "
        "must map to a category with P&L Group 'Expense' or 'Excluded', "
        "NEVER 'Income'. A 'credit' means money ENTERED the account "
        "(income/revenue received) -- it must map to a category with P&L "
        "Group 'Income' or 'Excluded', NEVER 'Expense'. Do not be misled by "
        "the merchant's business type (e.g. a transaction at a clothing "
        "store is an EXPENSE if it's a debit, even though the store itself "
        "sells clothing -- you are categorizing what the bank account "
        "owner did, not what the merchant does).\n\n"
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


def _call_groq(messages: list[dict]) -> Optional[str]:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to your .env file.")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 200,
    }

    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=AI_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        print(f"[category_engine] Groq request failed: {e}")
        return None
    except (KeyError, IndexError) as e:
        print(f"[category_engine] Unexpected Groq response shape: {e}")
        return None


def _parse_ai_json(raw: str) -> Optional[dict]:
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
    categories: list[dict],
    historical_examples: Optional[list[dict]] = None,
) -> Optional[tuple[int, float, str, str]]:
    messages = _build_ai_prompt(
        description, amount, direction, business_type_label, categories, historical_examples,
    )
    raw = _call_groq(messages)
    if raw is None:
        return None

    parsed = _parse_ai_json(raw)
    if parsed is None:
        print(f"[category_engine] Could not parse AI response as JSON: {raw!r}")
        return None

    category_name = parsed["category"]
    confidence = min(float(parsed["confidence"]), 0.85)  # AI always capped, needs human review

    match = next((c for c in categories if c["name"] == category_name), None)
    if match is None:
        print(f"[category_engine] AI returned unknown category: {category_name!r}")
        return None

    return match["id"], confidence, "ai", raw


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def categorize_transaction(
    client_id: int,
    description: str,
    amount: float,
    direction: str,
    business_type_code: str,
    historical_examples: Optional[list[dict]] = None,
) -> CategorySuggestion:
    """
    Main entry point used by routes/workflow_routes.py during the
    Categorize step. Pulls the live Category Master from the DB via
    core.category_master.list_categories() -- no need to pass it in.

    Returns a CategorySuggestion. Never raises for "no category found" --
    returns source="unresolved" instead, so the UI can show a manual-pick
    state without crashing the categorize page.
    """
    if not is_valid_business_type(business_type_code):
        raise ValueError(f"Unknown business_type_code: {business_type_code!r}")

    categories = list_categories()  # [{id, code, name, pnl_group, gst_applicable, gst_rate, bas_label, ...}, ...]
    raw_ai = None

    # --- Stage 1 ---
    deterministic = _try_deterministic(client_id, description, direction)
    if deterministic:
        category_id, confidence, source = deterministic
    else:
        # --- Stage 2 ---
        # Primary fix for the debit->Income bug: the AI only ever sees
        # categories that are structurally valid for this transaction's
        # direction, so it cannot pick a wrong-direction category even if
        # it misreads the merchant/description context.
        direction_filtered_categories = _categories_for_direction(direction, categories)
        business_type_label = next(b["label"] for b in BUSINESS_TYPES if b["code"] == business_type_code)
        ai_result = _try_ai(
            description, amount, direction, business_type_label,
            direction_filtered_categories, historical_examples,
        )
        if ai_result:
            category_id, confidence, source, raw_ai = ai_result
        else:
            return _unresolved("No deterministic or AI match found -- manual selection required.")

    cat_row = get_category(category_id)
    if cat_row is None:
        return _unresolved(f"Resolved category_id {category_id} no longer exists -- manual selection required.")

    gst = get_gst_treatment(
        category_name=cat_row["name"],
        business_type_code=business_type_code,
        default_gst_applicable=bool(cat_row["gst_applicable"]),
        default_gst_rate=cat_row["gst_rate"],
    )

    gst_note = gst["note"]

    # --- Direction sanity guardrail (defense in depth, runs regardless of source) ---
    if _direction_mismatch(direction, cat_row["pnl_group"]):
        flag = (
            f"⚠ FLAGGED FOR REVIEW: a '{direction}' transaction was resolved to "
            f"'{cat_row['name']}' (P&L Group: {cat_row['pnl_group']}), which "
            f"conflicts with the transaction direction -- this combination is "
            f"very unusual and should be manually verified before approving."
        )
        gst_note = f"{flag} {gst_note}" if gst_note else flag
        confidence = min(confidence, 0.30)  # force into manual-review range regardless of original score

    return CategorySuggestion(
        category_id=category_id,
        category_name=cat_row["name"],
        confidence=confidence,
        source=source,
        gst_applicable=gst["gst_applicable"],
        gst_rate=gst["gst_rate"],
        input_taxed=gst["input_taxed"],
        gst_note=gst_note,
        raw_ai_response=raw_ai if source == "ai" else None,
    )
