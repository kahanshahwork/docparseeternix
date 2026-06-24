"""
import_export_routes.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles:
  POST /api/import/csv              — import CSV or Excel → statement + transactions
  GET  /api/statements/<id>/export/gst  — download GST summary as .xlsx
  GET  /api/statements/<id>/export/pnl  — download P&L as .xlsx
"""

import io
import os
import re
import csv
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file
from core.db import get_db, log_audit
from core import gst_engine, pnl_engine

ie_bp = Blueprint("import_export", __name__, url_prefix="/api")


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_money(s):
    """Convert '$-1,650.00' / '-1650' / '' → float or None."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ("-", "—"):
        return None
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _normalise_row(raw, idx):
    """Convert a dict from CSV/Excel → canonical transaction dict."""
    def _get(*keys):
        for k in keys:
            v = raw.get(k) or raw.get(k.lower()) or raw.get(k.upper())
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    date        = _get("Date", "date", "DATE", "Trans Date", "Transaction Date")
    description = _get("Description", "Narration", "Details", "Particulars", "Memo")
    debit_raw   = _get("Debit", "debit", "DEBIT", "Withdrawals", "DR")
    credit_raw  = _get("Credit", "credit", "CREDIT", "Deposits", "CR")
    amount_raw  = _get("Amount", "amount", "AMOUNT")

    if debit_raw or credit_raw:
        debit_val  = _parse_money(debit_raw)
        credit_val = _parse_money(credit_raw)
        if credit_val and credit_val != 0:
            amount = abs(credit_val)
        elif debit_val and debit_val != 0:
            amount = -abs(debit_val)
        else:
            amount = 0.0
    elif amount_raw:
        amount = _parse_money(amount_raw) or 0.0
    else:
        amount = 0.0

    return {
        "date":        date,
        "description": description,
        "amount":      amount,
        "source_page": None,
    }


def _read_csv(file_bytes, filename):
    """Parse CSV bytes → list of row dicts."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _read_excel(file_bytes):
    """Parse Excel bytes → list of row dicts (uses openpyxl)."""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl not installed. Run: pip install openpyxl --break-system-packages")
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h).strip() if h else f"col{i}" for i, h in enumerate(rows[0])]
    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        result.append({headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row)})
    return result


# ── CSV / Excel import ───────────────────────────────────────────────────────

@ie_bp.route("/import/csv", methods=["POST"])
def import_csv():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    filename = f.filename or "import"
    ext      = filename.rsplit(".", 1)[-1].lower()
    raw_bytes = f.read()

    try:
        if ext == "csv":
            raw_rows = _read_csv(raw_bytes, filename)
        elif ext in ("xlsx", "xls"):
            raw_rows = _read_excel(raw_bytes)
        else:
            return jsonify({"error": f"Unsupported file type: .{ext}"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    if not raw_rows:
        return jsonify({"error": "File appears empty — no rows found"}), 400

    txns = [_normalise_row(r, i) for i, r in enumerate(raw_rows)]
    txns = [t for t in txns if t["description"] or t["amount"]]

    if not txns:
        return jsonify({"error": "No valid transactions found. Check column names (Date, Description, Debit/Credit or Amount)."}), 400

    # Persist to DB as a new statement
    conn = get_db()
    stmt_name = filename.rsplit(".", 1)[0]
    cur = conn.execute(
        "INSERT INTO statements (name, bank_id, filename, status, created_at) VALUES (?,?,?,?,?)",
        (stmt_name, "csv-import", filename, "parsed", datetime.utcnow().isoformat())
    )
    sid = cur.lastrowid
    for t in txns:
        conn.execute(
            "INSERT INTO transactions (statement_id, date, description, amount, source_page) VALUES (?,?,?,?,?)",
            (sid, t["date"], t["description"], t["amount"], t["source_page"])
        )
    conn.commit()
    log_audit("statement", sid, "csv_import")

    # Return transactions with IDs for the frontend
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE statement_id = ? ORDER BY id", (sid,)
    ).fetchall()]

    return jsonify({"statement_id": sid, "transactions": rows})


# ── GST Excel export ─────────────────────────────────────────────────────────

