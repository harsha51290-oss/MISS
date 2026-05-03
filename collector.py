"""
MIS Collector - collector.py
Standard MISS PLC data collector.
Reads config.json for machine registry and connection settings.

Polling Tiers:
  Tier 1   - every 1 sec    - StateID only (fast state detection)
  Tier 1a  - on fault       - AlarmCode (StateID = 3 or 4 only)
  Tier 2   - every 15 sec   - GoodPartCount, BadPartCount,
                               SupportCall, HMI_SupportReqPB,
                               No_Of_Tools, ToolCount[0..6]

Down Timer Logic:
  Machine leaves State 1 or 15
    -> pending timer starts
    -> if still not in 1/15 after 20 sec -> record down_start
  Machine returns to State 1 or 15
    -> recovery timer starts
    -> if stays in 1/15 for 60 sec -> clear down_start (recovered)
    -> if leaves 1/15 within 60 sec -> cancel recovery, still down

MIS UDT - 7 members per machine:
  StateID          INT
  GoodPartCount    INT
  BadPartCount     INT
  AlarmCode        INT
  SupportCall      INT
  HMI_SupportReqPB INT
  ToolCount        INT[7]

Per-machine IP: Each machine can have its own IP address.
Machines sharing the same IP are batched into a single PLC read.

Install: pip install pylogix
Run:     python collector.py
"""

import json
import sqlite3
import time
import logging
import threading
import os
from datetime import datetime, timedelta
from pylogix import PLC

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

AREA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AREA_DIR, "config.json")
DB_FILE = os.path.join(AREA_DIR, "mis.db")
LOG_DIR = os.path.join(AREA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "collector.log")),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

RUNNING_STATES = {1, 15}

STATE_LABELS = {
    1: "Running", 2: "Stopped", 3: "Faulted (Unattended)",
    4: "Faulted (Attended)", 5: "Powered Off (Guard Door)",
    6: "Powered Off (E-Stop)", 7: "Blocked", 8: "Starved",
    9: "Paused Stats", 10: "Manual Override", 11: "Hold Active",
    12: "Door Open", 13: "Warmup", 14: "Dressing",
    15: "Partial Running",
}

OEE_CATEGORY = {
    1: "productive", 2: "planned_downtime",
    3: "unplanned_downtime", 4: "unplanned_downtime",
    5: "unplanned_downtime", 6: "unplanned_downtime",
    7: "minor_stop", 8: "minor_stop", 9: "minor_stop",
    10: "planned_downtime", 11: "minor_stop",
    12: "unplanned_downtime", 13: "planned_downtime",
    14: "planned_downtime", 15: "productive",
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

_config_mtime = 0

def config_changed():
    global _config_mtime
    try:
        mt = os.path.getmtime(CONFIG_FILE)
        if mt != _config_mtime:
            _config_mtime = mt
            return True
    except OSError:
        pass
    return False

# ---------------------------------------------------------------------------
# Group machines by IP for efficient batch reads
# ---------------------------------------------------------------------------

def group_by_ip(machines):
    """Group machines by (ip, slot, program_prefix) for batch PLC reads."""
    groups = {}
    for m in machines:
        key = (m["ip"], m.get("slot", 0), m.get("program_prefix", "Program:Illuminate"))
        if key not in groups:
            groups[key] = []
        groups[key].append(m)
    return groups

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn):
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
            ip           TEXT PRIMARY KEY,
            last_success TEXT,
            last_error   TEXT,
            error_msg    TEXT,
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
    log.info("Database initialized.")


# ---------------------------------------------------------------------------
# Shared state (protected by lock)
# ---------------------------------------------------------------------------

lock = threading.Lock()


