"""
MIS REST API - api.py
Standard API for all MISS areas.
Reads config.json + mis.db (collector schema).
Runs under PM2 via uvicorn.

Endpoints:
  GET  /dashboard              - serves dashboard.html
  GET  /config                 - config management page
  GET  /api/health             - system health + PLC status
  GET  /api/machines           - all machine data for current/selected shift
  GET  /api/op_totals          - operation-level totals
  GET  /api/day_totals         - daily totals across all shifts
  GET  /api/hourly_trend       - hourly production per machine
  GET  /api/active_trades      - active support/trade calls
  GET  /api/tool_current       - current tool counter values
  GET  /api/plc_status         - PLC connection status (for banner)
  GET  /api/data               - simple endpoint for MIS central
  GET  /api/config             - current config.json
  POST /api/config             - update config.json
  POST /api/demand             - push daily targets from MIS
  GET  /api/demand             - get current demand targets
"""

import json
import sqlite3
import os
import time
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

AREA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AREA_DIR, "config.json")
DB_FILE = os.path.join(AREA_DIR, "mis.db")
DASHBOARD_FILE = os.path.join(AREA_DIR, "dashboard.html")
CONFIG_HTML_FILE = os.path.join(AREA_DIR, "config.html")
FLOW_EDITOR_FILE = os.path.join(AREA_DIR, "flow.html")
BACKUP_DIR = os.path.join(AREA_DIR, "backups", "config")
os.makedirs(BACKUP_DIR, exist_ok=True)

def get_password():
    try:
        return load_config().get("admin", {}).get("password", "")
    except Exception:
        return ""
app = FastAPI(title="MIS Area API")

RUNNING_STATES = {1, 15}


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# -- Shift logic --

def get_shift_info(cfg, now=None):
    if now is None:
        now = datetime.now()
    shifts = cfg["shifts"]
    for letter, times in shifts.items():
        sh, sm = map(int, times["start"].split(":"))
        eh, em = map(int, times["end"].split(":"))
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= start:
            if now.hour >= sh:
                end += timedelta(days=1)
            else:
                start -= timedelta(days=1)
        if start <= now < end:
            day_id = end.strftime("%Y-%m-%d") if letter == "C" else now.strftime("%Y-%m-%d")
            return letter, day_id, start, end
    return "?", now.strftime("%Y-%m-%d"), now, now

def resolve_shift(cfg, shift_param):
    now = datetime.now()
    cur_letter, cur_day, cur_start, cur_end = get_shift_info(cfg, now)
    if not shift_param or shift_param == "current":
        return cur_letter, cur_day, cur_start, cur_end
    if shift_param == "prev1":
        return get_shift_info(cfg, cur_start - timedelta(minutes=1))
    if shift_param == "prev2":
        _, _, p1_start, _ = get_shift_info(cfg, cur_start - timedelta(minutes=1))
        return get_shift_info(cfg, p1_start - timedelta(minutes=1))
    if shift_param.startswith("manual_"):
        parts = shift_param.split("_")
        if len(parts) >= 3:
            date_str, shift_letter = parts[1], parts[2]
            if shift_letter in cfg["shifts"]:
                times = cfg["shifts"][shift_letter]
                sh, sm = map(int, times["start"].split(":"))
                eh, em = map(int, times["end"].split(":"))
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                start = dt.replace(hour=sh, minute=sm, second=0)
                end = dt.replace(hour=eh, minute=em, second=0)
                if end <= start:
                    end += timedelta(days=1)
                day_id = end.strftime("%Y-%m-%d") if shift_letter == "C" else date_str
                return shift_letter, day_id, start, end
    return cur_letter, cur_day, cur_start, cur_end

def shift_time_range(cfg, shift_letter, day_id):
    """Get start/end datetime for a specific shift and day."""
    times = cfg["shifts"].get(shift_letter, {})
    if not times:
        return None, None
    sh, sm = map(int, times["start"].split(":"))
    eh, em = map(int, times["end"].split(":"))
    dt = datetime.strptime(day_id, "%Y-%m-%d")
    if shift_letter == "C":
        start = (dt - timedelta(days=1)).replace(hour=sh, minute=sm, second=0)
        end = dt.replace(hour=eh, minute=em, second=0)
    else:
        start = dt.replace(hour=sh, minute=sm, second=0)
        end = dt.replace(hour=eh, minute=em, second=0)
    return start, end

def parse_break_windows(cfg, shift_start, shift_end):
    """Parse breaks list [{start, end}] into (datetime, datetime) tuples for this shift."""
    breaks = cfg.get("breaks", [])
    if not breaks or not isinstance(breaks, list):
        return []
    windows = []
    for b in breaks:
        try:
            bsh, bsm = map(int, b["start"].split(":"))
            beh, bem = map(int, b["end"].split(":"))
            bs = shift_start.replace(hour=bsh, minute=bsm, second=0, microsecond=0)
            be = shift_start.replace(hour=beh, minute=bem, second=0, microsecond=0)
            if be <= bs:
                be += timedelta(days=1)
            if bs < shift_end and be > shift_start:
                windows.append((max(bs, shift_start), min(be, shift_end)))
        except Exception:
            continue
    return windows


def calc_productive_seconds(shift_start, as_of, break_windows):
    """Productive seconds from shift_start to as_of, minus any elapsed break time."""
    total = max(0, (as_of - shift_start).total_seconds())
    for bs, be in break_windows:
        if be <= shift_start or bs >= as_of:
            continue
        overlap = (min(be, as_of) - max(bs, shift_start)).total_seconds()
        if overlap > 0:
            total -= overlap
    return max(0, total)


def calc_expected_now(cfg, shift_start, shift_end, shift_target):
    """Precise expected count — pauses during exact break windows."""
    if shift_target <= 0:
        return 0
    now = datetime.now()
    as_of = min(now, shift_end)
    if as_of <= shift_start:
        return 0
    bw = parse_break_windows(cfg, shift_start, shift_end)
    productive_now = calc_productive_seconds(shift_start, as_of, bw)
    total_productive = calc_productive_seconds(shift_start, shift_end, bw)
    if total_productive <= 0:
        return 0
    return round(shift_target * (productive_now / total_productive))


