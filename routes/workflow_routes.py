"""
routes/workflow_routes.py — Modules 2/3/6/7/8/9 (HTTP layer).

This is the ONLY file that should change for approval-screen, category-
allocation, GST-review, or P&L-summary behavior. It calls into
core/category_master.py, core/gst_engine.py, core/pnl_engine.py and
core/vendor_memory.py — edit THOSE for logic changes, this file for
endpoint/request-shape changes only.

Flow: POST /api/statements (ingest parsed txns)
      -> GET/PATCH /api/statements/<id>/transactions (approval screen, item 5)
      -> POST /api/statements/<id>/approve
      -> GET /api/categories (item 6)
      -> GET /api/statements/<id>/groups (item 7)
      -> POST /api/statements/<id>/categorize (bulk or single)
      -> GET /api/statements/<id>/gst (item 8, editable table)
      -> PATCH /api/transactions/<id> (live amount edit -> GST recalced, item 8)
      -> GET /api/statements/<id>/pnl (item 9)
"""

from flask import Blueprint, request, jsonify
from core.db import get_db, log_audit
from core import category_master, gst_engine, pnl_engine, vendor_memory

workflow_bp = Blueprint("workflow", __name__, url_prefix="/api")


def _row_to_txn(row) -> dict:
    return dict(row)


def _hydrate_category_fields(conn, txn: dict) -> dict:
    """Attach category_name/pnl_group/bas_label/gst_rate for engine consumption."""
    if txn.get("category_id"):
        cat = conn.execute("SELECT * FROM categories WHERE id = ?", (txn["category_id"],)).fetchone()
        if cat:
            txn["category_name"] = cat["name"]
            txn["pnl_group"] = cat["pnl_group"]
            txn["bas_label"] = cat["bas_label"]
            txn["gst_rate"] = cat["gst_rate"]
            txn["gst_applicable"] = bool(cat["gst_applicable"])
            return txn
    txn["category_name"] = "Uncategorized"
    txn["pnl_group"] = "Excluded"
    txn["bas_label"] = "excluded"
    txn["gst_rate"] = 0.0
    txn["gst_applicable"] = False
    return txn


# ── Categories (item 6) ─────────────────────────────────────────────────────

@workflow_bp.route("/categories", methods=["GET"])
def get_categories():
    return jsonify(category_master.list_categories())


@workflow_bp.route("/categories", methods=["POST"])
def add_category():
    b = request.json or {}
    cat_id = category_master.create_category(
        code=b["code"], name=b["name"], pnl_group=b["pnl_group"],
        gst_applicable=b.get("gst_applicable", False),
        gst_rate=b.get("gst_rate", 0.10), bas_label=b.get("bas_label", "G11"),
    )
    return jsonify({"id": cat_id})


# ── Statement ingest (parsed -> stored, item 5) ─────────────────────────────