def init_state(machines):
    """Initialize per-machine tracking state."""
    state = {
        "current_state": {},
        "current_alarm": {},
        "prev_support": {},
        "support_start": {},
        "prev_good": {},
        "prev_bad": {},
        "consec_count": {},
        "last_consec_t": {},
        "prev_tool": {},
        "down_start": {},
        "down_pending_t": {},
        "recover_start_t": {},
    }
    for m in machines:
        mid = m["id"]
        state["current_state"][mid] = 0
        state["current_alarm"][mid] = 0
        state["prev_support"][mid] = 0
        state["support_start"][mid] = None
        state["prev_good"][mid] = 0
        state["prev_bad"][mid] = 0
        state["consec_count"][mid] = 0
        state["last_consec_t"][mid] = None
        state["prev_tool"][mid] = [0] * 7
        state["down_start"][mid] = None
        state["down_pending_t"][mid] = None
        state["recover_start_t"][mid] = None
    return state


# ---------------------------------------------------------------------------
# Down timer logic
# ---------------------------------------------------------------------------

def process_down_timer(conn, mid, m, sid, now, now_ts, st, cfg_local):
    """
    Down timer:
      Machine leaves 1/15 -> 20s pending -> confirmed down
      Machine returns to 1/15 -> 60s recovery -> confirmed recovered
    """
    is_running = sid in RUNNING_STATES
    down_confirm = cfg_local.get("down_confirm_sec", 20)
    recover_confirm = cfg_local.get("recover_confirm_sec", 60)

    with lock:
        ds = st["down_start"][mid]
        dpt = st["down_pending_t"][mid]
        rst = st["recover_start_t"][mid]

    if is_running:
        with lock:
            st["down_pending_t"][mid] = None

        if ds is not None:
            if rst is None:
                with lock:
                    st["recover_start_t"][mid] = now
            else:
                elapsed_recovery = (now - rst).total_seconds()
                if elapsed_recovery >= recover_confirm:
                    dur = round((now - datetime.fromisoformat(ds)).total_seconds() / 60, 1)
                    conn.execute("""
                        UPDATE downtime_events SET down_end=?, duration_min=?
                        WHERE machine_id=? AND down_end IS NULL
                    """, (now_ts, dur, mid))
                    conn.commit()
                    log.info(f"RECOV  {m['name']:20s} recovered after {dur} min")
                    with lock:
                        st["down_start"][mid] = None
                        st["recover_start_t"][mid] = None
        else:
            with lock:
                st["recover_start_t"][mid] = None
    else:
        with lock:
            st["recover_start_t"][mid] = None

        if ds is None:
            if dpt is None:
                with lock:
                    st["down_pending_t"][mid] = now
            else:
                elapsed_pending = (now - dpt).total_seconds()
                if elapsed_pending >= down_confirm:
                    alm = st["current_alarm"][mid]
                    conn.execute("""
                        INSERT INTO downtime_events
                        (down_start, machine_id, machine_name, op, alarm_code)
                        VALUES (?,?,?,?,?)
                    """, (now_ts, mid, m["name"], m["op"], alm))
                    conn.commit()
                    log.info(f"DOWN   {m['name']:20s} confirmed down (alarm {alm})")
                    with lock:
                        st["down_start"][mid] = now_ts
                        st["down_pending_t"][mid] = None


# ---------------------------------------------------------------------------
# TIER 1 - StateID every 1 second + AlarmCode on fault
# ---------------------------------------------------------------------------

