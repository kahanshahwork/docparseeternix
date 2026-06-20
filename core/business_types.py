"""
core/business_types.py

Defines the set of Australian business types this platform supports, and a
GST override matrix that captures cases where GST treatment for a category
differs from the Category Master default depending on the client's business
type.

Design principle (per project constraints):
- Category Master (core/category_master.py) remains the single global default
  source of truth for GST treatment.
- This module only stores OVERRIDES — rows where a specific business type's
  GST treatment for a category differs from the default. Most categories for
  most business types need no entry at all, which keeps this table small and
  easy to extend without touching any other file.
- Nothing here is auto-applied without being run through the normal GST
  engine; this module only supplies the lookup data.

GST treatment basics this matrix encodes (ATO-aligned, general rules):
- Standard taxable supply: GST_APPLICABLE, 10%
- GST-free supply (e.g. basic food, exports, health, education, childcare):
  NOT GST applicable, 0%, but still counts toward GST turnover for
  registration purposes (handled downstream, not here)
- Input taxed supply (e.g. residential rent, most financial services,
  bank fees on financial supplies): NOT GST applicable, 0%, and does NOT
  allow GST credits on related purchases (flagged via `input_taxed=True`)
"""

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Business Types
# ---------------------------------------------------------------------------
# Each client is assigned exactly one of these. Kept intentionally small and
# focused on the business types this platform's actual client base needs;
# extend the list by adding a new tuple, no other code changes required.

BUSINESS_TYPES = [
    {
        "code": "RETAIL_TRADING",
        "label": "Retail / Trading",
        "description": "Buys and sells goods, standard GST treatment in most categories.",
    },
    {
        "code": "PROFESSIONAL_SERVICES",
        "label": "Professional Services",
        "description": "Consulting, legal, accounting, advisory — standard GST on services.",
    },
    {
        "code": "FINANCIAL_SERVICES",
        "label": "Financial Services",
        "description": "Lending, investment advice, insurance broking. Most core income is "
                        "input-taxed; GST credits on related purchases are restricted.",
    },
    {
        "code": "MEDICAL_HEALTH",
        "label": "Medical & Health Services",
        "description": "GP, allied health, dental. Most patient-facing services are GST-free.",
    },
    {
        "code": "REAL_ESTATE_RESIDENTIAL",
        "label": "Real Estate — Residential",
        "description": "Residential rent is input-taxed; property management fees are taxable.",
    },
    {
        "code": "REAL_ESTATE_COMMERCIAL",
        "label": "Real Estate — Commercial",
        "description": "Commercial rent and related supplies are standard taxable.",
    },
    {
        "code": "EDUCATION",
        "label": "Education & Training",
        "description": "Course fees for registered courses are typically GST-free.",
    },
    {
        "code": "CHILDCARE",
        "label": "Childcare",
        "description": "Approved childcare services are GST-free.",
    },
    {
        "code": "EXPORT",
        "label": "Export Business",
        "description": "Goods/services exported outside Australia are generally GST-free.",
    },
    {
        "code": "CONSTRUCTION_TRADES",
        "label": "Construction & Trades",
        "description": "Standard taxable supply; subject to taxable payments reporting (TPAR).",
    },
    {
        "code": "HOSPITALITY_FOOD",
        "label": "Hospitality / Food Service",
        "description": "Prepared/restaurant food is taxable; some raw food inputs are GST-free.",
    },
    {
        "code": "AGRICULTURE",
        "label": "Agriculture / Primary Production",
        "description": "Many basic unprocessed food products are GST-free at wholesale stage.",
    },
    {
        "code": "NOT_FOR_PROFIT",
        "label": "Charity / Not-for-Profit",
        "description": "Eligible NFPs access GST concessions on certain supplies and may have "
                        "a higher GST registration turnover threshold.",
    },
    {
        "code": "IMPORT_WHOLESALE",
        "label": "Import / Wholesale",
        "description": "Standard taxable supply; GST may apply at import (handled separately "
                        "to standard category GST, flagged for review).",
    },
]

BUSINESS_TYPE_CODES = {b["code"] for b in BUSINESS_TYPES}


