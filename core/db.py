"""
core/db.py — Single source of truth for persistence.

Touch this file ONLY when changing table schema or adding a new table.
Every other module talks to the DB through get_db() + plain SQL — no ORM,
so there's nothing else to keep in sync when you add a column.
"""

import sqlite3
import os
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "docparse.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """Thread-local connection so Flask's dev server (threaded) doesn't share cursors."""
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # concurrent reads+writes
        conn.execute("PRAGMA synchronous = NORMAL")  # safe + faster with WAL
        _local.conn = conn
    return _local.conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    business_type TEXT NOT NULL DEFAULT 'RETAIL_TRADING',
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quarters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES clients(id),
    label        TEXT NOT NULL,
    period_start TEXT,
    period_end   TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS statements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter_id     INTEGER REFERENCES quarters(id),
    bank_id        TEXT NOT NULL,
    filename       TEXT,
    statement_name TEXT,
    status         TEXT NOT NULL DEFAULT 'parsed',
    uploaded_by    TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    pnl_group      TEXT NOT NULL,
    gst_applicable INTEGER NOT NULL DEFAULT 0,
    gst_rate       REAL NOT NULL DEFAULT 0.10,
    bas_label      TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    sort_order     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id   INTEGER NOT NULL REFERENCES statements(id),
    transaction_id TEXT,
    date           TEXT,
    description    TEXT,
    amount         REAL NOT NULL,
    balance        REAL,
    source_page    INTEGER,
    row_top        REAL,
    confidence     REAL,
    approved       INTEGER NOT NULL DEFAULT 0,
    category_id    INTEGER REFERENCES categories(id),
    gst_amount     REAL DEFAULT 0,
    net_amount     REAL,
    group_key      TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendor_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER REFERENCES clients(id),
    pattern     TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    hit_count   INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(client_id, pattern)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT,
    actor       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_usage_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id       INTEGER,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,
    limit_requests     INTEGER,
    remaining_requests INTEGER,
    limit_tokens       INTEGER,
    remaining_tokens   INTEGER,
    created_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quarter_consolidations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter_id         INTEGER NOT NULL REFERENCES quarters(id),
    consolidation_name TEXT NOT NULL DEFAULT 'Consolidated Report',
    created_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS annual_consolidations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    label       TEXT NOT NULL,
    quarter_ids TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
"""


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Safe, idempotent schema migrations for existing databases."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "business_type" not in cols:
        conn.execute(
            "ALTER TABLE clients ADD COLUMN business_type TEXT NOT NULL DEFAULT 'RETAIL_TRADING'"
        )
        conn.commit()
        print("[db] Migrated: added business_type to clients.")

    stmt_cols = [row[1] for row in conn.execute("PRAGMA table_info(statements)").fetchall()]
    if "statement_name" not in stmt_cols:
        conn.execute("ALTER TABLE statements ADD COLUMN statement_name TEXT")
        conn.commit()
        print("[db] Migrated: added statement_name to statements.")


def log_audit(entity_type: str, entity_id: int, action: str, detail: str = "", actor: str = "user"):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail, actor) VALUES (?,?,?,?,?)",
        (entity_type, entity_id, action, detail, actor),
    )
    conn.commit()