def tier1_loop(conn, machines, ip_groups, st, cfg_local):
    tier1_interval = cfg_local.get("tier1_sec", 1)

    while True:
        t_start = time.time()
        try:
            now = datetime.now()
            now_ts = now.isoformat()

            for (ip, slot, prog), group_machines in ip_groups.items():
                # Build StateID tags for this IP group
                tags = [f"{prog}.{m['udt_tag']}.StateID" for m in group_machines]

                try:
                    with PLC() as comm:
                        comm.IPAddress = ip
                        comm.ProcessorSlot = slot
                        results = comm.Read(tags)

                    # Update PLC status - success
                    conn.execute("""
                        INSERT INTO plc_status (ip, last_success, consecutive_fails)
                        VALUES (?, ?, 0)
                        ON CONFLICT(ip) DO UPDATE SET
                            last_success=excluded.last_success,
                            last_error=NULL, error_msg=NULL, consecutive_fails=0
                    """, (ip, now_ts))
                    conn.commit()

                    alarm_tags = []
                    alarm_mids = []

                    for i, m in enumerate(group_machines):
                        mid = m["id"]
                        try:
                            sid = int(results[i].Value or 0)
                        except Exception:
                            sid = 0

                        with lock:
                            prev_sid = st["current_state"][mid]

                        if sid != prev_sid:
                            label = STATE_LABELS.get(sid, f"Unknown ({sid})")
                            oee = OEE_CATEGORY.get(sid, "unknown")
                            alm = st["current_alarm"][mid]

                            conn.execute("""
                                INSERT INTO state_events
                                (timestamp, machine_id, machine_name, op,
                                 state_id, state_label, oee_category, alarm_code)
                                VALUES (?,?,?,?,?,?,?,?)
                            """, (now_ts, mid, m["name"], m["op"],
                                  sid, label, oee, alm))
                            conn.commit()
                            log.info(f"STATE  {m['name']:20s} "
                                     f"{STATE_LABELS.get(prev_sid, '?'):25s} -> {label}")
                            with lock:
                                st["current_state"][mid] = sid

                        process_down_timer(conn, mid, m, sid, now, now_ts, st, cfg_local)

                        if sid in (3, 4):
                            alarm_tags.append(f"{prog}.{m['udt_tag']}.AlarmCode")
                            alarm_mids.append(mid)

                    # Tier 1a - read AlarmCode for faulted machines
                    if alarm_tags:
                        try:
                            with PLC() as comm2:
                                comm2.IPAddress = ip
                                comm2.ProcessorSlot = slot
                                alm_res = comm2.Read(alarm_tags)
                            for j, amid in enumerate(alarm_mids):
                                try:
                                    alm = int(alm_res[j].Value or 0)
                                except Exception:
                                    alm = 0
                                with lock:
                                    st["current_alarm"][amid] = alm
                        except Exception as e:
                            log.warning(f"AlarmCode read error ({ip}): {e}")

                except Exception as e:
                    # PLC connection failed - update status
                    conn.execute("""
                        INSERT INTO plc_status (ip, last_error, error_msg, consecutive_fails)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(ip) DO UPDATE SET
                            last_error=excluded.last_error,
                            error_msg=excluded.error_msg,
                            consecutive_fails=consecutive_fails+1
                    """, (ip, now_ts, str(e)))
                    conn.commit()
                    fails = conn.execute(
                        "SELECT consecutive_fails FROM plc_status WHERE ip=?", (ip,)
                    ).fetchone()
                    fail_count = fails[0] if fails else 0
                    if fail_count <= 3 or fail_count % 60 == 0:
                        log.error(f"T1 PLC error ({ip}): {e} [fail #{fail_count}]")

        except Exception as e:
            log.error(f"Tier1 error: {e}")

        elapsed = time.time() - t_start
        time.sleep(max(0, tier1_interval - elapsed))


# ---------------------------------------------------------------------------
# TIER 2 - Full snapshot every 15 seconds
# ---------------------------------------------------------------------------