def calc_hourly_target(cfg, shift_start, shift_end, shift_target):
    """Hourly target based on precise productive hours in shift."""
    if shift_target <= 0:
        return 0
    bw = parse_break_windows(cfg, shift_start, shift_end)
    total_productive = calc_productive_seconds(shift_start, shift_end, bw)
    productive_hours = total_productive / 3600
    return round(shift_target / productive_hours) if productive_hours > 0 else 0


# -- Dashboard --
@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    if os.path.exists(DASHBOARD_FILE):
        with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


# -- Health + PLC status --
@app.get("/api/health")
async def health():
    cfg = load_config()
    area = cfg["area"]
    checks = {"status": "ok", "area_code": area["code"], "area_name": area["name"],
              "timestamp": datetime.now().isoformat(), "machine_count": len(cfg["machines"])}
    if os.path.exists(DB_FILE):
        checks["db_exists"] = True
        checks["db_age_seconds"] = round(time.time() - os.path.getmtime(DB_FILE))
    else:
        checks["db_exists"] = False
        checks["status"] = "error"
        checks["error"] = "Database not found"
        return JSONResponse(checks)
    try:
        conn = get_db()
        row = conn.execute("SELECT MAX(timestamp) FROM machine_snapshots").fetchone()
        if row and row[0]:
            checks["collector_last_scan"] = row[0]
            last = datetime.fromisoformat(row[0])
            gap = (datetime.now() - last).total_seconds()
            checks["collector_gap_seconds"] = round(gap)
            if gap > 300:
                checks["status"] = "error"
                checks["error"] = f"Collector offline - last scan {round(gap/60)}m ago"
        # PLC status
        plc_rows = conn.execute("SELECT * FROM plc_status").fetchall()
        checks["plc_connections"] = []
        for pr in plc_rows:
            checks["plc_connections"].append({
                "ip": pr["ip"],
                "last_success": pr["last_success"],
                "last_error": pr["last_error"],
                "error_msg": pr["error_msg"],
                "consecutive_fails": pr["consecutive_fails"],
            })
        conn.close()
    except Exception as e:
        checks["db_error"] = str(e)
    bs_file = os.path.join(AREA_DIR, "backup_status.json")
    if os.path.exists(bs_file):
        try:
            with open(bs_file) as f:
                checks["last_backup"] = json.load(f).get("last_backup")
        except Exception:
            pass
    return JSONResponse(checks)