# ---------------------------------------------------------------------------
# GST Override Matrix
# ---------------------------------------------------------------------------
# Keyed by (category_name, business_type_code) -> override dict.
# category_name MUST match a category name exactly as it exists in
# core/category_master.py.
#
# Only add a row here if the business type's treatment for that category
# DIFFERS from the Category Master default. If a (category, business_type)
# pair is absent, the engine falls back to the Category Master default —
# this is enforced in get_gst_treatment() below.

@dataclass
class GSTOverride:
    gst_applicable: bool
    gst_rate: float          # 0.0 to 0.10
    input_taxed: bool
    note: str


GST_OVERRIDES: dict[tuple[str, str], GSTOverride] = {

    # --- Rental Income ---
    ("Rental Income", "REAL_ESTATE_RESIDENTIAL"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=True,
        note="Residential rent is an input taxed supply under GST law.",
    ),
    ("Rental Income", "REAL_ESTATE_COMMERCIAL"): GSTOverride(
        gst_applicable=True, gst_rate=0.10, input_taxed=False,
        note="Commercial rent is a standard taxable supply.",
    ),

    # --- Sales / Service Income ---
    ("Sales", "MEDICAL_HEALTH"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Most patient medical/health services are GST-free under the ATO's "
             "medical services exemption.",
    ),
    ("Sales", "EDUCATION"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Fees for a recognised/registered course are GST-free.",
    ),
    ("Sales", "CHILDCARE"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Approved childcare services are GST-free.",
    ),
    ("Sales", "EXPORT"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Goods/services exported outside Australia within the required "
             "timeframe are GST-free.",
    ),
    ("Sales", "FINANCIAL_SERVICES"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=True,
        note="Core financial supplies (lending, account fees, most insurance) "
             "are input taxed, not GST-free — credits on related purchases "
             "are restricted (denied input tax credits, partially recoverable "
             "via the financial acquisitions threshold rules).",
    ),

    # --- Interest / Bank-related income or fees ---
    ("Interest Income", "FINANCIAL_SERVICES"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=True,
        note="Interest is an input taxed financial supply.",
    ),

    # --- Insurance ---
    ("Insurance", "FINANCIAL_SERVICES"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=True,
        note="Most general insurance premiums for a financial services "
             "business's own core supplies are input taxed; note this differs "
             "from insurance as a purchased EXPENSE by other business types, "
             "which is normally a taxable supply (standard 10% applies there).",
    ),

    # --- Food/Groceries-type categories for hospitality and agriculture ---
    ("Cost of Goods Sold", "AGRICULTURE"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Many unprocessed/basic food products are GST-free at the "
             "wholesale/primary production stage; this is description-level "
             "nuance — confidence should be capped lower and flagged for "
             "human review rather than blanket-applied.",
    ),

    # --- Donations for NFPs ---
    ("Donations Received", "NOT_FOR_PROFIT"): GSTOverride(
        gst_applicable=False, gst_rate=0.0, input_taxed=False,
        note="Genuine gifts/donations to an eligible NFP are outside the GST "
             "system entirely (not a supply for consideration).",
    ),
}


def get_gst_treatment(
    category_name: str,
    business_type_code: str,
    default_gst_applicable: bool,
    default_gst_rate: float,
) -> dict:
    """
    Resolve the effective GST treatment for a (category, business_type) pair.

    Falls back to the Category Master default if no override exists. This
    function NEVER guesses — it only returns an override if one is
    explicitly defined above, otherwise the caller's default is returned
    unchanged.
    """
    override = GST_OVERRIDES.get((category_name, business_type_code))
    if override is None:
        return {
            "gst_applicable": default_gst_applicable,
            "gst_rate": default_gst_rate,
            "input_taxed": False,
            "source": "category_master_default",
            "note": None,
        }
    return {
        "gst_applicable": override.gst_applicable,
        "gst_rate": override.gst_rate,
        "input_taxed": override.input_taxed,
        "source": "business_type_override",
        "note": override.note,
    }


def list_business_types() -> list[dict]:
    """Return the business type list for populating a Client dropdown."""
    return BUSINESS_TYPES


def is_valid_business_type(code: str) -> bool:
    return code in BUSINESS_TYPE_CODES
