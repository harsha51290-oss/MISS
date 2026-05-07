"""
MIS Collector - collector.py
Standard MISS PLC data collector with PT-PM&C sticky state logic.

Polling Tiers:
  Tier 1   - every 1 sec    - StateID only
  Tier 1a  - on fault       - AlarmCode (StateID = 3 or 4 only)
  Tier 2   - every 15 sec   - GoodPartCount, BadPartCount,
                               SupportCall, HMI_SupportReqPB,
                               No_Of_Tools, ToolCount[0..6]

PT-PM&C Sticky State Logic:
  ptpmc_category is stored alongside raw state_id.
  Once a machine enters alarm, it stays alarm until a clean
  PT-PM&C category state is achieved (running, blocked, starved,
  manual, dressing). Sub-states like door open and e-stop do NOT
  break the latch when already in alarm.

  Categories:
    running   - States 1, 15
    dressing  - State 14 (running equivalent, different shade of green)
    blocked   - State 7
    starved   - State 8
    manual    - States 2, 10, 13
    alarm     - States 3, 4, 5, 6, 12
    other     - No valid status
    no_comm   - Cannot communicate with PLC
"""

import json
import sqlite3
import time
import logging
import threading
import os
from datetime import datetime, timedelta
from pylogix import PLC

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

# PT-PM&C category mapping for clean latch-breaker states
PTPMC_CLEAN_STATES = {
    1: "running", 2: "manual", 3: "alarm", 4: "alarm",
    7: "blocked", 8: "starved", 10: "manual",
    13: "manual", 14: "dressing", 15: "running",
}

# States that are sub-states within alarm (stay alarm if already alarm)
PTPMC_ALARM_SUBSTATES = {5, 6, 9, 11, 12}