@workflow_bp.route("/statements", methods=["POST"])
def create_statement():
    """Takes the JSON output of /parse and stores it as a reviewable statement."""
    b = request.json or {}
    transactions = b.get("transactions", [])
    bank_id = b.get("bank_id", "unknown")
    filename = b.get("filename", "")
    quarter_id = b.get("quarter_id")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO statements (quarter_id, bank_id, filename, status) VALUES (?,?,?, 'parsed')",
        (quarter_id, bank_id, filename),
    )
    statement_id = cur.lastrowid

    for t in transactions:
        group_key = vendor_memory.normalize_description(t.get("description", ""))
        conn.execute(
            """INSERT INTO transactions
               (statement_id, transaction_id, date, description, amount, balance,
                source_page, row_top, confidence, group_key)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (statement_id, t.get("transaction_id"), t.get("date"), t.get("description"),
             t.get("amount", 0), t.get("balance"), t.get("source_page"),
             t.get("row_top", 0), t.get("confidence"), group_key),
        )
    conn.commit()
    return jsonify({"statement_id": statement_id, "count": len(transactions)})


# ── Approval screen (item 5) ────────────────────────────────────────────────

@workflow_bp.route("/statements/<int:sid>/transactions", methods=["GET"])
def list_statement_transactions(sid):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ? ORDER BY source_page, row_top", (sid,)
    ).fetchall()
    return jsonify([_row_to_txn(r) for r in rows])


@workflow_bp.route("/transactions/<int:tid>", methods=["PATCH"])
def update_transaction(tid):
    """Edit a single transaction (date/description/amount) and/or approve it.
    If amount changes and the row already has a category, GST is recalculated live (item 8)."""
    b = request.json or {}
    conn = get_db()
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not txn:
        return jsonify({"error": "Not found"}), 404

    fields, vals = [], []
    for k in ("date", "description", "amount", "approved", "category_id"):
        if k in b:
            fields.append(f"{k} = ?")
            vals.append(b[k])

    new_amount = b.get("amount", txn["amount"])
    new_category_id = b.get("category_id", txn["category_id"])

    if new_category_id:
        cat = category_master.get_category(new_category_id)
        gst = gst_engine.recalc_transaction_gst(new_amount, cat)
        fields += ["gst_amount = ?", "net_amount = ?"]
        vals += [gst["gst_amount"], gst["net_amount"]]

    if fields:
        vals.append(tid)
        conn.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id = ?", vals)
        conn.commit()
        log_audit("transaction", tid, "edit", str(b))

    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    return jsonify(_row_to_txn(row))


@workflow_bp.route("/statements/<int:sid>/approve", methods=["POST"])
def approve_statement(sid):
    """Bulk-approve all rows still pending, then advance statement status."""
    conn = get_db()
    conn.execute("UPDATE transactions SET approved = 1 WHERE statement_id = ?", (sid,))
    conn.execute("UPDATE statements SET status = 'approved' WHERE id = ?", (sid,))
    conn.commit()
    log_audit("statement", sid, "approve")
    return jsonify({"status": "approved"})


# ── Grouping for bulk categorization (item 7) ───────────────────────────────

@workflow_bp.route("/statements/<int:sid>/groups", methods=["GET"])
def get_groups(sid):
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ?", (sid,)
    ).fetchall()]
    groups = vendor_memory.group_transactions(rows)
    out = [
        {"group_key": k, "count": len(v), "total": round(sum(t["amount"] for t in v), 2),
         "transaction_ids": [t["id"] for t in v], "sample_description": v[0]["description"]}
        for k, v in groups.items()
    ]
    out.sort(key=lambda g: -g["count"])
    return jsonify(out)


# ── Categorization (items 6, 7) ─────────────────────────────────────────────

@workflow_bp.route("/statements/<int:sid>/categorize", methods=["POST"])
def categorize(sid):
    """body: {transaction_ids: [..], category_id: N}  -- works for single or bulk/group assignment."""
    b = request.json or {}
    tids = b.get("transaction_ids", [])
    category_id = b["category_id"]
    client_id = b.get("client_id")  # optional, for vendor memory

    cat = category_master.get_category(category_id)
    if not cat:
        return jsonify({"error": "Unknown category"}), 400

    conn = get_db()
    for tid in tids:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
        if not row:
            continue
        gst = gst_engine.recalc_transaction_gst(row["amount"], cat)
        conn.execute(
            "UPDATE transactions SET category_id = ?, gst_amount = ?, net_amount = ? WHERE id = ?",
            (category_id, gst["gst_amount"], gst["net_amount"], tid),
        )
        if client_id:
            vendor_memory.remember(client_id, row["description"], category_id)
    conn.commit()
    log_audit("statement", sid, "categorize", f"{len(tids)} txns -> {cat['name']}")
    return jsonify({"updated": len(tids)})


@workflow_bp.route("/statements/<int:sid>/suggest", methods=["GET"])
def suggest(sid):
    """Vendor-memory suggestions (Part D path 1) for every uncategorized row in this statement."""
    client_id = request.args.get("client_id", type=int)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ? AND category_id IS NULL", (sid,)
    ).fetchall()
    suggestions = {}
    if client_id:
        for r in rows:
            cat_id = vendor_memory.suggest_category(client_id, r["description"])
            if cat_id:
                suggestions[r["id"]] = cat_id
    return jsonify(suggestions)


# ── GST review (item 8) ─────────────────────────────────────────────────────

@workflow_bp.route("/statements/<int:sid>/gst", methods=["GET"])
def get_gst(sid):
    conn = get_db()
    rows = [_hydrate_category_fields(conn, dict(r)) for r in conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ?", (sid,)
    ).fetchall()]
    return jsonify({
        "transactions": rows,
        "summary": gst_engine.summarize_gst(rows),
    })


@workflow_bp.route("/statements/<int:sid>/finalize_gst", methods=["POST"])
def finalize_gst(sid):
    conn = get_db()
    conn.execute("UPDATE statements SET status = 'gst_reviewed' WHERE id = ?", (sid,))
    conn.commit()
    log_audit("statement", sid, "finalize_gst")
    return jsonify({"status": "gst_reviewed"})


# ── P&L (item 9) ─────────────────────────────────────────────────────────────

@workflow_bp.route("/statements/<int:sid>/pnl", methods=["GET"])
def get_pnl(sid):
    conn = get_db()
    rows = [_hydrate_category_fields(conn, dict(r)) for r in conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ?", (sid,)
    ).fetchall()]
    return jsonify(pnl_engine.generate_pnl(rows))


@workflow_bp.route("/quarters/<int:qid>/pnl", methods=["GET"])
def get_quarter_pnl(qid):
    """Module 8: Consolidation Engine — merges all statements in a quarter."""
    conn = get_db()
    statement_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM statements WHERE quarter_id = ?", (qid,)
    ).fetchall()]
    if not statement_ids:
        return jsonify({"error": "No statements in this quarter"}), 404

    placeholders = ",".join("?" * len(statement_ids))
    rows = [_hydrate_category_fields(conn, dict(r)) for r in conn.execute(
        f"SELECT * FROM transactions WHERE statement_id IN ({placeholders})", statement_ids
    ).fetchall()]
    return jsonify({
        "statement_count": len(statement_ids),
        "pnl": pnl_engine.generate_pnl(rows),
        "gst": gst_engine.summarize_gst(rows),
    })


# ── Quarters / clients (minimal, for Module 2 structure) ───────────────────

@workflow_bp.route("/clients", methods=["GET", "POST"])
def clients():
    conn = get_db()
    if request.method == "POST":
        b = request.json or {}
        cur = conn.execute("INSERT INTO clients (name) VALUES (?)", (b["name"],))
        conn.commit()
        return jsonify({"id": cur.lastrowid})
    return jsonify([dict(r) for r in conn.execute("SELECT * FROM clients").fetchall()])


@workflow_bp.route("/quarters", methods=["GET", "POST"])
def quarters():
    conn = get_db()
    if request.method == "POST":
        b = request.json or {}
        cur = conn.execute(
            "INSERT INTO quarters (client_id, label, period_start, period_end) VALUES (?,?,?,?)",
            (b["client_id"], b["label"], b.get("period_start"), b.get("period_end")),
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid})
    client_id = request.args.get("client_id", type=int)
    q = "SELECT * FROM quarters"
    params = ()
    if client_id:
        q += " WHERE client_id = ?"
        params = (client_id,)
    return jsonify([dict(r) for r in conn.execute(q, params).fetchall()])
