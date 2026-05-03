"""
MIS Database Migration - migrate_db.py
Reads config.json, creates SQLite tables matching collector schema.
Safe to re-run (uses CREATE IF NOT EXISTS).

Usage: python migrate_db.py
"""

import json
import sqlite3
import os
import sys

AREA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AREA_DIR, "config.json")
DB_FILE = os.path.join(AREA_DIR, "mis.db")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: config.json not found in {AREA_DIR}")
        sys.exit(1)
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def migrate(cfg):
    area = cfg["area"]
    machines = cfg["machines"]
    print(f"  Area: {area['code']} - {area['name']}")
    print(f"  Machines: {len(machines)}")
    print(f"  Database: {DB_FILE}")

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS machine_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT NOT NULL,
            machine_id       TEXT NOT NULL,
            machine_name     TEXT NOT NULL,
            op               TEXT,
            grp              TEXT,
            state_id         INTEGER DEFAULT 0,
            state_label      TEXT,
            oee_category     TEXT,
            alarm_code       INTEGER DEFAULT 0,
            good_part_count  INTEGER DEFAULT 0,
            bad_part_count   INTEGER DEFAULT 0,
            support_call     INTEGER DEFAULT 0,
            support_short    TEXT,
            down_start       TEXT,
            down_minutes     REAL,
            tool0 INTEGER DEFAULT 0, tool1 INTEGER DEFAULT 0,
            tool2 INTEGER DEFAULT 0, tool3 INTEGER DEFAULT 0,
            tool4 INTEGER DEFAULT 0, tool5 INTEGER DEFAULT 0,
            tool6 INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS state_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT NOT NULL,
            machine_id   TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            op           TEXT,
            state_id     INTEGER,
            state_label  TEXT,
            oee_category TEXT,
            alarm_code   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS downtime_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            down_start      TEXT NOT NULL,
            down_end        TEXT,
            machine_id      TEXT NOT NULL,
            machine_name    TEXT NOT NULL,
            op              TEXT,
            duration_min    REAL,
            alarm_code      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS support_call_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_start TEXT NOT NULL,
            timestamp_end   TEXT,
            machine_id      TEXT NOT NULL,
            machine_name    TEXT NOT NULL,
            support_call    INTEGER,
            support_short   TEXT,
            duration_min    REAL
        );

        CREATE TABLE IF NOT EXISTS tool_change_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT NOT NULL,
            machine_id   TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            op           TEXT,
            tool_slot    INTEGER,
            parts_run    INTEGER,
            shift_letter TEXT,
            day_id       TEXT
        );

        CREATE TABLE IF NOT EXISTS consec_good_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            machine_id    TEXT NOT NULL,
            machine_name  TEXT NOT NULL,
            minutes_since TEXT
        );

        CREATE TABLE IF NOT EXISTS plc_status (
            ip                TEXT PRIMARY KEY,
            last_success      TEXT,
            last_error        TEXT,
            error_msg         TEXT,
            consecutive_fails INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS demand_targets (
            day_id       TEXT NOT NULL,
            machine_id   TEXT NOT NULL,
            shift_target INTEGER NOT NULL,
            daily_target INTEGER NOT NULL,
            daily_demand INTEGER NOT NULL DEFAULT 0,
            pushed_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (day_id, machine_id)
        );

        CREATE INDEX IF NOT EXISTS idx_snap_mid_ts
            ON machine_snapshots (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_state_mid_ts
            ON state_events (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_down_mid
            ON downtime_events (machine_id, down_start);
        CREATE INDEX IF NOT EXISTS idx_tool_mid_ts
            ON tool_change_events (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_day
            ON tool_change_events (day_id, machine_id);
        CREATE INDEX IF NOT EXISTS idx_demand_day
            ON demand_targets (day_id);
    """)

    conn.commit()
    conn.close()
    print(f"  OK - database ready")


if __name__ == "__main__":
    print("MIS Database Migration")
    print("=" * 40)
    cfg = load_config()
    migrate(cfg)
    print("=" * 40)
    print("Done.")