@app.get("/api/plc_status")
async def plc_status():
    """PLC connection status for dashboard banner."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM plc_status").fetchall()
        result = []
        for r in rows:
            result.append({
                "ip": r["ip"],
                "last_success": r["last_success"],
                "last_error": r["last_error"],
                "error_msg": r["error_msg"],
                "consecutive_fails": r["consecutive_fails"],
                "status": "ok" if r["consecutive_fails"] == 0 else "error",
            })
        conn.close()
        return JSONResponse(result)
    except Exception:
        conn.close()
        return JSONResponse([])


# -- Machines --
@app.get("/api/machines")
async def get_machines(shift: str = "current"):
    cfg = load_config()
    shift_letter, day_id, shift_start, shift_end = resolve_shift(cfg, shift)
    s_start, s_end = shift_time_range(cfg, shift_letter, day_id)
    ts_start = s_start.isoformat() if s_start else ""
    ts_end = s_end.isoformat() if s_end else ""

    conn = get_db()

    # Demand targets
    demand_map = {}
    try:
        for dr in conn.execute("SELECT machine_id, shift_target, daily_target, daily_demand FROM demand_targets WHERE day_id=?", (day_id,)).fetchall():
            demand_map[dr["machine_id"]] = {"shift_target": dr["shift_target"], "daily_target": dr["daily_target"], "daily_demand": dr["daily_demand"]}
    except Exception:
        pass

    out = []
    for m in cfg["machines"]:
        mid = m["id"]

        # Latest snapshot for this machine in this shift window
        snap = conn.execute(
            "SELECT * FROM machine_snapshots WHERE machine_id=? AND timestamp>=? AND timestamp<? ORDER BY id DESC LIMIT 1",
            (mid, ts_start, ts_end)
        ).fetchone()

        # First snapshot in shift (for delta calculation)
        first_snap = conn.execute(
            "SELECT good_part_count, bad_part_count FROM machine_snapshots WHERE machine_id=? AND timestamp>=? AND timestamp<? ORDER BY id ASC LIMIT 1",
            (mid, ts_start, ts_end)
        ).fetchone()

        # Calculate shift parts from delta
        good_shift = 0
        bad_shift = 0
        if snap and first_snap:
            good_shift = max(0, (snap["good_part_count"] or 0) - (first_snap["good_part_count"] or 0))
            bad_shift = max(0, (snap["bad_part_count"] or 0) - (first_snap["bad_part_count"] or 0))

        # Demand target or config default
        dm = demand_map.get(mid)
        shift_target = dm["shift_target"] if dm else m["shift_target"]

        # OEE from state time in shift
        # Count productive vs non-productive snapshots
        state_counts = conn.execute(
            "SELECT oee_category, COUNT(*) as cnt FROM machine_snapshots "
            "WHERE machine_id=? AND timestamp>=? AND timestamp<? GROUP BY oee_category",
            (mid, ts_start, ts_end)
        ).fetchall()
        total_snaps = sum(r["cnt"] for r in state_counts)
        productive_snaps = sum(r["cnt"] for r in state_counts if r["oee_category"] == "productive")
        availability = (productive_snaps / total_snaps) if total_snaps > 0 else None

        # Performance
        rated_ct = m.get("rated_ct", 0)
        run_seconds = productive_snaps * 15  # each snapshot ~ 15 sec
        theoretical = (run_seconds / rated_ct) if rated_ct > 0 and run_seconds > 0 else 0
        performance = (good_shift / theoretical) if theoretical > 0 else None
        quality = (good_shift / (good_shift + bad_shift)) if (good_shift + bad_shift) > 0 else None
        oee_val = (availability * performance * quality * 100) if all(v is not None for v in [availability, performance, quality]) else None

        # Avg CT
        avg_ct = None
        if good_shift > 0 and run_seconds > 0:
            avg_ct = run_seconds / good_shift

        expected = calc_expected_now(cfg, shift_start, shift_end, shift_target) if shift == "current" else good_shift

        out.append({
            "machine_id": mid,
            "machine_name": m["name"],
            "op": m["op"],
            "op_name": m.get("op_name", ""),
            "type": m.get("op_name", ""),
            "state_id": snap["state_id"] if snap else 0,
            "alarm_code": snap["alarm_code"] if snap else 0,
            "support_call": snap["support_call"] if snap else 0,
            "good_shift": good_shift,
            "bad_shift": bad_shift,
            "expected_now": expected,
            "shift_target": shift_target,
            "config_target": m["shift_target"],
            "daily_target": dm["daily_target"] if dm else m["shift_target"] * 3,
            "hourly_target": calc_hourly_target(cfg, shift_start, shift_end, shift_target),
            "rated_ct": rated_ct,
            "avg_ct": avg_ct,
            "oee": oee_val,
            "availability": availability,
            "performance": performance,
            "quality": quality,
            "down_minutes": snap["down_minutes"] if snap and snap["down_minutes"] else 0,
            "series_output": m.get("series_output", False),
        })
    conn.close()
    return JSONResponse(out)


# -- OP Totals --
@app.get("/api/op_totals")
async def get_op_totals(shift: str = "current"):
    cfg = load_config()
    machines_data = (await get_machines(shift)).body
    machines = json.loads(machines_data)

    op_groups = {}
    for m in machines:
        key = m["op_name"]
        if key not in op_groups:
            op_groups[key] = {"good": 0, "bad": 0, "expected": 0, "shift_target": 0}
        if m.get("series_output"):
            op_groups[key]["good"] = m["good_shift"]
            op_groups[key]["bad"] = m["bad_shift"]
        else:
            op_groups[key]["good"] += m["good_shift"]
            op_groups[key]["bad"] += m["bad_shift"]
            op_groups[key]["expected"] += m["expected_now"]
            op_groups[key]["shift_target"] += m["shift_target"]

    result = {}
    for op_name, data in op_groups.items():
        data["pct"] = round(data["good"] / data["expected"] * 100) if data["expected"] > 0 else 0
        result[op_name] = data

    return JSONResponse(result)


# -- Day Totals --
@app.get("/api/day_totals")
async def get_day_totals():
    cfg = load_config()
    _, day_id, _, _ = get_shift_info(cfg)
    conn = get_db()

    # Get all shifts for this day
    shifts_done = []
    for s_letter in ["C", "A", "B"]:
        s_start, s_end = shift_time_range(cfg, s_letter, day_id)
        if s_start and s_end:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM machine_snapshots WHERE timestamp>=? AND timestamp<?",
                (s_start.isoformat(), s_end.isoformat())
            ).fetchone()
            if row and row["cnt"] > 0:
                shifts_done.append(s_letter)

    # Calculate totals per op_name across all shifts
    totals = {}
    for m in cfg["machines"]:
        op = m.get("op_name", "")
        if op not in totals:
            totals[op] = {"good": 0, "bad": 0}

        for s_letter in shifts_done:
            s_start, s_end = shift_time_range(cfg, s_letter, day_id)
            if not s_start:
                continue
            first = conn.execute(
                "SELECT good_part_count, bad_part_count FROM machine_snapshots "
                "WHERE machine_id=? AND timestamp>=? AND timestamp<? ORDER BY id ASC LIMIT 1",
                (m["id"], s_start.isoformat(), s_end.isoformat())
            ).fetchone()
            last = conn.execute(
                "SELECT good_part_count, bad_part_count FROM machine_snapshots "
                "WHERE machine_id=? AND timestamp>=? AND timestamp<? ORDER BY id DESC LIMIT 1",
                (m["id"], s_start.isoformat(), s_end.isoformat())
            ).fetchone()
            if first and last:
                g = max(0, (last["good_part_count"] or 0) - (first["good_part_count"] or 0))
                b = max(0, (last["bad_part_count"] or 0) - (first["bad_part_count"] or 0))
                if m.get("series_output"):
                    totals[op]["good"] = g  # series = last machine only
                    totals[op]["bad"] = b
                else:
                    totals[op]["good"] += g
                    totals[op]["bad"] += b

    conn.close()
    return JSONResponse({"day_id": day_id, "shifts": shifts_done, "totals": totals})


# -- Hourly Trend --
@app.get("/api/hourly_trend")
async def get_hourly_trend(shift: str = "current"):
    cfg = load_config()
    shift_letter, day_id, shift_start, shift_end = resolve_shift(cfg, shift)
    s_start, s_end = shift_time_range(cfg, shift_letter, day_id)
    if not s_start:
        return JSONResponse([])

    conn = get_db()
    result = []

    for m in cfg["machines"]:
        mid = m["id"]
        # Get all snapshots for this machine in this shift
        snaps = conn.execute(
            "SELECT timestamp, good_part_count FROM machine_snapshots "
            "WHERE machine_id=? AND timestamp>=? AND timestamp<? ORDER BY id",
            (mid, s_start.isoformat(), s_end.isoformat())
        ).fetchall()

        if not snaps:
            continue

        # Bucket into hours
        first_good = snaps[0]["good_part_count"] or 0
        for hour_num in range(1, 9):
            hr_start = s_start + timedelta(hours=hour_num - 1)
            hr_end = s_start + timedelta(hours=hour_num)
            hr_snaps = [s for s in snaps if hr_start.isoformat() <= s["timestamp"] < hr_end.isoformat()]
            if hr_snaps:
                hr_first = hr_snaps[0]["good_part_count"] or 0
                hr_last = hr_snaps[-1]["good_part_count"] or 0
                parts = max(0, hr_last - hr_first)
                result.append({
                    "machine_id": mid,
                    "machine_name": m["name"],
                    "op": m["op"],
                    "hour": hour_num,
                    "parts": parts,
                })

    conn.close()
    return JSONResponse(result)


# -- Active Trades --
@app.get("/api/active_trades")
async def get_active_trades():
    cfg = load_config()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM support_call_events WHERE timestamp_end IS NULL ORDER BY timestamp_start"
    ).fetchall()
    now = datetime.now()
    trades = []
    for r in rows:
        start = datetime.fromisoformat(r["timestamp_start"])
        trades.append({
            "machine_id": r["machine_id"],
            "machine_name": r["machine_name"],
            "support_code": r["support_call"],
            "support_short": r["support_short"],
            "called_at": r["timestamp_start"],
            "duration_min": round((now - start).total_seconds() / 60),
        })
    conn.close()
    return JSONResponse(trades)


@app.get("/api/trades_history")
async def get_trades_history(shift: str = "current", limit: int = 200):
    """All trade calls (open and closed) for a given shift, newest first."""
    cfg = load_config()
    shift_letter, day_id, shift_start, shift_end = resolve_shift(cfg, shift)
    s_start, s_end = shift_time_range(cfg, shift_letter, day_id)
    conn = get_db()
    now = datetime.now()

    if s_start and s_end:
        rows = conn.execute("""
            SELECT * FROM support_call_events
            WHERE timestamp_start >= ? AND timestamp_start < ?
            ORDER BY timestamp_start DESC LIMIT ?
        """, (s_start.isoformat(), s_end.isoformat(), limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM support_call_events
            ORDER BY timestamp_start DESC LIMIT ?
        """, (limit,)).fetchall()

    result = []
    for r in rows:
        start = datetime.fromisoformat(r["timestamp_start"])
        end_ts = r["timestamp_end"]
        if end_ts:
            dur = r["duration_min"] or round((datetime.fromisoformat(end_ts) - start).total_seconds() / 60, 1)
            status = "Closed"
        else:
            dur = round((now - start).total_seconds() / 60, 1)
            status = "Active"
        result.append({
            "id": r["id"],
            "machine_id": r["machine_id"],
            "machine_name": r["machine_name"],
            "support_code": r["support_call"],
            "support_short": r["support_short"],
            "called_at": r["timestamp_start"],
            "closed_at": end_ts,
            "duration_min": dur,
            "status": status,
        })
    conn.close()
    return JSONResponse(result)


