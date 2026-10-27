import os
import secrets
import sqlite3

from flask import g

DB_PATH = os.environ.get("DATABASE_PATH", "/data/accounts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    vault_created TEXT,
    vault_updated TEXT,
    status TEXT NOT NULL DEFAULT 'unreviewed',
    priority INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    vault_removed INTEGER NOT NULL DEFAULT 0,
    snoozed_until TEXT,
    imported_at TEXT NOT NULL,
    status_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id);

CREATE TABLE IF NOT EXISTS daily_picks (
    day TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (day, account_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Create tables and return the persistent session secret."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('secret_key', ?)",
            (secrets.token_hex(32),),
        )
        conn.commit()
        return conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()[0]
    finally:
        conn.close()


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
