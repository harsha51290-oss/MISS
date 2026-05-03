"""
MIS Local Settings Editor - localsettings.py
Standalone config editor for MISS PC local settings.
Opens http://localhost:9090 in your browser.
Edit IPs, ports, scan intervals, down timers.
MIS-pushed data (targets, MSD) shown read-only.

Usage: python localsettings.py
"""

import json
import os
import sys
import webbrowser
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

AREA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AREA_DIR, "config.json")
BACKUP_DIR = os.path.join(AREA_DIR, "backups", "config")
os.makedirs(BACKUP_DIR, exist_ok=True)
PORT = 9090


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(cfg):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(CONFIG_FILE, "r") as f:
        with open(os.path.join(BACKUP_DIR, f"config_{ts}.json"), "w") as bf:
            bf.write(f.read())
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def build_page(cfg, msg=""):
    area = cfg["area"]
    machines = cfg["machines"]
    local = cfg.get("local", {})
    scan = local.get("scan_intervals", {"tier1_sec": 1, "tier2_sec": 15})
    timers = local.get("down_timers", {"down_confirm_sec": 20, "recover_confirm_sec": 60})
    tool_thresh = local.get("tool_change_threshold", 50)
    consec = local.get("consecutive_good_target", 10)

    msg_html = ""
    if msg:
        msg_html = f'<div class="msg">{msg}</div>'

    # Machine rows
    mrows = ""
    for m in machines:
        mrows += f"""<tr>
<td class="mono id-col">{m['id']}</td>
<td>{m['name']}</td>
<td>{m.get('op_name','')}</td>
<td><input type="text" name="ip_{m['id']}" value="{m.get('ip','')}" class="ip-input"></td>
<td><input type="number" name="slot_{m['id']}" value="{m.get('slot',0)}" class="num-input" min="0" max="10"></td>
<td><input type="text" name="prog_{m['id']}" value="{m.get('program_prefix','Program:Illuminate')}" class="prog-input"></td>
<td><input type="text" name="udt_{m['id']}" value="{m.get('udt_tag','')}" class="ip-input"></td>
<td class="ro">{m.get('rated_ct','')}</td>
<td class="ro">{m.get('shift_target','')}</td>
</tr>"""

    # Build tool setup rows
    toolrows = ""
    for m in machines:
        tools = m.get("tools", [])
        mid = m["id"]
        toolrows += '<tr><td colspan="3" style="background:#F0F5FF;font-weight:800;padding:8px 10px;border-left:3px solid #0033A0">' + mid + ' - ' + m["name"] + '</td></tr>\n'
        for slot in range(7):
            t = tools[slot] if slot < len(tools) and isinstance(tools[slot], dict) else {}
            desc = t.get("description", "")
            life = t.get("expected_life", 0)
            toolrows += '<tr>'
            toolrows += '<td style="padding-left:20px;font-family:monospace;font-weight:700;color:#555">T' + str(slot) + '</td>'
            toolrows += '<td><input type="text" name="tdesc_' + mid + '_' + str(slot) + '" value="' + str(desc) + '" style="width:200px;font-size:12px;padding:3px 6px;border:1.5px solid #ddd;border-radius:3px" placeholder="Tool description"></td>'
            toolrows += '<td><input type="number" name="tlife_' + mid + '_' + str(slot) + '" value="' + str(life) + '" style="width:100px;font-size:12px;padding:3px 6px;border:1.5px solid #ddd;border-radius:3px;font-family:monospace" min="0" placeholder="0"></td>'
            toolrows += '</tr>\n'

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Local Settings - {area['code']} {area['name']}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#f5f5f5;color:#222}}
.hdr{{background:#0033A0;color:white;padding:14px 24px;display:flex;align-items:center;gap:14px}}
.hdr .logo{{background:white;color:#0033A0;font-weight:900;font-size:15px;padding:4px 10px;border-radius:2px}}
.hdr h1{{font-size:17px;font-weight:800;text-transform:uppercase}}
.hdr .sub{{font-size:11px;color:rgba(255,255,255,.7);margin-top:2px}}
.wrap{{max-width:1200px;margin:0 auto;padding:20px}}
.msg{{background:#e4f5da;color:#2d5a16;border:2px solid #b8dda0;border-radius:4px;padding:12px;margin-bottom:16px;font-weight:700}}
.card{{background:white;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:16px;overflow:hidden}}
.card-hdr{{background:#122C6C;color:white;padding:10px 16px;font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;display:flex;align-items:center;gap:10px}}
.card-hdr .tag{{padding:2px 8px;border-radius:2px;font-size:10px;font-weight:700}}
.tag.local{{background:#39B54A}}.tag.mis{{background:#F57F20}}.tag.mixed{{background:#2371E7}}
.card-body{{padding:16px}}
.row{{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}}
.field{{display:flex;flex-direction:column;gap:4px}}
.field label{{font-size:11px;font-weight:700;color:#555;text-transform:uppercase}}
.field input,.field select{{border:1.5px solid #ddd;padding:6px 10px;border-radius:3px;font-size:13px;font-family:monospace}}
.field input:focus{{border-color:#0033A0;outline:none}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#f0f0f0;padding:8px;text-align:left;font-size:10px;text-transform:uppercase;font-weight:700;color:#555;border-bottom:2px solid #ddd}}
td{{padding:6px 8px;border-bottom:1px solid #eee}}
.mono{{font-family:monospace;font-weight:700}}.id-col{{color:#0033A0}}
.ro{{color:#999;font-style:italic}}
.ip-input{{width:140px;font-family:monospace;font-size:12px;padding:4px 6px;border:1.5px solid #ddd;border-radius:3px}}
.num-input{{width:50px;font-family:monospace;font-size:12px;padding:4px 6px;border:1.5px solid #ddd;border-radius:3px}}
.prog-input{{width:180px;font-family:monospace;font-size:12px;padding:4px 6px;border:1.5px solid #ddd;border-radius:3px}}
.ip-input:focus,.num-input:focus,.prog-input:focus{{border-color:#0033A0;outline:none}}
.btn{{background:#0033A0;color:white;border:none;padding:10px 24px;border-radius:3px;font-size:14px;font-weight:800;cursor:pointer;text-transform:uppercase;letter-spacing:.05em}}
.btn:hover{{background:#122C6C}}
.btn-row{{display:flex;gap:10px;margin-top:8px;align-items:center}}
.note{{font-size:11px;color:#999;margin-top:4px}}
.legend{{display:flex;gap:16px;margin-bottom:16px;font-size:12px;font-weight:700}}
.legend span{{display:flex;align-items:center;gap:4px}}
.legend .dot{{width:12px;height:12px;border-radius:2px}}
.dot.local{{background:#39B54A}}.dot.mis{{background:#F57F20}}.dot.ro{{background:#ddd}}
.footer{{padding:12px 24px;font-size:11px;color:#999;text-align:center;margin-top:20px}}
</style></head><body>
<div class="hdr">
<span class="logo">GM</span>
<div><h1>Local Settings Editor</h1>
<div class="sub">Area {area['code']}: {area['name']} - {area.get('dept','')}</div></div>
</div>
<div class="wrap">
{msg_html}
<div class="legend">
<span><div class="dot local"></div> LOCAL - Editable on this PC</span>
<span><div class="dot mis"></div> MIS - Pushed from central (read-only)</span>
<span><div class="dot ro"></div> CONFIG - Set during initial setup</span>
</div>

<form method="POST" action="/save">

<div class="card">
<div class="card-hdr">Area Info <span class="tag local">LOCAL</span></div>
<div class="card-body">
<div class="row">
<div class="field"><label>Area Code</label><input name="area_code" value="{area['code']}"></div>
<div class="field"><label>Area Name</label><input name="area_name" value="{area['name']}" style="width:250px"></div>
<div class="field"><label>Department</label><input name="area_dept" value="{area.get('dept','')}" style="width:140px"></div>
<div class="field"><label>API Port</label><input type="number" name="api_port" value="{area.get('api_port',8000)}" style="width:80px"></div>
</div>
</div></div>

<div class="card">
<div class="card-hdr">Scan & Timer Settings <span class="tag local">LOCAL</span></div>
<div class="card-body">
<div class="row">
<div class="field"><label>Tier 1 Interval (sec)</label><input type="number" name="tier1_sec" value="{scan.get('tier1_sec',1)}" min="1" max="10"></div>
<div class="field"><label>Tier 2 Interval (sec)</label><input type="number" name="tier2_sec" value="{scan.get('tier2_sec',15)}" min="5" max="60"></div>
<div class="field"><label>Down Confirm (sec)</label><input type="number" name="down_confirm" value="{timers.get('down_confirm_sec',20)}" min="5" max="120"></div>
<div class="field"><label>Recover Confirm (sec)</label><input type="number" name="recover_confirm" value="{timers.get('recover_confirm_sec',60)}" min="10" max="300"></div>
<div class="field"><label>Tool Change Threshold</label><input type="number" name="tool_thresh" value="{tool_thresh}" min="10" max="500"></div>
<div class="field"><label>Consec Good Target</label><input type="number" name="consec_target" value="{consec}" min="5" max="100"></div>
</div>
<div class="note">Tier 1 reads StateID only (fast). Tier 2 reads all tags (parts, tools, support). Down confirm = seconds before marking machine as down.</div>
</div></div>

<div class="card">
<div class="card-hdr">Machine Connections <span class="tag mixed">LOCAL + MIS</span></div>
<div class="card-body">
<div class="note" style="margin-bottom:10px">
<b>LOCAL columns (editable):</b> IP Address, Slot, Program Prefix, UDT Tag - set these for each machine's PLC/CNC connection.<br>
<b>MIS columns (read-only):</b> Rated CT, Shift Target - pushed from MIS central.<br>
Machines on the same IP are batched into a single PLC read automatically.
</div>
<div style="overflow-x:auto">
<table>
<tr><th>Machine ID</th><th>Name</th><th>Operation</th><th>IP Address <span class="tag local" style="font-size:8px">LOCAL</span></th><th>Slot</th><th>Program Prefix</th><th>UDT Tag <span class="tag local" style="font-size:8px">LOCAL</span></th><th>CT</th><th>Target</th></tr>
{mrows}
</table></div>
<div class="note">Each machine can have a unique IP (e.g. individual Fanuc CNCs) or share an IP with other machines on the same PLC.</div>
</div></div>

<div class="card">
<div class="card-hdr">Tool Setup <span class="tag local">LOCAL</span></div>
<div class="card-body">
<div class="note" style="margin-bottom:10px">
Set tool description and expected life (parts) for each tool slot (T0-T6) per machine.<br>
Tool slots map to PLC tags: T0 = UDT.ToolCount[0], T1 = UDT.ToolCount[1], etc.<br>
Set Expected Life to 0 if unknown - system will learn from actual tool changes.
</div>
<div style="overflow-x:auto">
<table>
<tr><th>Slot</th><th>Description</th><th>Expected Life (parts)</th></tr>
{toolrows}
</table></div>
</div></div>

<div class="btn-row">
<button type="submit" class="btn">Save Local Settings</button>
<span class="note">Saves to config.json. Backup created automatically. Collector picks up changes within 30 seconds.</span>
</div>
</form>
</div>
<div class="footer">MIS Local Settings Editor - {area['code']} {area['name']} - config.json: {CONFIG_FILE}</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress HTTP log noise

    def do_GET(self):
        cfg = load_config()
        html = build_page(cfg)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")

        # Parse form data
        from urllib.parse import parse_qs
        params = parse_qs(body)

        def pv(key, default=""):
            v = params.get(key, [default])
            return v[0] if v else default

        cfg = load_config()

        # Update area
        cfg["area"]["code"] = pv("area_code", cfg["area"]["code"])
        cfg["area"]["name"] = pv("area_name", cfg["area"]["name"])
        cfg["area"]["dept"] = pv("area_dept", cfg["area"].get("dept", ""))
        cfg["area"]["api_port"] = int(pv("api_port", str(cfg["area"].get("api_port", 8000))))

        # Update local settings
        if "local" not in cfg:
            cfg["local"] = {}
        cfg["local"]["scan_intervals"] = {
            "tier1_sec": int(pv("tier1_sec", "1")),
            "tier2_sec": int(pv("tier2_sec", "15")),
        }
        cfg["local"]["down_timers"] = {
            "down_confirm_sec": int(pv("down_confirm", "20")),
            "recover_confirm_sec": int(pv("recover_confirm", "60")),
        }
        cfg["local"]["tool_change_threshold"] = int(pv("tool_thresh", "50"))
        cfg["local"]["consecutive_good_target"] = int(pv("consec_target", "10"))

        # Update per-machine IPs and UDT tags
        for m in cfg["machines"]:
            mid = m["id"]
            ip_val = pv(f"ip_{mid}", m.get("ip", ""))
            slot_val = pv(f"slot_{mid}", str(m.get("slot", 0)))
            prog_val = pv(f"prog_{mid}", m.get("program_prefix", "Program:Illuminate"))
            udt_val = pv(f"udt_{mid}", m.get("udt_tag", ""))
            m["ip"] = ip_val
            m["slot"] = int(slot_val)
            m["program_prefix"] = prog_val
            m["udt_tag"] = udt_val

            # Save tool setup
            tool_list = []
            for slot_num in range(7):
                tdesc = pv(f"tdesc_{mid}_{slot_num}", "")
                tlife = pv(f"tlife_{mid}_{slot_num}", "0")
                tool_list.append({
                    "description": tdesc,
                    "expected_life": int(tlife) if tlife else 0
                })
            m["tools"] = tool_list

        save_config(cfg)

        # Redirect back with success message
        html = build_page(cfg, msg=f"Settings saved at {datetime.now().strftime('%H:%M:%S')}. Backup created.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: config.json not found in {AREA_DIR}")
        print("Copy config.json to this folder first.")
        input("Press Enter to exit...")
        sys.exit(1)

    cfg = load_config()
    area = cfg["area"]
    print(f"MIS Local Settings Editor")
    print(f"  Area: {area['code']} - {area['name']}")
    print(f"  Config: {CONFIG_FILE}")
    print(f"  Opening http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop")
    print()

    # Open browser after a short delay
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSettings editor closed.")
        server.shutdown()


if __name__ == "__main__":
    main()
