"""
routes/consolidation_routes.py — Consolidation Engine (Module 8)

Handles:
  GET  /api/quarters/<qid>/consolidate/summary    — quarter GST+P&L consolidated (no transaction copy)
  POST /api/quarters/<qid>/consolidate/save       — save a named quarter consolidation report
  GET  /api/quarters/<qid>/consolidate/saved      — list saved quarter consolidations
  GET  /api/quarters/<qid>/consolidate/saved/<cid>— retrieve a saved quarter consolidation

  POST /api/clients/<cid>/consolidate/annual      — consolidate multiple quarters → annual GST+P&L
  GET  /api/clients/<cid>/consolidate/annual      — list annual consolidations for client
  GET  /api/clients/<cid>/consolidate/annual/<aid>— retrieve a saved annual consolidation

All consolidation operates on the already-computed gst_amount/net_amount/category_id on
transactions — no re-calculation, no transaction duplication. Results are stored as JSON
in the consolidation tables so they can be retrieved without recomputing.

Architecture notes:
  - quarter_consolidations  — stores per-quarter consolidated GST+P&L JSON
  - annual_consolidations   — stores per-client annual consolidated GST+P&L JSON spanning N quarters
  - Both tables are client-scoped (via quarters.client_id or direct client_id FK)
  - No transactions are ever copied or duplicated for consolidation
"""

import json
from flask import Blueprint, jsonify, request
from core.db import get_db, log_audit
from core import gst_engine, pnl_engine

consolidation_bp = Blueprint("consolidation", __name__, url_prefix="/api")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hydrate_category_fields(conn, txn: dict) -> dict:
    """Attach category fields needed by gst_engine and pnl_engine."""
    if txn.get("category_id"):
        cat = conn.execute(
            "SELECT * FROM categories WHERE id = ?", (txn["category_id"],)
        ).fetchone()
        if cat:
            txn["category_name"] = cat["name"]
            txn["pnl_group"]     = cat["pnl_group"]
            txn["bas_label"]     = cat["bas_label"]
            txn["gst_rate"]      = cat["gst_rate"]
            txn["gst_applicable"]= bool(cat["gst_applicable"])
            return txn
    txn["category_name"]  = "Uncategorized"
    txn["pnl_group"]      = "Excluded"
    txn["bas_label"]      = "excluded"
    txn["gst_rate"]       = 0.0
    txn["gst_applicable"] = False
    return txn


def _get_quarter_transactions(conn, qid: int) -> list:
    """Return all hydrated transactions for every statement in a quarter."""
    stmt_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM statements WHERE quarter_id = ?", (qid,)
        ).fetchall()
    ]
    if not stmt_ids:
        return []
    placeholders = ",".join("?" * len(stmt_ids))
    rows = [
        dict(r) for r in conn.execute(
            f"SELECT * FROM transactions WHERE statement_id IN ({placeholders})",
            stmt_ids,
        ).fetchall()
    ]
    return [_hydrate_category_fields(conn, r) for r in rows]


def _compute_consolidated(txns: list) -> dict:
    """Run GST + P&L engines over a flat transaction list."""
    return {
        "gst": gst_engine.summarize_gst(txns),
        "pnl": pnl_engine.generate_pnl(txns),
    }


def _statement_summary(conn, sid: int) -> dict:
    """Return a lightweight summary dict for one statement."""
    s = conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
    if not s:
        return {}
    txn_count = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE statement_id = ?", (sid,)
    ).fetchone()[0]
    cat_count = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE statement_id = ? AND category_id IS NOT NULL",
        (sid,),
    ).fetchone()[0]
    return {
        "id":             s["id"],
        "bank_id":        s["bank_id"],
        "statement_name": s["statement_name"] or s["filename"] or f"Statement #{s['id']}",
        "status":         s["status"],
        "txn_count":      txn_count,
        "categorized":    cat_count,
        "created_at":     s["created_at"],
    }


# ── Quarter Consolidation ─────────────────────────────────────────────────────

