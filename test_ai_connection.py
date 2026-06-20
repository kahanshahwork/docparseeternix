"""
test_ai_connection.py

Standalone sanity-check script -- run this anytime to confirm:
1. Your .env / OPENROUTER_API_KEY is loaded correctly
2. OpenRouter's llama-3.3-70b-instruct:free endpoint actually responds
3. The full category_engine.categorize_transaction() pipeline works
   end-to-end against your real database and Category Master

Run from your project root:
    python test_ai_connection.py

Safe to run repeatedly -- it creates a throwaway test client/transaction,
does not touch your real client data.
"""

import os
import sys

from dotenv import load_dotenv
load_dotenv()

from core.db import init_db, get_db
from core.category_master import seed_categories
from core.category_engine import categorize_transaction, GROQ_API_KEY


def main():
    print("=" * 60)
    print("DocParse BAS Workflow -- AI Connection Test")
    print("=" * 60)

    # --- Step 1: API key present? ---
    if not GROQ_API_KEY:
        print("\n❌ FAIL: GROQ_API_KEY is not set.")
        print("   Check that .env exists in the project root and contains:")
        print("   GROQ_API_KEY=gsk_xxxxxxxx...")
        sys.exit(1)
    masked = GROQ_API_KEY[:10] + "..." + GROQ_API_KEY[-4:]
    print(f"\n✅ Step 1: GROQ_API_KEY loaded ({masked})")

    # --- Step 2: DB + categories ready? ---
    init_db()
    seed_categories()
    conn = get_db()
    cat_count = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    print(f"✅ Step 2: Database ready, {cat_count} categories in Category Master")

    # --- Step 3: throwaway test client ---
    existing = conn.execute("SELECT id FROM clients WHERE name = '__AI_TEST_CLIENT__'").fetchone()
    if existing:
        test_client_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO clients (name, business_type) VALUES (?, ?)",
            ("__AI_TEST_CLIENT__", "RETAIL_TRADING"),
        )
        conn.commit()
        test_client_id = cur.lastrowid
    print(f"✅ Step 3: Test client ready (id={test_client_id})")

    # --- Step 4: call the AI fallback directly ---
    # Description deliberately won't match vendor memory or any semantic
    # bucket keyword, so this is guaranteed to fall through to Stage 2 (AI).
    test_description = "ZARA CLOTHING STORE MELBOURNE 4471"
    print(f"\n→ Sending test transaction to AI: \"{test_description}\"")
    print("  (this transaction won't match vendor memory or semantic buckets,")
    print("   so it MUST go through Groq to resolve)\n")

    try:
        result = categorize_transaction(
            client_id=test_client_id,
            description=test_description,
            amount=89.00,
            direction="debit",
            business_type_code="RETAIL_TRADING",
        )
    except Exception as e:
        print(f"❌ FAIL: categorize_transaction() raised an exception: {e}")
        sys.exit(1)

    print("-" * 60)
    print("RESULT:")
    print(f"  category_name : {result.category_name!r}")
    print(f"  source        : {result.source}")
    print(f"  confidence    : {result.confidence}")
    print(f"  gst_applicable: {result.gst_applicable}")
    print(f"  gst_rate      : {result.gst_rate}")
    print("-" * 60)

    if result.source == "ai" and result.category_name:
        print("\n✅ SUCCESS: Llama 3.3 70B (via Groq) is working correctly.")
        print(f"   It correctly resolved an unknown transaction to: {result.category_name!r}")
    elif result.source == "unresolved":
        print("\n❌ FAIL: AI did not resolve the transaction.")
        print(f"   Reason: {result.gst_note}")
        print("   Check the console output above this for an")
        print("   '[category_engine] Groq request failed: ...' line --")
        print("   that will show the actual error (auth, rate limit, etc).")
        sys.exit(1)
    else:
        print(f"\n⚠ Unexpected: resolved via '{result.source}' instead of 'ai'.")
        print("  This usually means vendor memory already has a stale mapping")
        print("  for this exact test description from a previous test run --")
        print("  not a real problem, but means this run didn't actually test the AI call.")

    # --- cleanup hint ---
    print("\nTo remove the test client and its data later, run in your DB:")
    print(f"  DELETE FROM vendor_memory WHERE client_id = {test_client_id};")
    print(f"  DELETE FROM clients WHERE id = {test_client_id};")


if __name__ == "__main__":
    main()