def resolve_ptpmc_category(state_id, current_latch):
    if state_id == 0:
        return "no_comm"
    # Clean latch breakers always set the category
    if state_id in PTPMC_CLEAN_STATES:
        return PTPMC_CLEAN_STATES[state_id]
    # Alarm sub-states: preserve current latch if already alarm, else set alarm
    if state_id in PTPMC_ALARM_SUBSTATES:
        return "alarm" if current_latch in ("alarm", "other", "no_comm") else current_latch
    return current_latch or "other"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def group_by_ip(machines):
    groups = {}
    for m in machines:
        key = (m["ip"], m.get("slot", 0), m.get("program_prefix", "Program:Illuminate"))
        if key not in groups:
            groups[key] = []
        groups[key].append(m)
    return groups


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
            ptpmc_category   TEXT DEFAULT 'other',
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
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT NOT NULL,
            machine_id     TEXT NOT NULL,
            machine_name   TEXT NOT NULL,
            op             TEXT,
            state_id       INTEGER,
            state_label    TEXT,
            oee_category   TEXT,
            ptpmc_category TEXT DEFAULT 'other',
            alarm_code     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS downtime_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            down_start      TEXT NOT NULL,
            down_end        TEXT,
            machine_id      TEXT NOT NULL,
            machine_name    TEXT NOT NULL,
            op              TEXT,
            duration_min    REAL,
            alarm_code      INTEGER DEFAULT 0,
            ptpmc_category  TEXT DEFAULT 'alarm'
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
            day_id       TEXT,
            reason       TEXT DEFAULT 'tool_life_utilized',
            notes        TEXT DEFAULT ''
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
        CREATE INDEX IF NOT EXISTS idx_snap_mid_ts ON machine_snapshots (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_state_mid_ts ON state_events (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_down_mid ON downtime_events (machine_id, down_start);
        CREATE INDEX IF NOT EXISTS idx_tool_mid_ts ON tool_change_events (machine_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_day ON tool_change_events (day_id, machine_id);
        CREATE INDEX IF NOT EXISTS idx_demand_day ON demand_targets (day_id);
    """)

    # Migrate: add new columns if they don't exist
    migrations = [
        ("machine_snapshots", "ptpmc_category", "TEXT DEFAULT 'other'"),
        ("state_events",      "ptpmc_category", "TEXT DEFAULT 'other'"),
        ("downtime_events",   "ptpmc_category", "TEXT DEFAULT 'alarm'"),
        ("tool_change_events","reason",          "TEXT DEFAULT 'tool_life_utilized'"),
        ("tool_change_events","notes",           "TEXT DEFAULT ''"),
    ]
    for table, col, coldef in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
            conn.commit()
            log.info(f"Migration: added {col} to {table}")
        except Exception:
            pass  # Already exists

    conn.commit()
    log.info("Database initialized.")


lock = threading.Lock()


def init_state(machines):
    state = {
        "current_state": {}, "current_alarm": {}, "current_ptpmc": {},
        "prev_support": {}, "support_start": {},
        "prev_good": {}, "prev_bad": {},
        "consec_count": {}, "last_consec_t": {},
        "prev_tool": {},
        "down_start": {}, "down_pending_t": {}, "recover_start_t": {},
    }
    for m in machines:
        mid = m["id"]
        state["current_state"][mid] = 0
        state["current_alarm"][mid] = 0
        state["current_ptpmc"][mid] = "other"
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


def process_down_timer(conn, mid, m, sid, now, now_ts, st, cfg_local):
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
                if (now - rst).total_seconds() >= recover_confirm:
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
                if (now - dpt).total_seconds() >= down_confirm:
                    alm = st["current_alarm"][mid]
                    ptpmc = st["current_ptpmc"][mid]
                    conn.execute("""
                        INSERT INTO downtime_events
                        (down_start, machine_id, machine_name, op, alarm_code, ptpmc_category)
                        VALUES (?,?,?,?,?,?)
                    """, (now_ts, mid, m["name"], m["op"], alm, ptpmc))
                    conn.commit()
                    log.info(f"DOWN   {m['name']:20s} confirmed down (alarm {alm}, {ptpmc})")
                    with lock:
                        st["down_start"][mid] = now_ts
                        st["down_pending_t"][mid] = None


def tier1_loop(conn, machines, ip_groups, st, cfg_local):
    tier1_interval = cfg_local.get("tier1_sec", 1)
    while True:
        t_start = time.time()
        try:
            now = datetime.now()
            now_ts = now.isoformat()

            for (ip, slot, prog), group_machines in ip_groups.items():
                tags = [f"{prog}.{m['udt_tag']}.StateID" for m in group_machines]
                try:
                    with PLC() as comm:
                        comm.IPAddress = ip
                        comm.ProcessorSlot = slot
                        results = comm.Read(tags)
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
                            prev_ptpmc = st["current_ptpmc"][mid]

                        new_ptpmc = resolve_ptpmc_category(sid, prev_ptpmc)

                        if sid != prev_sid:
                            label = STATE_LABELS.get(sid, f"Unknown ({sid})")
                            oee = OEE_CATEGORY.get(sid, "unknown")
                            alm = st["current_alarm"][mid]
                            conn.execute("""
                                INSERT INTO state_events
                                (timestamp, machine_id, machine_name, op,
                                 state_id, state_label, oee_category, ptpmc_category, alarm_code)
                                VALUES (?,?,?,?,?,?,?,?,?)
                            """, (now_ts, mid, m["name"], m["op"],
                                  sid, label, oee, new_ptpmc, alm))
                            conn.commit()
                            log.info(f"STATE  {m['name']:20s} "
                                     f"{STATE_LABELS.get(prev_sid,'?'):25s} -> {label} "
                                     f"[{prev_ptpmc}->{new_ptpmc}]")

                        with lock:
                            st["current_state"][mid] = sid
                            st["current_ptpmc"][mid] = new_ptpmc

                        process_down_timer(conn, mid, m, sid, now, now_ts, st, cfg_local)

                        if sid in (3, 4):
                            alarm_tags.append(f"{prog}.{m['udt_tag']}.AlarmCode")
                            alarm_mids.append(mid)

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
                    conn.execute("""
                        INSERT INTO plc_status (ip, last_error, error_msg, consecutive_fails)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(ip) DO UPDATE SET
                            last_error=excluded.last_error,
                            error_msg=excluded.error_msg,
                            consecutive_fails=consecutive_fails+1
                    """, (ip, now_ts, str(e)))
                    conn.commit()
                    for m in group_machines:
                        with lock:
                            st["current_ptpmc"][m["id"]] = "no_comm"
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
            h_now = now.hour
            s_ltr = "A" if 6 <= h_now < 14 else "B" if 14 <= h_now < 22 else "C"
            d_id = (now + timedelta(days=1)).strftime("%Y-%m-%d") if h_now >= 22 else now.strftime("%Y-%m-%d")

            for (ip, slot, prog), group_machines in ip_groups.items():
                tags = []
                for m in group_machines:
                    p = f"{prog}.{m['udt_tag']}"
                    tags += [
                        f"{p}.GoodPartCount", f"{p}.BadPartCount",
                        f"{p}.SupportCall", f"{p}.HMI_SupportReqPB",
                        f"{p}.No_Of_Tools",
                        f"{p}.ToolCount[0]", f"{p}.ToolCount[1]",
                        f"{p}.ToolCount[2]", f"{p}.ToolCount[3]",
                        f"{p}.ToolCount[4]", f"{p}.ToolCount[5]",
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
                        ptpmc = st["current_ptpmc"][mid]
                        pg = st["prev_good"][mid]
                        pb = st["prev_bad"][mid]
                        ps = st["prev_support"][mid]
                        pt = st["prev_tool"][mid][:]
                        ds = st["down_start"][mid]

                    state_label = STATE_LABELS.get(sid, f"Unknown ({sid})")
                    oee_cat = OEE_CATEGORY.get(sid, "unknown")
                    sup_info = support_codes.get(str(support_call), {"short": "--"})
                    sup_short = sup_info["short"] if isinstance(sup_info, dict) else sup_info
                    down_min = round((now - datetime.fromisoformat(ds)).total_seconds() / 60, 1) if ds else None

                    # Support call change
                    if support_call != ps:
                        if ps > 0 and st["support_start"][mid]:
                            dur = round((now - datetime.fromisoformat(st["support_start"][mid])).total_seconds() / 60, 1)
                            conn.execute("UPDATE support_call_events SET timestamp_end=?, duration_min=? WHERE machine_id=? AND timestamp_end IS NULL", (now_ts, dur, mid))
                            conn.commit()
                        if support_call > 0:
                            conn.execute("INSERT INTO support_call_events (timestamp_start, machine_id, machine_name, support_call, support_short) VALUES (?,?,?,?,?)", (now_ts, mid, m["name"], support_call, sup_short))
                            conn.commit()
                            log.info(f"TRADE  {m['name']:20s} {sup_short}")
                        with lock:
                            st["prev_support"][mid] = support_call
                            st["support_start"][mid] = now_ts if support_call > 0 else None

                    # Tool change detection
                    for slot_num in range(no_of_tools):
                        prev_val = pt[slot_num]
                        curr_val = tools[slot_num]
                        if prev_val > tool_threshold and curr_val < (prev_val - tool_threshold):
                            conn.execute("""
                                INSERT INTO tool_change_events
                                (timestamp, machine_id, machine_name, op,
                                 tool_slot, parts_run, shift_letter, day_id, reason, notes)
                                VALUES (?,?,?,?,?,?,?,?,?,?)
                            """, (now_ts, mid, m["name"], m["op"],
                                  slot_num, prev_val, s_ltr, d_id, "tool_life_utilized", ""))
                            conn.commit()
                            log.info(f"TOOL   {m['name']:20s} slot {slot_num} changed - {prev_val:,} parts")
                    with lock:
                        st["prev_tool"][mid] = tools[:]

                    # Consecutive good parts
                    cc = st["consec_count"][mid]
                    lct = st["last_consec_t"][mid]
                    new_good = good_count - pg
                    new_bad = bad_count - pb
                    if new_good > 0:
                        cc += new_good
                        if cc >= consec_target:
                            mins = round((now - datetime.fromisoformat(lct)).total_seconds() / 60, 1) if lct else None
                            lct = now_ts
                            cc = 0
                            conn.execute("INSERT INTO consec_good_events (timestamp, machine_id, machine_name, minutes_since) VALUES (?,?,?,?)", (now_ts, mid, m["name"], str(mins) if mins else "First"))
                            conn.commit()
                    if new_bad > 0:
                        cc = 0
                    with lock:
                        st["prev_good"][mid] = good_count
                        st["prev_bad"][mid] = bad_count
                        st["consec_count"][mid] = cc
                        st["last_consec_t"][mid] = lct

                    # Snapshot
                    conn.execute("""
                        INSERT INTO machine_snapshots
                        (timestamp, machine_id, machine_name, op, grp,
                         state_id, state_label, oee_category, ptpmc_category,
                         alarm_code, good_part_count, bad_part_count,
                         support_call, support_short, down_start, down_minutes,
                         tool0, tool1, tool2, tool3, tool4, tool5, tool6)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (now_ts, mid, m["name"], m["op"], m.get("op_name", ""),
                          sid, state_label, oee_cat, ptpmc, alarm_code,
                          good_count, bad_count, support_call, sup_short,
                          ds, down_min,
                          tools[0], tools[1], tools[2], tools[3], tools[4], tools[5], tools[6]))

                conn.commit()
                log.info(f"T2 {now.strftime('%H:%M:%S')} - {ip} - {len(group_machines)} machines")

        except Exception as e:
            log.error(f"Tier2 error: {e}")

        elapsed = time.time() - t_start
        time.sleep(max(0, tier2_interval - elapsed))


def main():
    cfg = load_config()
    area = cfg["area"]
    machines = cfg["machines"]
    cfg_local = cfg.get("local", {})
    scan = cfg_local.get("scan_intervals", {})
    timers = cfg_local.get("down_timers", {})
    cfg_local["tier1_sec"] = scan.get("tier1_sec", 1)
    cfg_local["tier2_sec"] = scan.get("tier2_sec", 15)
    cfg_local["down_confirm_sec"] = timers.get("down_confirm_sec", 20)
    cfg_local["recover_confirm_sec"] = timers.get("recover_confirm_sec", 60)

    log.info("=" * 60)
    log.info(f"MIS Collector - {area['code']} {area['name']}")
    log.info(f"  Machines     : {len(machines)}")
    log.info(f"  PT-PM&C      : Sticky state logic ENABLED")
    log.info("=" * 60)

    ip_groups = group_by_ip(machines)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    init_db(conn)
    st = init_state(machines)

    t1 = threading.Thread(target=tier1_loop, args=(conn, machines, ip_groups, st, cfg_local), daemon=True, name="Tier1")
    t1.start()
    log.info("Tier 1 thread started.")
    tier2_loop(conn, machines, ip_groups, st, cfg_local, cfg)


if __name__ == "__main__":
    main()