@consolidation_bp.route("/quarters/<int:qid>/consolidate/summary", methods=["GET"])
def quarter_consolidate_summary(qid):
    """
    Returns a live (not saved) consolidated GST + P&L for all statements in this quarter.
    Also returns per-statement breakdown for transparency.
    Does NOT write anything to DB — use /save to persist.
    """
    conn = get_db()

    # Verify quarter exists and get client context
    quarter = conn.execute("SELECT * FROM quarters WHERE id = ?", (qid,)).fetchone()
    if not quarter:
        return jsonify({"error": "Quarter not found"}), 404

    stmts = conn.execute(
        "SELECT id FROM statements WHERE quarter_id = ?", (qid,)
    ).fetchall()
    if not stmts:
        return jsonify({"error": "No statements in this quarter"}), 404

    # Build per-statement breakdown + collect all transactions
    all_txns = []
    per_statement = []
    for s in stmts:
        sid = s["id"]
        txns = [
            _hydrate_category_fields(conn, dict(r))
            for r in conn.execute(
                "SELECT * FROM transactions WHERE statement_id = ?", (sid,)
            ).fetchall()
        ]
        all_txns.extend(txns)
        info = _statement_summary(conn, sid)
        info["gst"] = gst_engine.summarize_gst(txns)
        info["pnl"] = pnl_engine.generate_pnl(txns)
        per_statement.append(info)

    consolidated = _compute_consolidated(all_txns)

    return jsonify({
        "quarter_id":      qid,
        "quarter_label":   quarter["label"],
        "client_id":       quarter["client_id"],
        "statement_count": len(stmts),
        "txn_count":       len(all_txns),
        "consolidated":    consolidated,
        "per_statement":   per_statement,
    })


@consolidation_bp.route("/quarters/<int:qid>/consolidate/save", methods=["POST"])
def quarter_consolidate_save(qid):
    """
    Saves (or replaces) the consolidated GST+P&L for a quarter.
    body: { name: "Q1 2025 Consolidated" }
    """
    conn = get_db()
    quarter = conn.execute("SELECT * FROM quarters WHERE id = ?", (qid,)).fetchone()
    if not quarter:
        return jsonify({"error": "Quarter not found"}), 404

    b = request.json or {}
    name = b.get("name", f"Q{qid} Consolidated Report")

    txns = _get_quarter_transactions(conn, qid)
    if not txns:
        return jsonify({"error": "No transactions found for this quarter"}), 400

    stmts = conn.execute(
        "SELECT id FROM statements WHERE quarter_id = ?", (qid,)
    ).fetchall()
    stmt_ids = [s["id"] for s in stmts]

    consolidated = _compute_consolidated(txns)
    data_json = json.dumps({
        "consolidated": consolidated,
        "statement_ids": stmt_ids,
        "txn_count": len(txns),
    })

    # Upsert — replace any existing consolidation for this quarter
    existing = conn.execute(
        "SELECT id FROM quarter_consolidations WHERE quarter_id = ?", (qid,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE quarter_consolidations SET consolidation_name = ?, data = ?, created_at = datetime('now') WHERE quarter_id = ?",
            (name, data_json, qid),
        )
        cid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO quarter_consolidations (quarter_id, consolidation_name, data) VALUES (?, ?, ?)",
            (qid, name, data_json),
        )
        cid = cur.lastrowid

    conn.commit()
    log_audit("quarter", qid, "consolidate_save", f"name={name}, txns={len(txns)}")

    return jsonify({
        "id": cid,
        "quarter_id": qid,
        "name": name,
        "txn_count": len(txns),
        "statement_count": len(stmt_ids),
        "consolidated": consolidated,
    })


