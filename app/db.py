"""SQLite staging store for plans, runs, quarantine evidence, and the mock target."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self):
        self._lock.acquire()
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._lock.release()

    def _init(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    parent_id TEXT,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    approval_note TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_retry INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    rolled_back_at TEXT,
                    source_count INTEGER NOT NULL,
                    transformed_count INTEGER NOT NULL,
                    accepted_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    duplicate_skipped_count INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    source_value TEXT,
                    rule TEXT NOT NULL,
                    message TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    case_id TEXT
                );
                CREATE TABLE IF NOT EXISTS quarantine_cases (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_index INTEGER NOT NULL,
                    source_key TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    correction_json TEXT,
                    status TEXT NOT NULL,
                    released_run_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS target_customers (
                    customer_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    load_run_id TEXT NOT NULL,
                    loaded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    plan_id TEXT,
                    run_id TEXT,
                    detail TEXT NOT NULL
                );
                """
            )
            for name in ("plan", "run"):
                connection.execute("INSERT OR IGNORE INTO counters(name, value) VALUES (?, 0)", (name,))
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(quarantine)").fetchall()}
            if "case_id" not in columns:
                connection.execute("ALTER TABLE quarantine ADD COLUMN case_id TEXT")

    def next_id(self, connection: sqlite3.Connection, kind: str) -> str:
        row = connection.execute("SELECT value FROM counters WHERE name = ?", (kind,)).fetchone()
        value = int(row["value"]) + 1
        connection.execute("UPDATE counters SET value = ? WHERE name = ?", (value, kind))
        return f"{kind}-{value:03d}"

    def add_history(self, connection: sqlite3.Connection, event_type: str, detail: dict, plan_id=None, run_id=None):
        connection.execute(
            "INSERT INTO history(at, event_type, plan_id, run_id, detail) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), event_type, plan_id, run_id, json.dumps(detail, ensure_ascii=False)),
        )


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def load(value: str):
    return json.loads(value)
