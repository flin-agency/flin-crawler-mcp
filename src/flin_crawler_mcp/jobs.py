"""Launch the same detached crawler for manual and scheduled requests."""

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from contextlib import closing

from . import storage


def spawn(module, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")))
    )
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", module, *args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def launch(crawl_id):
    with closing(storage.connect(crawl_id)) as db, db:
        try:
            pid = spawn("flin_crawler_mcp.worker", crawl_id)
        except OSError as exc:
            db.execute(
                "UPDATE job SET state='failed',message=?,finished=? WHERE state='queued'",
                (str(exc), time.time()),
            )
            db.commit()
            raise
        db.execute("UPDATE job SET pid=? WHERE state='queued' AND pid IS NULL", (pid,))
