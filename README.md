# Manufacturing Intelligence System (MIS)
## Area 102 — Body Hard End
## Grand Rapids Operation — MISS PC Standard

---

## FILE STRUCTURE

```
C:\MIS\102\
├── collector.py        ← PLC data collector (runs under PM2)
├── api.py              ← FastAPI REST server (runs under PM2)
├── backup.py           ← Database & config backup agent (runs under PM2)
├── dashboard.html      ← Production dashboard UI
├── config.html         ← Configuration management UI
├── flow.html           ← Process flow visualization
├── setup.ps1           ← One-time deployment script
├── config.json         ← EDIT THIS for area settings
├── mis.db              ← SQLite database (auto-created)
├── mis.db-wal          ← SQLite write-ahead log (runtime)
├── mis.db-shm          ← SQLite shared memory (runtime)
├── README.md           ← This file
├── .gitignore
├── logs\
│   ├── collector.log
│   ├── api.log
│   └── backup.log
└── backups\
    ├── mis_20260502_000014.db
    └── config\
        └── config_20260502_080000.json
```

**Rule: Only edit `config.json` — never edit .py or .html files directly.**

---

## ARCHITECTURE

```
PLC (Allen-Bradley)
  │  pylogix (CIP/EtherNet/IP)
  ▼
collector.py ──writes──► mis.db (SQLite, WAL mode)
                              │
api.py ────reads─────────────┘
  │
  ├── /dashboard  → dashboard.html
  ├── /config     → config.html
  ├── /flow       → flow.html
  └── /api/*      → JSON endpoints for MIS central

backup.py ──── daily DB backup (sqlite3.backup API, live-safe)
           └── hourly config.json backup
```

**Three PM2 processes per area:**
| Process | Purpose | Port |
|---------|---------|------|
| 102-collector | Reads PLC tags every scan cycle, writes to mis.db | — |
| 102-api | Serves dashboard + REST API | 8001 |
| 102-backup | Daily DB backup, hourly config backup | — |

---

## FIRST TIME SETUP

```powershell
cd C:\MIS\102
.\setup.ps1
```

Setup will:
1. Validate config.json
2. Create folder structure (logs/, backups/)
3. Check all required files are present
4. Install Python packages (pylogix, fastapi, uvicorn, openpyxl)
5. Register PM2 processes
6. Save PM2 config for auto-start on reboot

---

## URLS

| Page | URL |
|------|-----|
| Dashboard | http://localhost:8001/dashboard |
| Config | http://localhost:8001/config |
| Process Flow | http://localhost:8001/flow |
| Health API | http://localhost:8001/api/health |

---

## API ENDPOINTS

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/health | System health + PLC connection status |
| GET | /api/machines | All machine data for current/selected shift |
| GET | /api/op_totals | Operation-level production totals |
| GET | /api/day_totals | Daily totals across all shifts |
| GET | /api/hourly_trend | Hourly production per machine |
| GET | /api/active_trades | Active support/trade calls |
| GET | /api/tool_current | Current tool counter values |
| GET | /api/plc_status | PLC connection status (for banner) |
| GET | /api/data | Simple endpoint for MIS central |
| GET | /api/config | Current config.json |
| POST | /api/config | Update config.json |
| POST | /api/demand | Push daily targets from MIS central |
| GET | /api/demand | Get current demand targets |
| GET | /api/process_flow | PT-PM&C process flow data |

---

## DAILY OPERATIONS

### Check system status
```powershell
pm2 list
# All 3 processes should show: online
```

### View live logs
```powershell
pm2 logs 102-collector
pm2 logs 102-api
pm2 logs 102-backup
```

### Restart a process
```powershell
pm2 restart 102-collector
pm2 restart 102-api
pm2 restart 102-backup
```

---

## CONFIGURATION (via browser)

Open http://localhost:8001/config to manage:

1. **Machines** — add/remove/edit machines, set rated CT and shift targets
2. **Hourly Targets** — per-hour production targets
3. **Shift Schedule** — shift times and break minutes
4. **PLCs** — PLC connection settings (IP, slot, program prefix)
5. **Operations** — operation groupings
6. **Tool Counters** — tool descriptions and expected life per slot
7. **Audit Log** — full config change history with restore capability

All changes take effect immediately — no restart needed (collector picks up on next scan cycle).

---

## ADDING A NEW MACHINE

1. Open http://localhost:8001/config → **Machines** tab
2. Add new machine with required fields:
   - **Machine ID** (permanent, e.g. `BH_BAH8`)
   - **Name** (display name)
   - **Operation** (op code)
   - **PLC ID** (which PLC it connects to)
   - **UDT Tag** (PLC tag name)
   - **Rated CT** (seconds)
   - **Shift Target** (parts per shift)
3. Save Changes

**Important:** Machine ID is permanent. Never change it once production data exists — all history ties to this ID.

---

## ADDING A NEW PLC

1. Open config.json or use Config UI → PLCs tab
2. Add new PLC entry:
```json
{
  "id":             "PLC_2",
  "name":           "New PLC",
  "ip":             "120.165.238.99",
  "slot":           0,
  "program_prefix": "Program:Illuminate"
}
```
3. Update machine entries to reference the new `plc_id`
4. Restart collector: `pm2 restart 102-collector`

---

## BACKUP SYSTEM

**backup.py** runs continuously under PM2:
- **Database:** Backed up daily at midnight using SQLite's `.backup()` API (atomic, live-safe — no corruption risk from active writes)
- **Config:** Backed up hourly
- **Retention:** 20 DB backups, 48 config backups
- **Secondary:** Copies to `D:\MIS_Backups\102\` if D: drive exists

Backups stored in `backups/` and `backups/config/`.

---

## SHIFT & DAY ID RULES

| Shift | Time | Day ID |
|-------|------|--------|
| C | 22:00 → 06:00 | Date shift ENDS (next morning) |
| A | 06:00 → 14:00 | Same date |
| B | 14:00 → 22:00 | Same date |

All 3 shifts share the same Day ID = one production day.

---

## SETTING UP A NEW AREA ON A NEW PC

1. Create folder: `mkdir C:\MIS\<area_code>`
2. Copy all standard files (py, html, ps1) into the new folder
3. Copy `config.json` from another area as template
4. Edit `config.json`:
   - `area.code` → new area code
   - `area.name` → new area name
   - `area.api_port` → next available port
   - `plc` → correct PLC IPs
   - `machines` → correct machine list
5. Run: `cd C:\MIS\<area_code>` then `.\setup.ps1`

---

## PC REBUILD PROCEDURE

1. Fresh Windows install
2. Install: Python 3.12, Node.js, PM2, VS Code, Git
3. Copy standard files to `C:\MIS\102\`
4. Restore `config.json` from `backups\config\`
5. Restore `mis.db` from `backups\`
6. Run `.\setup.ps1`
7. Back online in under 10 minutes

---

## SYSTEM HEALTH

Dashboard banner shows status:
- **Green** — all systems normal
- **Yellow** — warning (e.g. collector restarted)
- **Red** — critical (e.g. PLC offline, backup overdue)

---

## CONTACTS

| Role | Name |
|------|------|
| Controls Engineering Lead | Harsha Kunchala |

---

*Manufacturing Intelligence System — General Motors Grand Rapids*
*Area 102 — Body Hard End — Dept 909 / DEAC*
*Document version: 2.0 — May 2026*