# -- Tool Current --
@app.get("/api/tool_current")
async def get_tool_current():
    cfg = load_config()
    conn = get_db()
    result = []
    for m in cfg["machines"]:
        mid = m["id"]
        snap = conn.execute(
            "SELECT tool0,tool1,tool2,tool3,tool4,tool5,tool6 FROM machine_snapshots "
            "WHERE machine_id=? ORDER BY id DESC LIMIT 1", (mid,)
        ).fetchone()

        # Get avg life from tool change history
        avg_rows = conn.execute(
            "SELECT tool_slot, AVG(parts_run) as avg_life FROM tool_change_events "
            "WHERE machine_id=? GROUP BY tool_slot", (mid,)
        ).fetchall()
        avg_map = {r["tool_slot"]: round(r["avg_life"]) for r in avg_rows if r["avg_life"]}

        # Get tool config (description, limit, warning) from cfg.tools
        tool_cfg = cfg.get("tools", {}).get(mid, {})

        tools = []
        for slot in range(7):
            count = (snap[slot] or 0) if snap else 0
            avg_life = avg_map.get(slot, 0)
            pct_used = round(count / avg_life * 100) if avg_life > 0 else None
            slot_cfg = tool_cfg.get(f"T{slot}", {})
            limit = slot_cfg.get("limit", 0)
            warning = slot_cfg.get("warning", 0)
            description = slot_cfg.get("description", "")
            # Calculate pct from limit if available, fallback to avg_life
            if limit > 0:
                pct_used = round(count / limit * 100)
            tools.append({
                "slot": slot,
                "count": count,
                "avg_life": avg_life,
                "pct_used": pct_used,
                "limit": limit,
                "warning": warning,
                "description": description,
            })

        result.append({
            "machine_id": mid, "machine_name": m["name"],
            "op": m["op"], "op_name": m.get("op_name", ""),
            "tools": tools,
        })
    conn.close()
    return JSONResponse(result)


# -- Simple data for MIS central --
@app.get("/api/data")
async def get_data():
    cfg = load_config()
    machines_data = (await get_machines("current")).body
    machines = json.loads(machines_data)
    shift, day_id, shift_start, shift_end = get_shift_info(cfg)
    return {
        "area": cfg["area"], "shift": shift, "day_id": day_id,
        "shift_start": shift_start.isoformat(), "shift_end": shift_end.isoformat(),
        "timestamp": datetime.now().isoformat(),
        "machines": machines,
    }


# -- Config --
@app.get("/api/config")
async def get_config():
    return load_config()

AUDIT_LOG_FILE = os.path.join(AREA_DIR, "backups", "audit_log.json")
os.makedirs(os.path.join(AREA_DIR, "backups"), exist_ok=True)


def detect_changes(old_cfg, new_cfg, path=""):
    """Recursively detect changes between two config dicts. Returns list of change strings."""
    changes = []
    all_keys = set(list(old_cfg.keys()) + list(new_cfg.keys())) if isinstance(old_cfg, dict) else []
    for key in all_keys:
        full_path = f"{path}.{key}" if path else key
        old_val = old_cfg.get(key)
        new_val = new_cfg.get(key)
        if old_val == new_val:
            continue
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            changes.extend(detect_changes(old_val, new_val, full_path))
        elif isinstance(old_val, list) and isinstance(new_val, list):
            if old_val != new_val:
                changes.append(f"{full_path}: list changed ({len(old_val)} -> {len(new_val)} items)")
        elif old_val is None:
            changes.append(f"{full_path}: added = {new_val}")
        elif new_val is None:
            changes.append(f"{full_path}: removed (was {old_val})")
        else:
            changes.append(f"{full_path}: {old_val} -> {new_val}")
    return changes