def tier2_loop(conn, machines, ip_groups, st, cfg_local, cfg):
    tier2_interval = cfg_local.get("tier2_sec", 15)
    tool_threshold = cfg_local.get("tool_change_threshold", 50)
    consec_target = cfg_local.get("consecutive_good_target", 10)
    support_codes = cfg.get("support_codes", {})

    while True:
        t_start = time.time()
        try:
            now = datetime.now()
            now_ts = now.isoformat()

            # Shift info
            h_now = now.hour
            s_ltr = "A" if 6 <= h_now < 14 else "B" if 14 <= h_now < 22 else "C"
            if h_now >= 22:
                d_id = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                d_id = now.strftime("%Y-%m-%d")

            for (ip, slot, prog), group_machines in ip_groups.items():
                tags = []
                for m in group_machines:
                    p = f"{prog}.{m['udt_tag']}"
                    tags += [
                        f"{p}.GoodPartCount",
                        f"{p}.BadPartCount",
                        f"{p}.SupportCall",
                        f"{p}.HMI_SupportReqPB",
                        f"{p}.No_Of_Tools",
                        f"{p}.ToolCount[0]",
                        f"{p}.ToolCount[1]",
                        f"{p}.ToolCount[2]",
                        f"{p}.ToolCount[3]",
                        f"{p}.ToolCount[4]",
                        f"{p}.ToolCount[5]",
                        f"{p}.ToolCount[6]",
                    ]
                TAGS_PER = 12

                try:
                    with PLC() as comm:
                        comm.IPAddress = ip
                        comm.ProcessorSlot = slot
                        results = comm.Read(tags)
                except Exception as e:
                    log.error(f"T2 PLC error ({ip}): {e}")
                    continue

                for i, m in enumerate(group_machines):
                    mid = m["id"]
                    base = i * TAGS_PER

                    try:
                        good_count = int(results[base].Value or 0)
                        bad_count = int(results[base + 1].Value or 0)
                        support_call = int(results[base + 2].Value or 0)
                        hmi_pb = int(results[base + 3].Value or 0)
                        no_of_tools = max(1, min(7, int(results[base + 4].Value or 7)))
                        tools = []
                        for t in range(7):
                            try:
                                tools.append(int(results[base + 5 + t].Value or 0))
                            except Exception:
                                tools.append(0)
                    except Exception as e:
                        log.warning(f"T2 parse {mid}: {e}")
                        continue

                    with lock:
                        sid = st["current_state"][mid]
                        alarm_code = st["current_alarm"][mid]
                        pg = st["prev_good"][mid]
                        pb = st["prev_bad"][mid]
                        ps = st["prev_support"][mid]
                        pt = st["prev_tool"][mid][:]
                        ds = st["down_start"][mid]

                    state_label = STATE_LABELS.get(sid, f"Unknown ({sid})")
                    oee_cat = OEE_CATEGORY.get(sid, "unknown")
                    sup_code = str(support_call)
                    sup_info = support_codes.get(sup_code, {"short": "--"})
                    sup_short = sup_info["short"] if isinstance(sup_info, dict) else sup_info

                    # Down minutes (calculated live)
                    down_min = None
                    if ds:
                        down_min = round(
                            (now - datetime.fromisoformat(ds)).total_seconds() / 60, 1
                        )

                    # -- Support call change --
                    if support_call != ps:
                        if ps > 0 and st["support_start"][mid]:
                            dur = round((now - datetime.fromisoformat(
                                st["support_start"][mid])).total_seconds() / 60, 1)
                            conn.execute("""
                                UPDATE support_call_events
                                SET timestamp_end=?, duration_min=?
                                WHERE machine_id=? AND timestamp_end IS NULL
                            """, (now_ts, dur, mid))
                            conn.commit()
                        if support_call > 0:
                            conn.execute("""
                                INSERT INTO support_call_events
                                (timestamp_start, machine_id, machine_name,
                                 support_call, support_short)
                                VALUES (?,?,?,?,?)
                            """, (now_ts, mid, m["name"], support_call, sup_short))
                            conn.commit()
                            log.info(f"TRADE  {m['name']:20s} {sup_short}")
                        with lock:
                            st["prev_support"][mid] = support_call
                            st["support_start"][mid] = now_ts if support_call > 0 else None

                    # -- Tool change detection --
                    for slot_num in range(no_of_tools):
                        prev_val = pt[slot_num]
                        curr_val = tools[slot_num]
                        if prev_val > tool_threshold and curr_val < (prev_val - tool_threshold):
                            life = prev_val
                            conn.execute("""
                                INSERT INTO tool_change_events
                                (timestamp, machine_id, machine_name, op,
                                 tool_slot, parts_run, shift_letter, day_id)
                                VALUES (?,?,?,?,?,?,?,?)
                            """, (now_ts, mid, m["name"], m["op"],
                                  slot_num, life, s_ltr, d_id))
                            conn.commit()
                            log.info(f"TOOL   {m['name']:20s} "
                                     f"slot {slot_num} changed - {life:,} parts run "
                                     f"({s_ltr} shift, day {d_id})")
                    with lock:
                        st["prev_tool"][mid] = tools[:]

                    # -- Consecutive good parts --
                    cc = st["consec_count"][mid]
                    lct = st["last_consec_t"][mid]
                    new_good = good_count - pg
                    new_bad = bad_count - pb

                    if new_good > 0:
                        cc += new_good
                        if cc >= consec_target:
                            mins = None
                            if lct:
                                delta = now - datetime.fromisoformat(lct)
                                mins = round(delta.total_seconds() / 60, 1)
                            lct = now_ts
                            cc = 0
                            conn.execute("""
                                INSERT INTO consec_good_events
                                (timestamp, machine_id, machine_name, minutes_since)
                                VALUES (?,?,?,?)
                            """, (now_ts, mid, m["name"],
                                  str(mins) if mins else "First"))
                            conn.commit()
                            log.info(f"STREAK {m['name']:20s} {consec_target} in a row!")

                    if new_bad > 0:
                        if cc > 0:
                            log.info(f"RESET  {m['name']:20s} streak at {cc}")
                        cc = 0

                    with lock:
                        st["prev_good"][mid] = good_count
                        st["prev_bad"][mid] = bad_count
                        st["consec_count"][mid] = cc
                        st["last_consec_t"][mid] = lct

                    # -- Snapshot --
                    conn.execute("""
                        INSERT INTO machine_snapshots
                        (timestamp, machine_id, machine_name, op, grp,
                         state_id, state_label, oee_category, alarm_code,
                         good_part_count, bad_part_count,
                         support_call, support_short,
                         down_start, down_minutes,
                         tool0, tool1, tool2, tool3, tool4, tool5, tool6)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        now_ts, mid, m["name"], m["op"], m.get("op_name", ""),
                        sid, state_label, oee_cat, alarm_code,
                        good_count, bad_count,
                        support_call, sup_short,
                        ds, down_min,
                        tools[0], tools[1], tools[2], tools[3],
                        tools[4], tools[5], tools[6]
                    ))

                conn.commit()
                log.info(f"T2 {now.strftime('%H:%M:%S')} - {ip} - {len(group_machines)} machines")

        except Exception as e:
            log.error(f"Tier2 error: {e}")

        elapsed = time.time() - t_start
        time.sleep(max(0, tier2_interval - elapsed))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    area = cfg["area"]
    machines = cfg["machines"]
    cfg_local = cfg.get("local", {})
    scan = cfg_local.get("scan_intervals", {})
    timers = cfg_local.get("down_timers", {})

    # Merge scan_intervals into cfg_local for easy access
    cfg_local["tier1_sec"] = scan.get("tier1_sec", 1)
    cfg_local["tier2_sec"] = scan.get("tier2_sec", 15)
    cfg_local["down_confirm_sec"] = timers.get("down_confirm_sec", 20)
    cfg_local["recover_confirm_sec"] = timers.get("recover_confirm_sec", 60)

    log.info("=" * 60)
    log.info(f"MIS Collector - {area['code']} {area['name']}")
    log.info(f"  Machines     : {len(machines)}")
    log.info(f"  Tier 1       : StateID every {cfg_local['tier1_sec']}s")
    log.info(f"  Tier 1a      : AlarmCode on fault only")
    log.info(f"  Tier 2       : Snapshot every {cfg_local['tier2_sec']}s")
    log.info(f"  Down confirm : {cfg_local['down_confirm_sec']}s")
    log.info(f"  Recover conf : {cfg_local['recover_confirm_sec']}s")

    # Group machines by IP
    ip_groups = group_by_ip(machines)
    for (ip, slot, prog), group in ip_groups.items():
        log.info(f"  PLC {ip} slot {slot} : {len(group)} machines")

    log.info("=" * 60)

    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    init_db(conn)

    st = init_state(machines)

    # Tier 1 in background thread
    t1 = threading.Thread(
        target=tier1_loop,
        args=(conn, machines, ip_groups, st, cfg_local),
        daemon=True,
        name="Tier1"
    )
    t1.start()
    log.info("Tier 1 thread started.")

    # Tier 2 in main thread
    log.info("Tier 2 starting.")
    tier2_loop(conn, machines, ip_groups, st, cfg_local, cfg)


if __name__ == "__main__":
    main()