@ie_bp.route("/statements/<int:sid>/export/gst", methods=["GET"])
def export_gst(sid):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return "openpyxl not installed", 500

    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT t.*, c.name as category_name, c.pnl_group, c.bas_label, c.has_gst "
        "FROM transactions t LEFT JOIN categories c ON t.category_id = c.id "
        "WHERE t.statement_id = ?", (sid,)
    ).fetchall()]

    summary = gst_engine.summarize_gst(rows)
    bas     = summary.get("bas", {})
    by_cat  = summary.get("by_category", [])

    wb = openpyxl.Workbook()

    # ── Sheet 1: BAS Summary ──
    ws1 = wb.active
    ws1.title = "BAS Summary"

    hdr_fill  = PatternFill("solid", fgColor="1C3557")
    hdr_font  = Font(color="FFFFFF", bold=True, size=11)
    bold      = Font(bold=True)
    mono      = Font(name="Courier New", size=10)
    center    = Alignment(horizontal="center")
    right     = Alignment(horizontal="right")
    thin      = Side(style="thin", color="CCCCCC")
    border    = Border(bottom=thin)

    bas_fields = [
        ("G1",  "Total Sales (inc. GST)"),
        ("1A",  "GST on Sales"),
        ("G10", "Total Purchases (inc. GST)"),
        ("1B",  "GST Credits on Purchases"),
    ]

    ws1.append(["BAS Field", "Label", "Amount"])
    for cell in ws1[1]:
        cell.font = hdr_font; cell.fill = hdr_fill; cell.alignment = center
    for key, label in bas_fields:
        ws1.append([key, label, bas.get(key, 0)])
        ws1.cell(ws1.max_row, 3).number_format = '#,##0.00'
        ws1.cell(ws1.max_row, 3).alignment = right

    ws1.append([])
    net = (bas.get("1A", 0) or 0) - (bas.get("1B", 0) or 0)
    ws1.append(["NET GST", "Payable" if net >= 0 else "Refundable", net])
    r = ws1.max_row
    for c in [1, 2, 3]:
        ws1.cell(r, c).font = bold
    ws1.cell(r, 3).number_format = '#,##0.00'
    ws1.cell(r, 3).alignment = right

    ws1.column_dimensions["A"].width = 12
    ws1.column_dimensions["B"].width = 36
    ws1.column_dimensions["C"].width = 18

    # ── Sheet 2: Category Breakdown ──
    ws2 = wb.create_sheet("By Category")
    ws2.append(["Category", "P&L Group", "BAS Label", "Gross (inc GST)", "GST Amount", "Net (ex GST)", "Txn Count"])
    for cell in ws2[1]:
        cell.font = hdr_font; cell.fill = hdr_fill; cell.alignment = center
    for cat in by_cat:
        ws2.append([
            cat.get("category", ""),
            cat.get("pnl_group", ""),
            cat.get("bas_label", ""),
            cat.get("gross", 0),
            cat.get("gst", 0),
            cat.get("net", 0),
            cat.get("count", 0),
        ])
        for col in [4, 5, 6]:
            ws2.cell(ws2.max_row, col).number_format = '#,##0.00'
    for col in ["A","B","C","D","E","F","G"]:
        ws2.column_dimensions[col].width = 22

    # ── Sheet 3: Transaction Detail ──
    ws3 = wb.create_sheet("Transactions")
    ws3.append(["Date", "Description", "Category", "BAS Label", "Amount (inc GST)", "GST (÷11)", "Net (ex GST)"])
    for cell in ws3[1]:
        cell.font = hdr_font; cell.fill = hdr_fill; cell.alignment = center
    for t in rows:
        amt = t.get("amount", 0) or 0
        has_gst = bool(t.get("has_gst"))
        gst_amt = round(amt / 11, 2) if has_gst else 0
        net_amt = round(amt - gst_amt, 2)
        ws3.append([
            t.get("date", ""),
            t.get("description", ""),
            t.get("category_name") or "Uncategorized",
            t.get("bas_label") or "",
            amt, gst_amt, net_amt,
        ])
        for col in [5, 6, 7]:
            ws3.cell(ws3.max_row, col).number_format = '#,##0.00'
    ws3.column_dimensions["A"].width = 14
    ws3.column_dimensions["B"].width = 50
    ws3.column_dimensions["C"].width = 24
    ws3.column_dimensions["D"].width = 14
    for col in ["E","F","G"]:
        ws3.column_dimensions[col].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"gst_summary_stmt{sid}.xlsx")


# ── P&L Excel export ──────────────────────────────────────────────────────────

@ie_bp.route("/statements/<int:sid>/export/pnl", methods=["GET"])
def export_pnl(sid):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl not installed", 500

    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT t.*, c.name as category_name, c.pnl_group, c.bas_label, c.has_gst "
        "FROM transactions t LEFT JOIN categories c ON t.category_id = c.id "
        "WHERE t.statement_id = ?", (sid,)
    ).fetchall()]

    pnl = pnl_engine.generate_pnl(rows)

    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Profit & Loss"

    hdr_fill = PatternFill("solid", fgColor="1C3557")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)
    bold     = Font(bold=True)
    right    = Alignment(horizontal="right")
    grn_fill = PatternFill("solid", fgColor="D1FAE5")
    red_fill = PatternFill("solid", fgColor="FEE2E2")

    def section(title, lines, total, fill):
        ws.append([title, ""])
        for cell in ws[ws.max_row]:
            cell.font = hdr_font; cell.fill = hdr_fill
        for line in lines:
            ws.append(["  " + (line.get("category") or ""), line.get("amount", 0)])
            ws.cell(ws.max_row, 2).number_format = '#,##0.00'
            ws.cell(ws.max_row, 2).alignment = right
        ws.append(["Total " + title, total])
        r = ws.max_row
        ws.cell(r, 1).font = bold; ws.cell(r, 2).font = bold
        ws.cell(r, 2).number_format = '#,##0.00'
        ws.cell(r, 2).alignment = right
        for c in [1, 2]:
            ws.cell(r, c).fill = fill
        ws.append([])

    section("Income",   pnl.get("income_lines",  []), pnl.get("total_income",  0), grn_fill)
    section("Expenses", pnl.get("expense_lines", []), pnl.get("total_expense", 0), red_fill)

    net = pnl.get("net_profit", 0)
    ws.append(["Net Profit / Loss", net])
    r = ws.max_row
    ws.cell(r, 1).font = Font(bold=True, size=13)
    ws.cell(r, 2).font = Font(bold=True, size=13)
    ws.cell(r, 2).number_format = '#,##0.00'
    ws.cell(r, 2).alignment = right
    net_fill = PatternFill("solid", fgColor=("D1FAE5" if net >= 0 else "FEE2E2"))
    ws.cell(r, 1).fill = net_fill; ws.cell(r, 2).fill = net_fill

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"pnl_stmt{sid}.xlsx")
