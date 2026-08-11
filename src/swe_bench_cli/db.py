"""SQLite logging: every command invocation + per-test results."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(".task-bundle/log.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT NOT NULL,
    task_id      TEXT,
    args_json    TEXT,
    status       TEXT NOT NULL,
    harness      TEXT,
    image        TEXT,
    solver_patch TEXT,
    transcript   TEXT,
    summary      TEXT,
    error        TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    duration_sec REAL
);
CREATE TABLE IF NOT EXISTS test_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id         INTEGER NOT NULL REFERENCES commands(id),
    bucket             TEXT NOT NULL,
    test_id            TEXT NOT NULL,
    phase              TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    expected           TEXT,
    passed_expectation INTEGER,
    duration_sec       REAL
);
"""


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # ponytail: small migrations for DBs created before these columns existed.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(commands)")}
    if "transcript" not in cols:
        conn.execute("ALTER TABLE commands ADD COLUMN transcript TEXT")
        conn.commit()
    if "harness" not in cols and "solver" in cols:
        conn.execute("ALTER TABLE commands RENAME COLUMN solver TO harness")
        conn.commit()
    return conn


def start_command(conn, command, task_id, args, started_at) -> int:
    cur = conn.execute(
        "INSERT INTO commands (command, task_id, args_json, status, started_at) "
        "VALUES (?,?,?,?,?)",
        (command, task_id, json.dumps(args), "running", started_at),
    )
    conn.commit()
    return cur.lastrowid


def finish_command(conn, command_id, **fields) -> None:
    keys = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE commands SET {keys} WHERE id=?", (*fields.values(), command_id))
    conn.commit()


def add_test_result(conn, command_id, bucket, test_id, phase, outcome,
                    expected, duration_sec=None) -> None:
    conn.execute(
        "INSERT INTO test_results (command_id, bucket, test_id, phase, outcome, "
        "expected, passed_expectation, duration_sec) VALUES (?,?,?,?,?,?,?,?)",
        (command_id, bucket, test_id, phase, outcome, expected,
         int(outcome == expected), duration_sec),
    )
    conn.commit()


def get_command(conn, command_id):
    cmd = conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    if not cmd:
        return None, []
    results = conn.execute(
        "SELECT * FROM test_results WHERE command_id=? ORDER BY bucket, test_id",
        (command_id,),
    ).fetchall()
    return cmd, results


def list_commands(conn, limit=20):
    return conn.execute(
        "SELECT id, command, task_id, status, harness, started_at FROM commands "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