@consolidation_bp.route("/quarters/<int:qid>/consolidate/saved", methods=["GET"])
def quarter_consolidate_list(qid):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, quarter_id, consolidation_name, created_at FROM quarter_consolidations WHERE quarter_id = ? ORDER BY created_at DESC",
        (qid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@consolidation_bp.route("/quarters/<int:qid>/consolidate/saved/<int:cid>", methods=["GET"])
def quarter_consolidate_get(qid, cid):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM quarter_consolidations WHERE id = ? AND quarter_id = ?", (cid, qid)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = json.loads(row["data"]) if row["data"] else {}
    return jsonify({
        "id":         row["id"],
        "quarter_id": row["quarter_id"],
        "name":       row["consolidation_name"],
        "created_at": row["created_at"],
        **data,
    })


# ── Annual Consolidation ──────────────────────────────────────────────────────

@consolidation_bp.route("/clients/<int:client_id>/consolidate/annual", methods=["POST"])
def annual_consolidate_save(client_id):
    """
    body: { label: "FY 2024-25", quarter_ids: [1, 2, 3, 4] }
    Computes and saves an annual consolidated GST + P&L spanning multiple quarters.
    """
    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": "Client not found"}), 404

    b = request.json or {}
    label = b.get("label", "Annual Consolidation")
    quarter_ids = b.get("quarter_ids", [])

    if not quarter_ids:
        return jsonify({"error": "quarter_ids required"}), 400

    # Verify all quarters belong to this client
    for qid in quarter_ids:
        q = conn.execute(
            "SELECT id FROM quarters WHERE id = ? AND client_id = ?", (qid, client_id)
        ).fetchone()
        if not q:
            return jsonify({"error": f"Quarter {qid} not found for this client"}), 400

    # Collect all transactions from all quarters + per-quarter breakdown
    all_txns = []
    per_quarter = []
    for qid in quarter_ids:
        quarter = conn.execute("SELECT * FROM quarters WHERE id = ?", (qid,)).fetchone()
        txns = _get_quarter_transactions(conn, qid)
        all_txns.extend(txns)
        stmts = conn.execute("SELECT id FROM statements WHERE quarter_id = ?", (qid,)).fetchall()
        per_quarter.append({
            "quarter_id":      qid,
            "quarter_label":   quarter["label"] if quarter else f"Q{qid}",
            "statement_count": len(stmts),
            "txn_count":       len(txns),
            "gst":             gst_engine.summarize_gst(txns),
            "pnl":             pnl_engine.generate_pnl(txns),
        })

    if not all_txns:
        return jsonify({"error": "No transactions found across selected quarters"}), 400

    consolidated = _compute_consolidated(all_txns)
    data_json = json.dumps({
        "consolidated": consolidated,
        "per_quarter":  per_quarter,
        "quarter_ids":  quarter_ids,
        "txn_count":    len(all_txns),
    })

    cur = conn.execute(
        "INSERT INTO annual_consolidations (client_id, label, quarter_ids, data) VALUES (?, ?, ?, ?)",
        (client_id, label, json.dumps(quarter_ids), data_json),
    )
    annual_id = cur.lastrowid
    conn.commit()
    log_audit("client", client_id, "annual_consolidation", f"label={label}, quarters={quarter_ids}, txns={len(all_txns)}")

    return jsonify({
        "id":               annual_id,
        "client_id":        client_id,
        "label":            label,
        "quarter_count":    len(quarter_ids),
        "txn_count":        len(all_txns),
        "consolidated":     consolidated,
        "per_quarter":      per_quarter,
    })


@consolidation_bp.route("/clients/<int:client_id>/consolidate/annual", methods=["GET"])
def annual_consolidate_list(client_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, client_id, label, quarter_ids, created_at FROM annual_consolidations WHERE client_id = ? ORDER BY created_at DESC",
        (client_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["quarter_ids"] = json.loads(r["quarter_ids"])
        except Exception:
            item["quarter_ids"] = []
        result.append(item)
    return jsonify(result)


@consolidation_bp.route("/clients/<int:client_id>/consolidate/annual/<int:aid>", methods=["GET"])
def annual_consolidate_get(client_id, aid):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM annual_consolidations WHERE id = ? AND client_id = ?", (aid, client_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = json.loads(row["data"]) if row["data"] else {}
    return jsonify({
        "id":         row["id"],
        "client_id":  row["client_id"],
        "label":      row["label"],
        "created_at": row["created_at"],
        **data,
    })