def load_audit_log():
    if os.path.exists(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_audit_log(log):
    with open(AUDIT_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


@app.post("/api/config")
async def update_config(request: Request):
    body = await request.json()
    if body.get("password") != get_password():
        raise HTTPException(403, "Invalid password")
    new_config = body.get("config")
    if not new_config:
        raise HTTPException(400, "Missing config data")
    changed_by = body.get("changed_by", "admin")
    comment = body.get("comment", "")
    tab = body.get("tab", "unknown")

    # Load current config for diff
    old_config = load_config()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ts_iso = datetime.now().isoformat()

    # Detect changes
    changes = detect_changes(old_config, new_config)

    # Save backup of old config
    backup_filename = f"config_{ts}.json"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    with open(backup_path, "w") as bf:
        json.dump(old_config, bf, indent=2)

    # Save new config
    with open(CONFIG_FILE, "w") as f:
        json.dump(new_config, f, indent=2)

    # Append to audit log
    log = load_audit_log()
    log.append({
        "timestamp": ts_iso,
        "changed_by": changed_by,
        "tab": tab,
        "comment": comment,
        "changes": changes,
        "backup_file": backup_filename,
    })
    save_audit_log(log)

    return {"status": "ok", "changes_detected": len(changes), "backup": backup_filename}


@app.get("/api/audit_log")
async def get_audit_log():
    """Return full audit log history."""
    return JSONResponse(load_audit_log())


@app.post("/api/config/restore")
async def restore_config(request: Request):
    """Restore a previous config from backup file."""
    body = await request.json()
    if body.get("password") != get_password():
        raise HTTPException(403, "Invalid password")
    backup_file = body.get("backup_file")
    changed_by = body.get("changed_by", "admin")
    if not backup_file:
        raise HTTPException(400, "Missing backup_file")
    backup_path = os.path.join(BACKUP_DIR, backup_file)
    if not os.path.exists(backup_path):
        raise HTTPException(404, f"Backup file not found: {backup_file}")

    # Load backup
    with open(backup_path, "r") as f:
        restored_config = json.load(f)

    # Save current as backup before restoring
    old_config = load_config()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ts_iso = datetime.now().isoformat()
    pre_restore_backup = f"config_{ts}_pre_restore.json"
    with open(os.path.join(BACKUP_DIR, pre_restore_backup), "w") as bf:
        json.dump(old_config, bf, indent=2)

    # Write restored config
    with open(CONFIG_FILE, "w") as f:
        json.dump(restored_config, f, indent=2)

    # Log the restore
    log = load_audit_log()
    log.append({
        "timestamp": ts_iso,
        "changed_by": changed_by,
        "tab": "restore",
        "comment": f"Restored from backup: {backup_file}",
        "changes": [f"Full config restored from {backup_file}"],
        "backup_file": pre_restore_backup,
    })
    save_audit_log(log)

    return {"status": "ok", "restored_from": backup_file}


# -- Demand --
@app.post("/api/demand")
async def push_demand(request: Request):
    body = await request.json()
    if body.get("password") != get_password():
        raise HTTPException(403, "Invalid password")
    day_id = body.get("day_id")
    if not day_id:
        raise HTTPException(400, "Missing day_id")
    daily_demand = body.get("daily_demand", 0)
    machines = body.get("machines", {})
    if not machines:
        raise HTTPException(400, "Missing machines dict")
    conn = get_db()
    count = 0
    for mid, targets in machines.items():
        st = targets.get("shift_target", 0)
        dt = targets.get("daily_target", st * 3)
        conn.execute("""
            INSERT INTO demand_targets (day_id, machine_id, shift_target, daily_target, daily_demand)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day_id, machine_id) DO UPDATE SET
                shift_target=excluded.shift_target, daily_target=excluded.daily_target,
                daily_demand=excluded.daily_demand, pushed_at=datetime('now','localtime')
        """, (day_id, mid, st, dt, daily_demand))
        count += 1
    conn.commit()
    conn.close()
    return {"status": "ok", "day_id": day_id, "machines_updated": count}

@app.get("/api/demand")
async def get_demand(day_id: str = None):
    cfg = load_config()
    if not day_id:
        _, day_id, _, _ = get_shift_info(cfg)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM demand_targets WHERE day_id=? ORDER BY machine_id", (day_id,)
    ).fetchall()
    conn.close()
    return {
        "day_id": day_id,
        "pushed_at": rows[0]["pushed_at"] if rows else None,
        "daily_demand": rows[0]["daily_demand"] if rows else 0,
        "machines": {r["machine_id"]: {"shift_target": r["shift_target"], "daily_target": r["daily_target"]} for r in rows}
    }


# -- Config page --
@app.get("/config", response_class=HTMLResponse)
async def config_page():
    if os.path.exists(CONFIG_HTML_FILE):
        with open(CONFIG_HTML_FILE, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>config.html not found</h1>", status_code=404)


# -- Admin auth (config page login) --
@app.post("/api/auth")
async def check_auth(request: Request):
    """Validate config page credentials against config.json admin section."""
    body = await request.json()
    cfg = load_config()
    admin = cfg.get("admin", {})
    if body.get("username") == admin.get("username") and body.get("password") == admin.get("password"):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False}, status_code=401)




# ---------------------------------------------------------------------------
# Buffer Flow
# ---------------------------------------------------------------------------

BUFFER_LOG_TABLE = """
    CREATE TABLE IF NOT EXISTS buffer_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   TEXT NOT NULL,
        buffer_id   TEXT NOT NULL,
        count       REAL NOT NULL,
        source      TEXT DEFAULT 'manual',
        shift       TEXT,
        day_id      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_buf_id_ts ON buffer_events (buffer_id, timestamp);
"""

def ensure_buffer_table():
    conn = get_db()
    conn.executescript(BUFFER_LOG_TABLE)
    conn.commit()
    conn.close()

ensure_buffer_table()


