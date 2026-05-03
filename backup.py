"""
MIS Backup Agent - backup.py
Runs continuously under PM2.
- Backs up mis.db daily at midnight
- Backs up config.json hourly
- Keeps last 20 db backups, 48 config backups
- Writes backup_status.json for /api/health

Backup locations:
  Primary:   {area_dir}/backups/
  Secondary: D:/MIS_Backups/{area_code}/ (if D: drive exists)
"""

import json
import os
import shutil
import time
import logging
from datetime import datetime
from pathlib import Path

# ── Setup ────────────────────────────────────────────────────
AREA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AREA_DIR, "config.json")
DB_FILE = os.path.join(AREA_DIR, "mis.db")
BACKUP_DIR = os.path.join(AREA_DIR, "backups")
CONFIG_BACKUP_DIR = os.path.join(BACKUP_DIR, "config")
STATUS_FILE = os.path.join(AREA_DIR, "backup_status.json")
LOG_DIR = os.path.join(AREA_DIR, "logs")

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "backup.log"), encoding="utf-8")
    ]
)
log = logging.getLogger("backup")

MAX_DB_BACKUPS = 20
MAX_CONFIG_BACKUPS = 48
DB_BACKUP_INTERVAL = 3600      # Check every hour, but only backup once per day
CONFIG_BACKUP_INTERVAL = 3600  # Hourly


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def get_secondary_dir(area_code):
    """Get secondary backup dir on D: drive if available."""
    secondary = f"D:\\MIS_Backups\\{area_code}"
    if os.path.exists("D:\\"):
        os.makedirs(secondary, exist_ok=True)
        return secondary
    return None


def write_status(status, error=None):
    """Write backup_status.json for health monitoring."""
    data = {
        "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
    }
    if error:
        data["error"] = error
    
    # Read existing to preserve last_backup time
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                existing = json.load(f)
            if "last_backup" in existing:
                data["last_backup"] = existing["last_backup"]
            if "last_config_backup" in existing:
                data["last_config_backup"] = existing["last_config_backup"]
        except Exception:
            pass
    
    if status == "db_ok":
        data["last_backup"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif status == "config_ok":
        data["last_config_backup"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def backup_database(area_code):
    """Copy mis.db to backup dir with timestamp."""
    if not os.path.exists(DB_FILE):
        log.warning("mis.db not found — skipping backup")
        write_status("error", "mis.db not found")
        return False
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"mis_{ts}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    
    try:
        shutil.copy2(DB_FILE, backup_path)
        log.info(f"Database backup: {backup_name} ({os.path.getsize(backup_path)} bytes)")
        
        # Copy to secondary drive too
        secondary_dir = get_secondary_dir(area_code)
        if secondary_dir:
            sec_path = os.path.join(secondary_dir, backup_name)
            shutil.copy2(DB_FILE, sec_path)
            log.info(f"Secondary backup: {sec_path}")
        
        write_status("db_ok")
        return True
        
    except Exception as e:
        log.error(f"Database backup failed: {e}")
        write_status("error", str(e))
        return False


def backup_config(area_code):
    """Copy config.json to backup dir with timestamp."""
    if not os.path.exists(CONFIG_FILE):
        return False
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"config_{ts}.json"
    backup_path = os.path.join(CONFIG_BACKUP_DIR, backup_name)
    
    try:
        shutil.copy2(CONFIG_FILE, backup_path)
        log.info(f"Config backup: {backup_name}")
        
        # Copy to secondary drive too
        secondary_dir = get_secondary_dir(area_code)
        if secondary_dir:
            config_sec = os.path.join(secondary_dir, "config")
            os.makedirs(config_sec, exist_ok=True)
            shutil.copy2(CONFIG_FILE, os.path.join(config_sec, backup_name))
        
        write_status("config_ok")
        return True
        
    except Exception as e:
        log.error(f"Config backup failed: {e}")
        return False


def cleanup_old_backups():
    """Remove old backups beyond retention limits."""
    # Database backups
    db_backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("mis_") and f.endswith(".db")],
        reverse=True
    )
    for old in db_backups[MAX_DB_BACKUPS:]:
        old_path = os.path.join(BACKUP_DIR, old)
        os.remove(old_path)
        log.info(f"Cleaned old backup: {old}")
    
    # Config backups
    cfg_backups = sorted(
        [f for f in os.listdir(CONFIG_BACKUP_DIR) if f.startswith("config_") and f.endswith(".json")],
        reverse=True
    )
    for old in cfg_backups[MAX_CONFIG_BACKUPS:]:
        old_path = os.path.join(CONFIG_BACKUP_DIR, old)
        os.remove(old_path)
        log.info(f"Cleaned old config backup: {old}")


def main():
    log.info("=" * 50)
    log.info("MIS Backup Agent starting")
    
    cfg = load_config()
    area_code = cfg["area"]["code"]
    log.info(f"Area: {area_code} — {cfg['area']['name']}")
    
    secondary = get_secondary_dir(area_code)
    if secondary:
        log.info(f"Secondary backup drive: {secondary}")
    else:
        log.warning("No D: drive found — secondary backups disabled")
    
    last_db_backup_date = None
    last_config_backup_hour = None
    
    log.info("Backup loop started")
    log.info("=" * 50)
    
    while True:
        try:
            now = datetime.now()
            
            # Database backup: once per day at midnight (hour 0)
            today = now.strftime("%Y-%m-%d")
            if now.hour == 0 and last_db_backup_date != today:
                log.info("Running daily database backup...")
                if backup_database(area_code):
                    last_db_backup_date = today
                    cleanup_old_backups()
            
            # Also backup if we've never backed up today (catch missed midnight)
            if last_db_backup_date != today:
                # Check if any backup exists for today
                existing = [f for f in os.listdir(BACKUP_DIR) 
                           if f.startswith(f"mis_{now.strftime('%Y%m%d')}")]
                if not existing:
                    log.info("Missed backup detected — running now...")
                    if backup_database(area_code):
                        last_db_backup_date = today
                        cleanup_old_backups()
            
            # Config backup: hourly
            current_hour = now.strftime("%Y-%m-%d-%H")
            if last_config_backup_hour != current_hour:
                backup_config(area_code)
                last_config_backup_hour = current_hour
            
            # Sleep 60 seconds between checks
            time.sleep(60)
            
        except KeyboardInterrupt:
            log.info("Backup agent stopped by user")
            break
        except Exception as e:
            log.error(f"Backup error: {e}", exc_info=True)
            write_status("error", str(e))
            time.sleep(300)  # Back off 5 min on error
    
    log.info("Backup agent shutdown")


if __name__ == "__main__":
    main()
