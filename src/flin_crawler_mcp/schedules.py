"""Local daily/weekly crawl schedules. A generation token elects one background dispatcher."""

from contextlib import closing
from datetime import datetime, timedelta, time as clock
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from . import jobs, storage


def connect():
    root = storage.root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(root / "schedules.sqlite", timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE IF NOT EXISTS dispatcher(id INTEGER PRIMARY KEY,generation TEXT);
    CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, template TEXT, config TEXT, cadence TEXT, local_time TEXT,
    zone TEXT, weekday INTEGER, notify INTEGER, enabled INTEGER, due REAL, current TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, schedule_id TEXT,crawl_id TEXT UNIQUE, finished REAL, message TEXT);""")
    return db


def next_due(now, cadence, local_time, zone, weekday):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise ValueError("Use a 24-hour time such as 08:30")
    tz = ZoneInfo(zone)
    today = datetime.fromtimestamp(now, tz).date()
    for n in range(8):
        date = today + timedelta(days=n)
        if cadence == "weekly" and date.weekday() != weekday:
            continue
        at = datetime.combine(
            date, clock.fromisoformat(local_time), tzinfo=tz
        ).timestamp()
        if at > now:
            return at
    raise ValueError("No matching weekday")


def wake():
    generation = uuid.uuid4().hex
    with closing(connect()) as db, db:
        db.execute("INSERT OR REPLACE INTO dispatcher VALUES(1,?)", (generation,))
        jobs.spawn("flin_crawler_mcp.schedules", generation)


def create(template, cadence, local_time, zone, weekday, notify):
    due = next_due(time.time(), cadence, local_time, zone, weekday)
    with closing(storage.connect(template)) as crawl:
        config = crawl.execute("SELECT config FROM job").fetchone()[0]
    schedule_id = str(uuid.uuid4())
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,1,?,NULL)",
            (
                schedule_id,
                template,
                config,
                cadence,
                local_time,
                zone,
                weekday,
                notify,
                due,
            ),
        )
    wake()
    return {
        "schedule_id": schedule_id,
        "next_run": datetime.fromtimestamp(due, ZoneInfo(zone)).isoformat(),
    }


def toggle(schedule_id, enabled):
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT * FROM schedules WHERE id=?", (schedule_id,)
        ).fetchone()
        if not row:
            raise ValueError("Unknown schedule")
        due = next_due(
            time.time(), row["cadence"], row["local_time"], row["zone"], row["weekday"]
        )
        db.execute(
            "UPDATE schedules SET enabled=?,due=? WHERE id=?",
            (enabled, due, schedule_id),
        )
    if enabled:
        wake()
    return {"schedule_id": schedule_id, "enabled": enabled}


def overview(limit):
    if not (storage.root() / "schedules.sqlite").exists():
        return {"schedules": [], "events": []}
    with closing(connect()) as db:
        items = [
            dict(r)
            for r in db.execute(
                "SELECT id,template,cadence,local_time,zone,weekday,notify,enabled,due,current FROM schedules ORDER BY enabled DESC,due,id LIMIT ?",
                (limit,),
            )
        ]
        for item in items:
            item["next_run"] = (
                datetime.fromtimestamp(
                    item.pop("due"), ZoneInfo(item["zone"])
                ).isoformat()
                if item["enabled"]
                else None
            )
        return {
            "schedules": items,
            "events": [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                )
            ],
        }


def desktop_notice(message):
    try:
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'on run argv\ndisplay notification (item 1 of argv) with title "flin SEO Crawler"\nend run',
                    message,
                ],
                timeout=6,
                capture_output=True,
            )
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "flin SEO Crawler", message],
                timeout=6,
                capture_output=True,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


def tick(generation, now):
    if not (storage.root() / "schedules.sqlite").is_file():
        return False
    notices = []
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        leader = db.execute("SELECT generation FROM dispatcher WHERE id=1").fetchone()
        if (
            not leader
            or leader[0] != generation
            or not db.execute(
                "SELECT 1 FROM schedules WHERE enabled=1 OR current IS NOT NULL LIMIT 1"
            ).fetchone()
        ):
            return False
        for row in db.execute(
            "SELECT * FROM schedules WHERE current IS NOT NULL OR (enabled=1 AND due<=?)",
            (now,),
        ).fetchall():
            cid = row["current"]
            if cid:
                folder = storage.directory(cid)
                # The run ID was durably reserved during a previous tick.
                if not (folder / "crawl.sqlite").exists():
                    storage.create(json.loads(row["config"]), cid)
                with closing(storage.connect(cid)) as crawl:
                    state = storage.job_status(crawl)
                    if (
                        state["state"] == "queued"
                        and not crawl.execute("SELECT pid FROM job").fetchone()[0]
                    ):
                        jobs.launch(cid)
                        continue
                    if state["state"] in ("queued", "running", "stopping"):
                        continue
                    high = crawl.execute(
                        "SELECT count(DISTINCT url) FROM issues WHERE severity='high'"
                    ).fetchone()[0]
                message = f"{state['config']['urls'][0]}: {state['state']}, {state['pages_checked']} URLs checked, {high} URLs with high-priority issues."
                inserted = db.execute(
                    "INSERT OR IGNORE INTO events(schedule_id,crawl_id,finished,message) VALUES(?,?,?,?)",
                    (row["id"], cid, now, message),
                ).rowcount
                db.execute("UPDATE schedules SET current=NULL WHERE id=?", (row["id"],))
                if row["notify"] and inserted:
                    notices.append(message)
            elif row["enabled"] and row["due"] <= now:
                cid = str(uuid.uuid4())
                due = next_due(
                    now, row["cadence"], row["local_time"], row["zone"], row["weekday"]
                )
                db.execute(
                    "UPDATE schedules SET current=?,due=? WHERE id=?",
                    (cid, due, row["id"]),
                )
        db.commit()
    for message in notices:
        desktop_notice(message)
    return True


def run(generation):
    while True:
        try:
            if not tick(generation, time.time()):
                return
        except (OSError, sqlite3.OperationalError):
            pass
        time.sleep(15)


if __name__ == "__main__":
    run(sys.argv[1])