@app.get("/api/buffer_state")
async def get_buffer_state():
    """
    Returns current buffer counts for all buffers in process_flow.
    Count = last manual entry + net auto adjustments since then.
    Auto adjustments: upstream op output increments, downstream op input decrements.
    """
    cfg = load_config()
    flow = cfg.get("process_flow", {})
    nodes = {n["id"]: n for n in flow.get("nodes", [])}
    connections = flow.get("connections", [])
    conn = get_db()
    now = datetime.now()
    shift_letter, day_id, shift_start, shift_end = get_shift_info(cfg, now)

    # Build upstream/downstream machine maps for each buffer
    # upstream_of[buffer_id] = list of operation node ids feeding INTO buffer
    # downstream_of[buffer_id] = list of operation node ids consuming FROM buffer
    upstream_of = {}
    downstream_of = {}
    for edge in connections:
        src = edge["from"]
        dst = edge["to"]
        if nodes.get(dst, {}).get("type") == "buffer":
            upstream_of.setdefault(dst, []).append(src)
        if nodes.get(src, {}).get("type") == "buffer":
            downstream_of.setdefault(src, []).append(dst)

    result = []
    for node in flow.get("nodes", []):
        if node["type"] != "buffer":
            continue
        bid = node["id"]

        # Get last manual entry
        last_manual = conn.execute("""
            SELECT count, timestamp FROM buffer_events
            WHERE buffer_id=? AND source='manual'
            ORDER BY timestamp DESC LIMIT 1
        """, (bid,)).fetchone()

        base_count = last_manual["count"] if last_manual else 0
        base_ts = last_manual["timestamp"] if last_manual else shift_start.isoformat()

        # Calculate auto adjustments since last manual entry
        # Parts added = sum of parts made by upstream operations since base_ts
        parts_added = 0
        for up_node_id in upstream_of.get(bid, []):
            up_machines = nodes.get(up_node_id, {}).get("machines", [])
            for mid in up_machines:
                # Get parts delta since base_ts
                first = conn.execute("""
                    SELECT good_part_count FROM machine_snapshots
                    WHERE machine_id=? AND timestamp>=? ORDER BY id ASC LIMIT 1
                """, (mid, base_ts)).fetchone()
                last = conn.execute("""
                    SELECT good_part_count FROM machine_snapshots
                    WHERE machine_id=? AND timestamp>=? ORDER BY id DESC LIMIT 1
                """, (mid, base_ts)).fetchone()
                if first and last:
                    delta = max(0, (last["good_part_count"] or 0) - (first["good_part_count"] or 0))
                    # For series output machines, only count the last one in group
                    cfg_m = next((m for m in cfg["machines"] if m["id"] == mid), {})
                    if not cfg_m.get("series_output", False):
                        parts_added += delta
                    else:
                        # Series output — use this as the group total
                        parts_added = delta

        # Parts consumed = total parts (good + bad) made by downstream operations since base_ts
        # Every part taken from buffer is consumed regardless of quality outcome
        parts_consumed = 0
        for dn_node_id in downstream_of.get(bid, []):
            dn_machines = nodes.get(dn_node_id, {}).get("machines", [])
            for mid in dn_machines:
                first = conn.execute("""
                    SELECT good_part_count, bad_part_count FROM machine_snapshots
                    WHERE machine_id=? AND timestamp>=? ORDER BY id ASC LIMIT 1
                """, (mid, base_ts)).fetchone()
                last = conn.execute("""
                    SELECT good_part_count, bad_part_count FROM machine_snapshots
                    WHERE machine_id=? AND timestamp>=? ORDER BY id DESC LIMIT 1
                """, (mid, base_ts)).fetchone()
                if first and last:
                    good_delta = max(0, (last["good_part_count"] or 0) - (first["good_part_count"] or 0))
                    bad_delta = max(0, (last["bad_part_count"] or 0) - (first["bad_part_count"] or 0))
                    delta = good_delta + bad_delta
                    cfg_m = next((m for m in cfg["machines"] if m["id"] == mid), {})
                    if not cfg_m.get("series_output", False):
                        parts_consumed += delta

        current_count = max(0, base_count + parts_added - parts_consumed)

        result.append({
            "buffer_id": bid,
            "label": node.get("label", bid),
            "note": node.get("note", ""),
            "base_count": base_count,
            "base_timestamp": base_ts,
            "parts_added": parts_added,
            "parts_consumed": parts_consumed,
            "current_count": round(current_count),
        })

    conn.close()
    return JSONResponse(result)


