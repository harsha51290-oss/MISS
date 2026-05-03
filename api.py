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
BACKUP_DIR = os.path.join(AREA_DIR, "backups", "config")
os.makedirs(BACKUP_DIR, exist_ok=True)

PASSWORD = "Iamcontrols@2100"
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

def calc_expected_now(cfg, shift_start, shift_target):
    now = datetime.now()
    elapsed = min((now - shift_start).total_seconds(), 7.25 * 3600)
    if elapsed <= 0 or shift_target <= 0:
        return 0
    breaks = cfg.get("breaks", {})
    total_break_sec = 0
    for hr_key, mins in breaks.items():
        if elapsed > (int(hr_key) - 1) * 3600:
            total_break_sec += int(mins) * 60
    available = max(0, elapsed - total_break_sec)
    total_available = 7.25 * 3600 - sum(int(m) * 60 for m in breaks.values())
    return round(shift_target * (available / total_available)) if total_available > 0 else 0

def calc_hourly_target(cfg, shift_target):
    breaks = cfg.get("breaks", {})
    available_min = 7.25 * 60 - sum(int(v) for v in breaks.values())
    return round(shift_target / (available_min / 60)) if available_min > 0 else 0


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

        expected = calc_expected_now(cfg, shift_start, shift_target) if shift == "current" else good_shift

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
            "hourly_target": calc_hourly_target(cfg, shift_target),
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
    sc = cfg.get("support_codes", {})
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

        tools = []
        for slot in range(7):
            count = (snap[slot] or 0) if snap else 0
            avg_life = avg_map.get(slot, 0)
            pct_used = round(count / avg_life * 100) if avg_life > 0 else None
            tools.append({"slot": slot, "count": count, "avg_life": avg_life, "pct_used": pct_used})

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

@app.post("/api/config")
async def update_config(request: Request):
    body = await request.json()
    if body.get("password") != PASSWORD:
        raise HTTPException(403, "Invalid password")
    new_config = body.get("config")
    if not new_config:
        raise HTTPException(400, "Missing config data")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(CONFIG_FILE, "r") as f:
        with open(os.path.join(BACKUP_DIR, f"config_{ts}.json"), "w") as bf:
            bf.write(f.read())
    with open(CONFIG_FILE, "w") as f:
        json.dump(new_config, f, indent=2)
    return {"status": "ok"}


# -- Demand --
@app.post("/api/demand")
async def push_demand(request: Request):
    body = await request.json()
    if body.get("password") != PASSWORD:
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
    cfg = load_config()
    a = cfg["area"]
    h = f'<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Config - {a["code"]}</title>'
    h += '<style>body{font-family:Arial;margin:20px;background:#f5f5f5}h1{color:#001B3A}'
    h += '.c{background:white;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.1)}'
    h += 'table{width:100%;border-collapse:collapse;font-size:13px}th{background:#002856;color:white;padding:8px;text-align:left}'
    h += 'td{padding:6px 8px;border-bottom:1px solid #eee}.m{font-family:monospace}</style></head><body>'
    h += f'<h1>Config - Area {a["code"]}: {a["name"]}</h1>'
    h += f'<p>Port: {a["api_port"]} | Machines: {len(cfg["machines"])} | <a href="/dashboard">Dashboard</a> | <a href="/api/health">Health</a></p>'
    h += '<div class="c"><h3>Machines</h3><table><tr><th>ID</th><th>Name</th><th>Op Name</th><th>IP</th><th>UDT Tag</th><th>CT</th><th>Target</th></tr>'
    for m in cfg["machines"]:
        h += f'<tr><td class="m">{m["id"]}</td><td>{m["name"]}</td><td>{m.get("op_name","")}</td><td class="m">{m.get("ip","")}</td><td class="m">{m.get("udt_tag","")}</td><td>{m.get("rated_ct","")}</td><td>{m.get("shift_target","")}</td></tr>'
    h += '</table></div></body></html>'
    return HTMLResponse(h)


if __name__ == "__main__":
    cfg = load_config()
    port = cfg["area"]["api_port"]
    print(f"MIS API starting - Area {cfg['area']['code']} on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
