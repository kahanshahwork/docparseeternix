# Category Allocation System — Setup & Integration Guide

## 1. Get your OpenRouter API key

1. Go to https://openrouter.ai/keys (sign in / sign up if needed).
2. Click "Create Key", copy it (starts with `sk-or-v1-...`).
3. Verify the free tier model is what you expect:
   https://openrouter.ai/meta-llama/llama-3.3-70b-instruct:free
   -> confirm "Free" pricing shows $0 / 1M tokens on that page before relying
      on it for production volume.

## 2. Create your .env file

You don't have one yet -- that's normal, it's just a plain text file.

1. In `C:\New combined Doc parse`, create a new file named exactly `.env`
   (no filename before the dot).
2. Open `.env.example` (provided in this delivery) and copy its contents in.
3. Replace `paste_your_openrouter_key_here` with your real key.
4. Make sure `.gitignore` in the project root contains a line `.env` so the
   key never gets committed to git. If you don't have a `.gitignore` yet,
   create one with at minimum:
   ```
   .env
   __pycache__/
   *.pyc
   ```

## 3. Install the one new Python dependency

This system uses the `requests` library to call OpenRouter. If it's not
already installed in your project's virtual environment:

```powershell
pip install requests python-dotenv
```

`python-dotenv` is needed so Flask actually loads `.env` into
`os.environ` -- if `app.py` doesn't already do this, add near the top:

```python
from dotenv import load_dotenv
load_dotenv()
```

## 4. Files delivered in this step

- `core/business_types.py` — Australian business type list + GST override
  matrix. Self-contained, no dependency on your existing files.
- `core/category_engine.py` — two-stage categorization engine. **Depends on
  `core/vendor_memory.py` already having these three functions** (from your
  existing build):
  - `normalize_description(text) -> str`
  - `lookup_vendor_memory(client_id, normalized_description) -> Optional[str]`
  - `suggest_category_from_bucket(normalized_description) -> Optional[str]`

  **I don't have visibility into your current `core/vendor_memory.py`
  contents**, so if your existing function names differ from the three
  above, the import at the top of `category_engine.py` will fail. Either:
  (a) rename your existing functions to match, or
  (b) tell me the actual function names/signatures and I'll adjust
      `category_engine.py` to match in one edit.

## 5. What's NOT yet wired in (next steps, not in this delivery)

- `business_type` column on the Client table in `core/db.py` — I don't have
  your current schema, so I haven't touched `db.py`. Send me the current
  `clients` table schema (or the file) and I'll add the column + a safe
  migration in the next step.
- The `routes/workflow_routes.py` Categorize endpoint doesn't yet call
  `category_engine.categorize_transaction()` — that wiring is the next step
  once the schema change above is in.
- The category-driven grouping UI change in `index.html` (expand/collapse
  by resolved category, live re-file on edit) — not started yet, this was
  agreed to come after the engine itself is working and tested.

## 6. Quick manual test (once .env is set up)

```python
from core.business_types import BUSINESS_TYPES
from core.category_engine import categorize_transaction

# Minimal fake category master for testing -- replace with your real one
test_category_master = {
    "Meals & Entertainment": {"gst_applicable": True, "gst_rate": 0.10, "pnl_group": "Expense"},
    "Travel": {"gst_applicable": True, "gst_rate": 0.10, "pnl_group": "Expense"},
}

result = categorize_transaction(
    client_id="test-client-1",
    description="KFC RESTAURANT SYDNEY",
    amount=24.50,
    direction="debit",
    business_type_code="RETAIL_TRADING",
    category_master=test_category_master,
)
print(result)
```

If `core/vendor_memory.py` functions don't match yet, this will raise an
`ImportError` or `AttributeError` -- that's expected until step 4 above is
resolved.
