# Manufacturing Intelligence Dashboard (MID)
## Grand Rapids Operation — MISS PC Standard

---

## FOLDER STRUCTURE

```
C:\MIS\
└── 101\                        ← Area code folder
    ├── collector.py            ← DO NOT EDIT (standard)
    ├── api.py                  ← DO NOT EDIT (standard)
    ├── backup.py               ← DO NOT EDIT (standard)
    ├── dashboard.html          ← DO NOT EDIT (standard)
    ├── migrate_db.py           ← DO NOT EDIT (standard)
    ├── setup.ps1               ← DO NOT EDIT (standard)
    ├── config.json             ← EDIT THIS for area settings
    ├── config.xlsx             ← Reference doc (optional)
    ├── mis.db                  ← Database (auto-created)
    ├── README.md               ← This file
    ├── logs\
    │   ├── collector.log
    │   ├── api.log
    │   └── backup.log
    └── backups\
        ├── mis_20260402_000014.db
        └── config\
            └── config_20260402_080000.json
```

**Rule: Only ever edit config.json — never edit the .py or .html files.**

---

## FIRST TIME SETUP

```powershell
cd C:\MIS\101
.\setup.ps1
```

That's it. Setup will:
- Validate config.json
- Create folder structure
- Install Python packages
- Initialise database
- Register PM2 processes
- Configure auto-start on reboot

---

## DAILY OPERATIONS

### Check system status
```powershell
pm2 list
# All processes should show: online
```

### View live logs
```powershell
pm2 logs 101-collector
pm2 logs 101-api
pm2 logs 101-backup
```

### Restart a process
```powershell
pm2 restart 101-collector
pm2 restart 101-api
pm2 restart 101-backup
```

---

## ADDING A NEW MACHINE

1. Open browser → `http://localhost:8000/config`
2. Login
3. Go to **1. Machines** tab
4. Note the existing Machine ID format (e.g. BH_BAH1, BH_BAH2)
5. Add new row in `config.json` under `"machines"`:

```json
{
  "id":            "BH_BAH8",
  "name":          "Bahmueller 8",
  "op":            "190",
  "op_name":       "Body ID Grind Area",
  "plc_id":        "PLC_1",
  "udt_tag":       "MIS_BodyBM_8",
  "rated_ct":      10.8,
  "shift_target":  1735,
  "series_output": false,
  "tools":         []
}
```

6. Save config.json
7. Collector picks up new machine on next scan cycle — no restart needed

**Important:** Machine ID (e.g. BH_BAH8) is permanent.
Never change it once production data exists — all history ties to this ID.

---

## REMOVING A MACHINE

1. Open `config.json` in VS Code
2. Find the machine entry and delete the entire `{ }` block
3. Save
4. Collector stops collecting for that machine on next scan
5. Historical data remains in database — it is never deleted

**Note:** Do not remove a machine if it has active tool change or downtime data
you still need to report on. Mark it inactive instead by setting:
```json
"shift_target": 0
```

---

## UPDATING MACHINE SETTINGS

### Change shift target or rated CT:
1. Open browser → `http://localhost:8000/config`
2. Login → **1. Machines** tab
3. Edit the value → **Save Changes**

### Update via Excel:
1. Download Config (.xlsx) from config page
2. Edit in Excel
3. Upload Config (.xlsx)
4. Changes apply immediately

### Update hourly targets:
1. Config page → **2. Hourly Targets** tab
2. Edit values → Save Changes

---

## UPDATING SHIFT SCHEDULE OR BREAKS

1. Config page → **3. Shift Schedule** tab
2. Edit times or break minutes → Save Changes

**Day ID rule (do not change):**
- C Shift 22:00→06:00 = Day ID is the date it ENDS (next morning)
- A Shift 06:00→14:00 = Day ID is same date
- B Shift 14:00→22:00 = Day ID is same date
- All 3 shifts share same Day ID = one production day

---

## ADDING TOOL COUNTER CONFIG

1. Config page → **7. Tool Counters** tab
2. Find the machine
3. Enter description and expected life for each tool slot (T0-T6)
4. Save Changes

Tool slots map directly to PLC tags:
- T0 → `MIS_MachineName.ToolCount[0]`
- T1 → `MIS_MachineName.ToolCount[1]`
- etc.

Set Expected Life to 0 if unknown — system learns from actual changes.

---

## ADDING A NEW PLC

If a new machine connects to a different PLC:

1. Open `config.json` in VS Code
2. Add new PLC entry under `"plc"`:
```json
{
  "id":             "PLC_2",
  "name":           "New PLC",
  "ip":             "120.165.238.99",
  "slot":           0,
  "program_prefix": "Program:Illuminate"
}
```
3. Update the machine entry to reference `"plc_id": "PLC_2"`
4. Restart collector: `pm2 restart 101-collector`

---

## SETTING UP A NEW AREA ON A NEW PC

1. Create folder: `mkdir C:\MIS\102`
2. Copy all standard files into `C:\MIS\102\`
3. Copy `config.json` from another area as a template
4. Edit `config.json`:
   - Change `area.code` to `102`
   - Change `area.name` to new area name
   - Change `area.api_port` to next available port (e.g. 8001)
   - Update `plc` section with correct IP
   - Update `machines` section with correct machines
5. Run: `cd C:\MIS\102 && .\setup.ps1`

---

## IF A PC NEEDS TO BE REBUILT

1. Fresh Windows install
2. Install: Python 3.12, Node.js, PM2, VS Code, Git
3. Copy standard files to `C:\MIS\101\`
4. Restore `config.json` from backup (`C:\MIS\101\backups\config\`)
5. Restore `mis.db` from backup (`C:\MIS\101\backups\`)
6. Run: `.\setup.ps1`
7. Done — back online in under 10 minutes

---

## SYSTEM HEALTH

Dashboard shows health status in the message banner:
- ✅ Green = all systems normal
- ⚠ Yellow = warning (e.g. collector restarted)
- 🔴 Red = critical (e.g. PLC offline, backup overdue)

Health API (for MIS central server monitoring):
```
GET http://localhost:8000/api/health
```

---

## CONTACTS

| Role | Name | Contact |
|---|---|---|
| Controls Engineering Lead | Harsha Kunchala | — |
| IT Support | — | — |

---

*Manufacturing Intelligence Dashboard — General Motors Grand Rapids*
*Document version: 1.0 — April 2026*
