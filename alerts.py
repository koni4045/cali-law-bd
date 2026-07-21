import os
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client

from db import get_conn
from integrations import jsearch_collect_all_roles, jsearch_collect_law_school_roles

log = logging.getLogger("alerts")

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM  = os.getenv("TWILIO_FROM")
ALERT_PHONE  = os.getenv("ALERT_PHONE")

_scheduler = BackgroundScheduler(timezone="America/Los_Angeles")
_scheduler_started = False


def send_sms(body: str, job_title: str = "", firm_name: str = "", location: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(to=ALERT_PHONE, from_=TWILIO_FROM, body=body)
        log.info("SMS sent: %s", body[:60])
        status = "sent"
    except Exception as e:
        log.error("SMS failed: %s", e)
        status = f"failed: {e}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alert_log (job_title, firm_name, location, message, sent_at) VALUES (?,?,?,?,?)",
            (job_title, firm_name, location, f"[{status}] {body}", now)
        )
        conn.commit()


def _collect_and_alert():
    log.info("Job alert check started — %s", datetime.now(timezone.utc).isoformat())
    try:
        raw_jobs = jsearch_collect_all_roles() + jsearch_collect_law_school_roles()
    except Exception as e:
        log.error("Job collection failed: %s", e)
        return

    new_jobs = []
    with get_conn() as conn:
        for j in raw_jobs:
            employer = j.get("employer_name") or ""
            title    = j.get("job_title") or ""
            location = j.get("job_city") or j.get("job_state") or ""
            url      = j.get("job_apply_link") or j.get("job_google_link") or ""
            posted   = j.get("job_posted_at_datetime_utc") or datetime.now(timezone.utc).isoformat()

            if not title or not employer:
                continue

            existing = conn.execute(
                "SELECT id FROM jobs WHERE title=? AND firm_name=?",
                (title, employer)
            ).fetchone()

            if existing:
                continue  # already in DB — not new

            # It's new — insert it
            firm = conn.execute("SELECT id FROM firms WHERE name=?", (employer,)).fetchone()
            firm_id = firm["id"] if firm else None

            conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (firm_id, title, firm_name, location, source, source_url, posted_date, active, last_checked)
                   VALUES (?, ?, ?, ?, 'jsearch', ?, ?, 1, ?)""",
                (firm_id, title, employer, location, url, posted,
                 datetime.now(timezone.utc).isoformat())
            )
            new_jobs.append({"title": title, "firm": employer, "location": location})

        conn.commit()

    log.info("New jobs found: %d", len(new_jobs))

    for job in new_jobs:
        loc = job["location"] or "CA"
        msg = (
            f"New CA Law Job Alert\n"
            f"{job['title']} @ {job['firm']}\n"
            f"Location: {loc}\n"
            f"Check: http://127.0.0.1:8000"
        )
        send_sms(msg, job_title=job["title"], firm_name=job["firm"], location=loc)


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return {"status": "already_running"}
    _scheduler.add_job(
        _collect_and_alert,
        trigger="interval",
        minutes=15,
        id="job_alert",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run immediately on start
    )
    _scheduler.start()
    _scheduler_started = True
    log.info("Alert scheduler started — every 15 minutes")
    return {"status": "started"}


def stop_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        return {"status": "not_running"}
    _scheduler.pause()
    _scheduler_started = False
    log.info("Alert scheduler stopped")
    return {"status": "stopped"}


def scheduler_status():
    if not _scheduler_started:
        return {"running": False, "next_run": None}
    jobs = _scheduler.get_jobs()
    next_run = None
    if jobs:
        nr = jobs[0].next_run_time
        next_run = nr.strftime("%Y-%m-%d %H:%M:%S %Z") if nr else None
    return {"running": True, "next_run": next_run}