@app.post("/api/buffer_update")
async def update_buffer(request: Request):
    """Manual buffer count update by operator."""
    body = await request.json()
    buffer_id = body.get("buffer_id")
    count = body.get("count")
    if buffer_id is None or count is None:
        raise HTTPException(400, "Missing buffer_id or count")

    cfg = load_config()
    now = datetime.now()
    shift_letter, day_id, _, _ = get_shift_info(cfg, now)

    conn = get_db()
    conn.execute("""
        INSERT INTO buffer_events (timestamp, buffer_id, count, source, shift, day_id)
        VALUES (?, ?, ?, 'manual', ?, ?)
    """, (now.isoformat(), buffer_id, float(count), shift_letter, day_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "buffer_id": buffer_id, "count": count}


@app.get("/api/process_flow")
async def get_process_flow():
    """Returns process flow definition with live machine states overlaid."""
    cfg = load_config()
    flow = cfg.get("process_flow", {"nodes": [], "connections": []})

    # Overlay live machine data
    machines_resp = await get_machines("current")
    import json as _json
    machines_live = _json.loads(machines_resp.body)
    machine_map = {m["machine_id"]: m for m in machines_live}

    # Enrich operation nodes with live data
    for node in flow.get("nodes", []):
        if node["type"] == "operation":
            node_machines = node.get("machines", [])
            node["live"] = []
            node["total_good"] = 0
            node["total_bad"] = 0
            for mid in node_machines:
                if mid in machine_map:
                    m = machine_map[mid]
                    node["live"].append({
                        "machine_id": mid,
                        "machine_name": m.get("machine_name", mid),
                        "state_id": m.get("state_id", 0),
                        "ptpmc_category": m.get("ptpmc_category", "other"),
                        "good_shift": m.get("good_shift", 0),
                        "bad_shift": m.get("bad_shift", 0),
                    })
                    if not next((mc for mc in cfg["machines"] if mc["id"] == mid and mc.get("series_output")), None):
                        node["total_good"] += m.get("good_shift", 0)
                    else:
                        node["total_good"] = m.get("good_shift", 0)
                    node["total_bad"] += m.get("bad_shift", 0)

    return JSONResponse(flow)
@app.get("/flow", response_class=HTMLResponse)
async def serve_flow_editor():
    if os.path.exists(FLOW_EDITOR_FILE):
        with open(FLOW_EDITOR_FILE, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>flow_editor.html not found</h1>", status_code=404)


# -- Losses --
def _weekend_ranges(start_dt, end_dt):
    """Return list of (start, end) datetime tuples for weekend periods within a range.
    Weekend = Friday 22:00 to Sunday 22:00."""
    ranges = []
    d = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= end_dt + timedelta(days=2):
        if d.weekday() == 4:  # Friday
            ws = d.replace(hour=22)
            we = (d + timedelta(days=2)).replace(hour=22)
            if ws < end_dt and we > start_dt:
                ranges.append((max(ws, start_dt), min(we, end_dt)))
        d += timedelta(days=1)
    return ranges

def _subtract_weekends(times_dict, start_dt, end_dt, get_time_fn):
    """Subtract weekend time from each category by computing weekend-only times."""
    weekends = _weekend_ranges(start_dt, end_dt)
    for ws, we in weekends:
        wk_times = get_time_fn(ws, we)
        for cat in times_dict:
            times_dict[cat] = max(0, times_dict[cat] - wk_times.get(cat, 0))
    return times_dict

@app.get("/api/losses")
async def get_losses(request: Request, shift: str = "current"):

    cfg = load_config()
    shift_letter, day_id, shift_start, shift_end = resolve_shift(cfg, shift)
    s_start, s_end = shift_time_range(cfg, shift_letter, day_id)
    if not s_start:
        return JSONResponse([])

    conn = get_db()
    now = datetime.now()

    # Custom range params
    q = request.query_params
    custom_from = q.get("from")
    custom_to = q.get("to")
    exclude_weekends = q.get("exclude_weekends") == "1"
    custom_start = None
    custom_end = None
    if custom_from and custom_to:
        try:
            custom_start = datetime.strptime(custom_from, "%Y-%m-%d").replace(hour=6)
            custom_end = datetime.strptime(custom_to, "%Y-%m-%d").replace(hour=22)
        except ValueError:
            pass

    # Today = all shifts in current day_id
    today_start = datetime.strptime(day_id, "%Y-%m-%d").replace(hour=6, minute=0, second=0)
    today_end = today_start + timedelta(hours=24)

    result = []
    for m in cfg["machines"]:
        mid = m["id"]
        rated_ct = m.get("rated_ct", 0)

        def get_time_by_category(ts_start, ts_end):
            """Sum seconds in each ptpmc_category from state_events.
            Falls back to oee_category for records without ptpmc_category."""
            rows = conn.execute("""
                SELECT ptpmc_category, oee_category, state_id, timestamp
                FROM state_events
                WHERE machine_id=? AND timestamp>=? AND timestamp<?
                ORDER BY timestamp ASC
            """, (mid, ts_start.isoformat(), ts_end.isoformat())).fetchall()

            times = {"alarm": 0, "blocked": 0, "starved": 0, "manual": 0, "running": 0, "dressing": 0}
            for idx, row in enumerate(rows):
                cat = row["ptpmc_category"]
                # Fallback: derive from state_id or oee_category for older records
                if not cat or cat == "other":
                    sid = row["state_id"] or 0
                    oee = row["oee_category"] or ""
                    if sid in (1, 15): cat = "running"
                    elif sid == 14: cat = "dressing"
                    elif sid == 7: cat = "blocked"
                    elif sid == 8: cat = "starved"
                    elif sid in (2, 10, 13): cat = "manual"
                    elif sid in (3, 4, 5, 6, 12): cat = "alarm"
                    elif "productive" in oee: cat = "running"
                    elif "unplanned" in oee: cat = "alarm"
                    elif "planned" in oee: cat = "manual"
                    elif "minor" in oee: cat = "blocked"
                    else: cat = "other"
                if cat not in times:
                    continue
                # Duration = time until next event or end of window
                if idx + 1 < len(rows):
                    next_ts = datetime.fromisoformat(rows[idx + 1]["timestamp"])
                else:
                    next_ts = min(datetime.fromisoformat(row["timestamp"]) + timedelta(seconds=15), ts_end)
                cur_ts = datetime.fromisoformat(row["timestamp"])
                dur = max(0, (next_ts - cur_ts).total_seconds())
                times[cat] = times.get(cat, 0) + dur
            return times

        def calc_lost(seconds, rated_ct):
            if rated_ct <= 0 or seconds <= 0:
                return 0
            return round(seconds / rated_ct)

        # Get rejects
        def get_rejects(ts_start, ts_end):
            first = conn.execute("""
                SELECT bad_part_count FROM machine_snapshots
                WHERE machine_id=? AND timestamp>=? AND timestamp<?
                ORDER BY id ASC LIMIT 1
            """, (mid, ts_start.isoformat(), ts_end.isoformat())).fetchone()
            last = conn.execute("""
                SELECT bad_part_count FROM machine_snapshots
                WHERE machine_id=? AND timestamp>=? AND timestamp<?
                ORDER BY id DESC LIMIT 1
            """, (mid, ts_start.isoformat(), ts_end.isoformat())).fetchone()
            if first and last:
                return max(0, (last["bad_part_count"] or 0) - (first["bad_part_count"] or 0))
            return 0

        def get_good_parts(ts_start, ts_end):
            first = conn.execute("""
                SELECT good_part_count FROM machine_snapshots
                WHERE machine_id=? AND timestamp>=? AND timestamp<?
                ORDER BY id ASC LIMIT 1
            """, (mid, ts_start.isoformat(), ts_end.isoformat())).fetchone()
            last = conn.execute("""
                SELECT good_part_count FROM machine_snapshots
                WHERE machine_id=? AND timestamp>=? AND timestamp<?
                ORDER BY id DESC LIMIT 1
            """, (mid, ts_start.isoformat(), ts_end.isoformat())).fetchone()
            if first and last:
                return max(0, (last["good_part_count"] or 0) - (first["good_part_count"] or 0))
            return 0

        def calc_overtime_lost(parts_made, run_seconds, rated_ct):
            """Parts lost to cycle time exceeding rated CT."""
            if rated_ct <= 0 or parts_made <= 0 or run_seconds <= 0:
                return 0
            ideal_parts = run_seconds / rated_ct
            lost = ideal_parts - parts_made
            return max(0, round(lost))

        # Shift data
        shift_times = get_time_by_category(s_start, s_end)
        shift_rejects = get_rejects(s_start, s_end)
        shift_parts = get_good_parts(s_start, s_end)

        # Today data
        today_times = get_time_by_category(today_start, today_end)
        today_rejects = get_rejects(today_start, today_end)
        today_parts = get_good_parts(today_start, today_end)

        # Custom range data
        custom_data = {}
        if custom_start and custom_end:
            cust_times = get_time_by_category(custom_start, custom_end)
            cust_rejects = get_rejects(custom_start, custom_end)
            cust_parts = get_good_parts(custom_start, custom_end)
            if exclude_weekends:
                cust_times = _subtract_weekends(cust_times, custom_start, custom_end, get_time_by_category)
                weekends = _weekend_ranges(custom_start, custom_end)
                for ws, we in weekends:
                    cust_rejects = max(0, cust_rejects - get_rejects(ws, we))
                    cust_parts = max(0, cust_parts - get_good_parts(ws, we))
            custom_data = {
                "alarm_min": round(cust_times["alarm"] / 60, 1),
                "blocked_min": round(cust_times["blocked"] / 60, 1),
                "starved_min": round(cust_times["starved"] / 60, 1),
                "manual_min": round(cust_times["manual"] / 60, 1),
                "lost_alarm": calc_lost(cust_times["alarm"], rated_ct),
                "lost_blocked": calc_lost(cust_times["blocked"], rated_ct),
                "lost_starved": calc_lost(cust_times["starved"], rated_ct),
                "lost_manual": calc_lost(cust_times["manual"], rated_ct),
                "rejects": cust_rejects,
                "parts_made": cust_parts,
                "run_min": round(cust_times["running"] / 60, 1),
                "overtime_lost": calc_overtime_lost(cust_parts, cust_times["running"], rated_ct),
            }

        entry = {
            "machine_id": mid,
            "machine_name": m["name"],
            "op": m["op"],
            "op_name": m.get("op_name", ""),
            "rated_ct": rated_ct,
            "shift": {
                "alarm_min": round(shift_times["alarm"] / 60, 1),
                "blocked_min": round(shift_times["blocked"] / 60, 1),
                "starved_min": round(shift_times["starved"] / 60, 1),
                "manual_min": round(shift_times["manual"] / 60, 1),
                "lost_alarm": calc_lost(shift_times["alarm"], rated_ct),
                "lost_blocked": calc_lost(shift_times["blocked"], rated_ct),
                "lost_starved": calc_lost(shift_times["starved"], rated_ct),
                "lost_manual": calc_lost(shift_times["manual"], rated_ct),
                "rejects": shift_rejects,
                "parts_made": shift_parts,
                "run_min": round(shift_times["running"] / 60, 1),
                "overtime_lost": calc_overtime_lost(shift_parts, shift_times["running"], rated_ct),
            },
            "today": {
                "alarm_min": round(today_times["alarm"] / 60, 1),
                "blocked_min": round(today_times["blocked"] / 60, 1),
                "starved_min": round(today_times["starved"] / 60, 1),
                "manual_min": round(today_times["manual"] / 60, 1),
                "lost_alarm": calc_lost(today_times["alarm"], rated_ct),
                "lost_blocked": calc_lost(today_times["blocked"], rated_ct),
                "lost_starved": calc_lost(today_times["starved"], rated_ct),
                "lost_manual": calc_lost(today_times["manual"], rated_ct),
                "rejects": today_rejects,
                "parts_made": today_parts,
                "run_min": round(today_times["running"] / 60, 1),
                "overtime_lost": calc_overtime_lost(today_parts, today_times["running"], rated_ct),
            },
        }
        if custom_data:
            entry["custom"] = custom_data
        result.append(entry)

    conn.close()
    return JSONResponse(result)


# -- Tool History --
@app.get("/api/tool_history")
async def get_tool_history(machine_id: str = None, limit: int = 10):
    """Tool change history with reason and notes."""
    conn = get_db()
    if machine_id:
        rows = conn.execute("""
            SELECT * FROM tool_change_events
            WHERE machine_id=?
            ORDER BY timestamp DESC LIMIT ?
        """, (machine_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM tool_change_events
            ORDER BY timestamp DESC LIMIT ?
        """, (limit * 17,)).fetchall()

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "machine_id": r["machine_id"],
            "machine_name": r["machine_name"],
            "op": r["op"],
            "tool_slot": r["tool_slot"],
            "parts_run": r["parts_run"],
            "shift_letter": r["shift_letter"],
            "day_id": r["day_id"],
            "reason": r["reason"] or "tool_life_utilized",
            "notes": r["notes"] or "",
        })
    conn.close()
    return JSONResponse(result)


# -- Update tool change reason --
@app.post("/api/tool_change_reason")
async def update_tool_change_reason(request: Request):
    """Update reason and notes on a tool change event."""
    body = await request.json()
    event_id = body.get("id")
    reason = body.get("reason", "tool_life_utilized")
    notes = body.get("notes", "")
    if not event_id:
        raise HTTPException(400, "Missing id")
    if reason not in ("tool_life_utilized", "other"):
        raise HTTPException(400, "Invalid reason")
    if len(notes) > 160:
        notes = notes[:160]
    conn = get_db()
    conn.execute("UPDATE tool_change_events SET reason=?, notes=? WHERE id=?",
                 (reason, notes, event_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


if __name__ == "__main__":
    cfg = load_config()
    port = cfg["area"]["api_port"]
    print(f"MIS API starting - Area {cfg['area']['code']} on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
