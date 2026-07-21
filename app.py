import io
import json
import os
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

APOLLO_SEQUENCE_ID    = os.getenv("APOLLO_SEQUENCE_ID")
APOLLO_EMAIL_ACCOUNT  = os.getenv("APOLLO_EMAIL_ACCOUNT_ID")

import integrations
import alerts
import matching
import templates_email
from db import get_conn, init_db

app = FastAPI(title="CA Law Firm Recruiting BD Automation - MVP")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------- Dashboard ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_conn() as conn:
        bucket_counts = {row["bucket"]: row["c"] for row in conn.execute(
            "SELECT bucket, COUNT(*) c FROM leads GROUP BY bucket"
        ).fetchall()}
        jobs_by_role = conn.execute(
            "SELECT title, COUNT(*) c FROM jobs WHERE active = 1 GROUP BY title ORDER BY c DESC LIMIT 20"
        ).fetchall()
        dm_count = conn.execute("SELECT COUNT(*) c FROM decision_makers").fetchone()["c"]
        candidate_count = conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]
        matched_candidates = conn.execute(
            "SELECT COUNT(DISTINCT candidate_id) c FROM matches WHERE match_score >= 50"
        ).fetchone()["c"]
        pending_drafts = conn.execute(
            "SELECT COUNT(*) c FROM email_drafts WHERE status = 'pending_approval'"
        ).fetchone()["c"]
        firm_count = conn.execute("SELECT COUNT(*) c FROM firms").fetchone()["c"]
        job_count = conn.execute("SELECT COUNT(*) c FROM jobs WHERE active = 1").fetchone()["c"]
        top_leads = conn.execute(
            """SELECT f.id, f.name, f.city, l.score, l.bucket,
                 (SELECT COUNT(*) FROM jobs j WHERE j.firm_id=f.id AND j.active=1) AS job_count,
                 (SELECT COUNT(*) FROM decision_makers d WHERE d.firm_id=f.id) AS dm_count,
                 (SELECT COUNT(*) FROM decision_makers d WHERE d.firm_id=f.id AND d.email IS NOT NULL) AS dm_with_email,
                 (SELECT COUNT(*) FROM email_drafts e WHERE e.firm_id=f.id AND e.status='pending_approval') AS pending,
                 (SELECT COUNT(*) FROM decision_makers d WHERE d.firm_id=f.id AND d.enrollment_status='enrolled') AS enrolled
               FROM leads l JOIN firms f ON f.id=l.firm_id ORDER BY l.score DESC LIMIT 50"""
        ).fetchall()

    return templates.TemplateResponse(request, "index.html", {
        "bucket_counts": bucket_counts,
        "jobs_by_role": jobs_by_role,
        "dm_count": dm_count,
        "candidate_count": candidate_count,
        "matched_candidates": matched_candidates,
        "pending_drafts": pending_drafts,
        "firm_count": firm_count,
        "job_count": job_count,
        "top_leads": top_leads,
    })


# ---------------- Firms ----------------

@app.post("/firms/seed")
def seed_firms(practice_area: str = Form("law firm"), pages: int = Form(1)):
    created = 0
    with get_conn() as conn:
        for page in range(1, pages + 1):
            data = integrations.apollo_org_search(practice_area_keywords=practice_area, page=page)
            orgs = data.get("organizations") or data.get("accounts") or []
            for org in orgs:
                try:
                    conn.execute(
                        """INSERT INTO firms (name, website, domain, city, attorney_count, linkedin_url, phone, source, last_updated)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'apollo', ?)
                           ON CONFLICT(name, city) DO UPDATE SET
                             website=excluded.website, domain=excluded.domain, attorney_count=excluded.attorney_count,
                             linkedin_url=excluded.linkedin_url, phone=excluded.phone, last_updated=excluded.last_updated""",
                        (
                            org.get("name"),
                            org.get("website_url"),
                            org.get("primary_domain"),
                            org.get("city") or "California",
                            org.get("estimated_num_employees"),
                            org.get("linkedin_url"),
                            org.get("primary_phone", {}).get("number") if isinstance(org.get("primary_phone"), dict) else None,
                            now_iso(),
                        ),
                    )
                    created += 1
                except Exception:
                    continue
        conn.commit()
    return {"firms_upserted": created}


def _bulk_seed_worker(job_id: int, keyword: str, total_pages: int):
    """Background thread: pages through all Apollo results, upserts firms, updates progress."""
    added_total = 0
    with get_conn() as conn:
        conn.execute(
            "UPDATE seed_jobs SET status='running', updated_at=? WHERE id=?",
            (now_iso(), job_id),
        )
        conn.commit()

    for page in range(1, total_pages + 1):
        try:
            data = integrations.apollo_org_search(
                practice_area_keywords=keyword, page=page, per_page=25
            )
            orgs = data.get("organizations") or data.get("accounts") or []
            page_added = 0
            with get_conn() as conn:
                for org in orgs:
                    try:
                        conn.execute(
                            """INSERT INTO firms (name, website, domain, city, attorney_count,
                                 linkedin_url, phone, source, last_updated)
                               VALUES (?, ?, ?, ?, ?, ?, ?, 'apollo', ?)
                               ON CONFLICT(name, city) DO UPDATE SET
                                 website=excluded.website, domain=excluded.domain,
                                 attorney_count=excluded.attorney_count,
                                 linkedin_url=excluded.linkedin_url,
                                 phone=excluded.phone, last_updated=excluded.last_updated""",
                            (
                                org.get("name"),
                                org.get("website_url"),
                                org.get("primary_domain"),
                                org.get("city") or "California",
                                org.get("estimated_num_employees"),
                                org.get("linkedin_url"),
                                org.get("primary_phone", {}).get("number")
                                    if isinstance(org.get("primary_phone"), dict) else None,
                                now_iso(),
                            ),
                        )
                        page_added += 1
                    except Exception:
                        continue
                added_total += page_added
                conn.execute(
                    "UPDATE seed_jobs SET pages_done=?, firms_added=?, updated_at=? WHERE id=?",
                    (page, added_total, now_iso(), job_id),
                )
                conn.commit()
        except Exception:
            time.sleep(5)
            continue
        time.sleep(0.5)  # polite rate limiting

    with get_conn() as conn:
        conn.execute(
            "UPDATE seed_jobs SET status='done', pages_done=?, firms_added=?, updated_at=? WHERE id=?",
            (total_pages, added_total, now_iso(), job_id),
        )
        conn.commit()


@app.post("/firms/bulk-seed")
def bulk_seed_firms(practice_area: str = Form("law firm")):
    """Start a background job that pulls ALL pages from Apollo for the given keyword."""
    # Get total pages first
    data = integrations.apollo_org_search(practice_area_keywords=practice_area, page=1, per_page=25)
    pagination = data.get("pagination", {})
    total_pages = pagination.get("total_pages", 1)
    total_entries = pagination.get("total_entries", 0)

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO seed_jobs (keyword, total_pages, pages_done, firms_added, status, started_at, updated_at) VALUES (?, ?, 0, 0, 'running', ?, ?)",
            (practice_area, total_pages, now_iso(), now_iso()),
        )
        job_id = cur.lastrowid
        conn.commit()

    thread = threading.Thread(
        target=_bulk_seed_worker,
        args=(job_id, practice_area, total_pages),
        daemon=True,
    )
    thread.start()

    return {
        "seed_job_id": job_id,
        "keyword": practice_area,
        "total_entries": total_entries,
        "total_pages": total_pages,
        "status": "running",
        "progress_url": f"/firms/bulk-seed/{job_id}/progress",
    }


@app.get("/firms/bulk-seed/{job_id}/progress")
def bulk_seed_progress(job_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM seed_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Seed job not found")
        r = dict(row)
        r["pct"] = round(r["pages_done"] / r["total_pages"] * 100) if r["total_pages"] else 0
        return r


@app.get("/firms/bulk-seed/latest")
def bulk_seed_latest():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM seed_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"status": "no_jobs"}
        r = dict(row)
        r["pct"] = round(r["pages_done"] / r["total_pages"] * 100) if r["total_pages"] else 0
        return r


@app.get("/firms/view/{firm_id}", response_class=HTMLResponse)
def firm_detail(request: Request, firm_id: int):
    with get_conn() as conn:
        firm = conn.execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
        if not firm:
            raise HTTPException(404, "Firm not found")
        lead = conn.execute("SELECT * FROM leads WHERE firm_id = ?", (firm_id,)).fetchone()
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE firm_id = ? AND active = 1 ORDER BY posted_date DESC", (firm_id,)
        ).fetchall()
        dms = conn.execute(
            "SELECT * FROM decision_makers WHERE firm_id = ? ORDER BY enrollment_status DESC, email DESC", (firm_id,)
        ).fetchall()
        candidates = conn.execute(
            """SELECT c.*, m.match_score, m.match_reasons FROM matches m
               JOIN candidates c ON c.id = m.candidate_id
               WHERE m.firm_id = ? ORDER BY m.match_score DESC LIMIT 10""", (firm_id,)
        ).fetchall()
        drafts = conn.execute(
            "SELECT * FROM email_drafts WHERE firm_id = ? ORDER BY created_at DESC LIMIT 10", (firm_id,)
        ).fetchall()
        all_candidates = conn.execute("SELECT id, name, role_type FROM candidates ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "firm.html", {
        "firm": firm,
        "lead": lead,
        "jobs": jobs,
        "dms": dms,
        "candidates": candidates,
        "drafts": drafts,
        "all_candidates": all_candidates,
    })


@app.get("/drafts", response_class=HTMLResponse)
def drafts_queue(request: Request, status: str = "pending_approval"):
    with get_conn() as conn:
        drafts = conn.execute(
            """SELECT e.*, f.name AS firm_name, f.city,
                 d.name AS dm_name, d.email AS dm_email, d.title AS dm_title,
                 c.name AS candidate_name, c.role_type
               FROM email_drafts e
               LEFT JOIN firms f ON f.id = e.firm_id
               LEFT JOIN decision_makers d ON d.id = e.decision_maker_id
               LEFT JOIN candidates c ON c.id = e.candidate_id
               WHERE e.status = ? ORDER BY e.created_at DESC""", (status,)
        ).fetchall()
        counts = {row["status"]: row["c"] for row in conn.execute(
            "SELECT status, COUNT(*) c FROM email_drafts GROUP BY status"
        ).fetchall()}
    return templates.TemplateResponse(request, "drafts.html", {
        "drafts": drafts,
        "current_status": status,
        "counts": counts,
    })


@app.get("/firms")
def list_firms():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM firms ORDER BY last_updated DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/firms/{firm_id}/enrich")
def enrich_firm(firm_id: int):
    with get_conn() as conn:
        firm = conn.execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
        if not firm:
            raise HTTPException(404, "Firm not found")

        domain = firm["domain"]
        if not domain and firm["website"]:
            domain = firm["website"]
        if not domain:
            domain = integrations.apollo_resolve_domain(firm["name"])
            if domain:
                conn.execute("UPDATE firms SET domain = ? WHERE id = ?", (domain, firm_id))

        # Strip protocol/path — Apollo needs bare hostname only
        if domain:
            from urllib.parse import urlparse
            parsed = urlparse(domain if "://" in domain else "https://" + domain)
            domain = parsed.hostname or domain

        added = 0
        revealed = 0
        if domain:
            data = integrations.apollo_people_search(organization_domain=domain)
            people = data.get("people") or []

            priority_rank = {cat: i for i, cat in enumerate(integrations.DECISION_MAKER_PRIORITY)}

            def rank(p):
                cat = integrations.categorize_role(p.get("title"))
                return priority_rank.get(cat, len(priority_rank))

            people.sort(key=rank)

            for i, p in enumerate(people):
                role_cat = integrations.categorize_role(p.get("title"))
                name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name_obfuscated', p.get('last_name', ''))}".strip()
                email, email_status, linkedin_url = None, "not_enriched", p.get("linkedin_url")

                if i == 0 and p.get("id"):
                    try:
                        matched = integrations.apollo_people_match(p["id"])
                        email = matched.get("email")
                        email_status = matched.get("email_status") or email_status
                        linkedin_url = matched.get("linkedin_url") or linkedin_url
                        revealed += 1
                    except Exception:
                        pass

                try:
                    conn.execute(
                        """INSERT INTO decision_makers (firm_id, name, title, email, email_status, linkedin_url, role_category, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'apollo')
                           ON CONFLICT(firm_id, name, email) DO NOTHING""",
                        (
                            firm_id,
                            name,
                            p.get("title"),
                            email,
                            email_status,
                            linkedin_url,
                            role_cat,
                        ),
                    )
                    added += 1
                except Exception:
                    continue
        conn.commit()
    return {"decision_makers_added": added, "emails_revealed": revealed, "domain_used": domain}


# ---------------- Jobs ----------------

@app.post("/jobs/collect")
def collect_jobs():
    created = 0
    with get_conn() as conn:
        raw_jobs = integrations.jsearch_collect_all_roles()
        for j in raw_jobs:
            employer = j.get("employer_name")
            if not employer:
                continue
            firm = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
            firm_id = firm["id"] if firm else None
            if not firm_id:
                domain = j.get("employer_website")
                cur = conn.execute(
                    """INSERT INTO firms (name, website, domain, city, source, last_updated)
                       VALUES (?, ?, ?, ?, 'jsearch', ?)
                       ON CONFLICT(name, city) DO NOTHING""",
                    (employer, j.get("employer_website"), domain, j.get("job_city") or "California", now_iso()),
                )
                row = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
                firm_id = row["id"] if row else None

            try:
                conn.execute(
                    """INSERT INTO jobs (firm_id, title, firm_name, location, work_mode, description, posted_date, source_url, source, active, last_checked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'jsearch', 1, ?)
                       ON CONFLICT(title, firm_name, source_url) DO UPDATE SET last_checked=excluded.last_checked, active=1""",
                    (
                        firm_id,
                        j.get("job_title"),
                        employer,
                        j.get("job_city"),
                        "remote" if j.get("job_is_remote") else "onsite",
                        j.get("job_description"),
                        j.get("job_posted_at_datetime_utc"),
                        j.get("job_apply_link") or j.get("job_id"),
                        now_iso(),
                    ),
                )
                created += 1
            except Exception:
                continue
        conn.commit()
    return {"jobs_upserted": created}


@app.post("/jobs/collect-law-schools")
def collect_law_school_jobs():
    """Collect jobs from CA law schools via JSearch using law-school-specific roles."""
    created = 0
    with get_conn() as conn:
        raw_jobs = integrations.jsearch_collect_law_school_roles()
        for j in raw_jobs:
            employer = j.get("employer_name")
            if not employer:
                continue
            firm = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
            firm_id = firm["id"] if firm else None
            if not firm_id:
                cur = conn.execute(
                    """INSERT INTO firms (name, website, domain, city, source, last_updated)
                       VALUES (?, ?, ?, ?, 'jsearch_law_school', ?)
                       ON CONFLICT(name, city) DO NOTHING""",
                    (employer, j.get("employer_website"), j.get("employer_website"),
                     j.get("job_city") or "California", now_iso()),
                )
                row = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
                firm_id = row["id"] if row else None
            try:
                conn.execute(
                    """INSERT INTO jobs (firm_id, title, firm_name, location, work_mode, description,
                           posted_date, source_url, source, active, last_checked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'jsearch_law_school', 1, ?)
                       ON CONFLICT(title, firm_name, source_url) DO UPDATE SET
                           last_checked=excluded.last_checked, active=1""",
                    (firm_id, j.get("job_title"), employer, j.get("job_city"),
                     "remote" if j.get("job_is_remote") else "onsite",
                     j.get("job_description"), j.get("job_posted_at_datetime_utc"),
                     j.get("job_apply_link") or j.get("job_id"), now_iso()),
                )
                created += 1
            except Exception:
                continue
        conn.commit()
    return {"jobs_upserted": created}


@app.post("/jobs/collect-linkedin")
def collect_linkedin_jobs(keyword: str = Form(...), location: str = Form("California")):
    url = integrations.build_linkedin_search_url(keyword, location)
    items = integrations.apify_linkedin_jobs([url])
    created = 0
    with get_conn() as conn:
        for item in items:
            employer = item.get("companyName") or item.get("company")
            if not employer:
                continue
            row = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
            firm_id = row["id"] if row else None
            if not firm_id:
                conn.execute(
                    """INSERT INTO firms (name, city, source, last_updated) VALUES (?, ?, 'apify_linkedin', ?)
                       ON CONFLICT(name, city) DO NOTHING""",
                    (employer, location, now_iso()),
                )
                row = conn.execute("SELECT id FROM firms WHERE name = ?", (employer,)).fetchone()
                firm_id = row["id"] if row else None

            try:
                conn.execute(
                    """INSERT INTO jobs (firm_id, title, firm_name, location, work_mode, description, posted_date, source_url, source, active, last_checked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'apify_linkedin', 1, ?)
                       ON CONFLICT(title, firm_name, source_url) DO UPDATE SET last_checked=excluded.last_checked, active=1""",
                    (
                        firm_id,
                        item.get("title"),
                        employer,
                        item.get("location") or location,
                        "remote" if "remote" in (item.get("location") or "").lower() else "onsite",
                        item.get("description"),
                        item.get("postedAt"),
                        item.get("link") or item.get("jobUrl"),
                        now_iso(),
                    ),
                )
                created += 1
            except Exception:
                continue
        conn.commit()
    return {"jobs_upserted": created}


@app.get("/jobs")
def list_jobs():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY last_checked DESC").fetchall()
        return [dict(r) for r in rows]


# ---------------- Candidates ----------------

REQUIRED_CANDIDATE_COLUMNS = [
    "name", "role_type", "state", "city", "location_preference",
    "practice_areas", "years_experience", "skills", "certifications",
    "salary_expectation", "availability", "status",
]


@app.post("/candidates/upload")
async def upload_candidates(file: UploadFile = File(...)):
    raw = await file.read()
    if file.filename.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw))
    else:
        df = pd.read_excel(io.BytesIO(raw))

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = [c for c in REQUIRED_CANDIDATE_COLUMNS if c not in df.columns]
    if missing:
        raise HTTPException(400, f"Missing required columns: {missing}")

    inserted = 0
    with get_conn() as conn:
        for _, row in df.iterrows():
            conn.execute(
                """INSERT INTO candidates (name, role_type, state, city, location_preference, practice_areas,
                     years_experience, skills, certifications, salary_expectation, availability, status, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.get("name"), row.get("role_type"), row.get("state"), row.get("city"),
                    row.get("location_preference"), row.get("practice_areas"),
                    row.get("years_experience"), row.get("skills"), row.get("certifications"),
                    str(row.get("salary_expectation")), row.get("availability"), row.get("status"),
                    now_iso(),
                ),
            )
            inserted += 1
        conn.commit()
    return {"candidates_inserted": inserted}


@app.get("/candidates")
def list_candidates():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM candidates ORDER BY uploaded_at DESC").fetchall()
        return [dict(r) for r in rows]


# ---------------- Matching & Leads ----------------

@app.post("/match")
def run_match():
    count = matching.run_matching()
    return {"matches_upserted": count}


@app.post("/leads/recompute")
def recompute_leads():
    count = matching.recompute_all_leads()
    return {"firms_scored": count}


@app.get("/leads")
def list_leads(bucket: str | None = None):
    with get_conn() as conn:
        if bucket:
            rows = conn.execute(
                """SELECT f.*, l.score, l.bucket, l.components FROM leads l
                   JOIN firms f ON f.id = l.firm_id WHERE l.bucket = ? ORDER BY l.score DESC""",
                (bucket,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT f.*, l.score, l.bucket, l.components FROM leads l
                   JOIN firms f ON f.id = l.firm_id ORDER BY l.score DESC"""
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------- Email Drafts ----------------

@app.post("/email-drafts/generate")
def generate_draft(
    firm_id: int = Form(...),
    template_type: str = Form(...),
    job_id: int | None = Form(None),
    decision_maker_id: int | None = Form(None),
    candidate_id: int | None = Form(None),
):
    with get_conn() as conn:
        firm = conn.execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
        if not firm:
            raise HTTPException(404, "Firm not found")
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone() if job_id else None
        dm = conn.execute("SELECT * FROM decision_makers WHERE id = ?", (decision_maker_id,)).fetchone() if decision_maker_id else None
        candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone() if candidate_id else None

        draft_id = templates_email.build_draft(conn, template_type, firm, job=job, decision_maker=dm, candidate=candidate)
        conn.commit()
    return {"draft_id": draft_id, "status": "pending_approval"}


@app.get("/email-drafts")
def list_drafts(status: str = "pending_approval"):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM email_drafts WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/email-drafts/{draft_id}/approve")
def approve_draft(draft_id: int):
    with get_conn() as conn:
        draft = conn.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not draft:
            raise HTTPException(404, "Draft not found")

        conn.execute("UPDATE email_drafts SET status = 'approved' WHERE id = ?", (draft_id,))

        # Enroll decision maker into Apollo sequence
        enrollment_result = {}
        dm = None
        if draft["decision_maker_id"]:
            dm = conn.execute(
                "SELECT * FROM decision_makers WHERE id = ?", (draft["decision_maker_id"],)
            ).fetchone()

        if dm and dm["email"] and APOLLO_SEQUENCE_ID and APOLLO_EMAIL_ACCOUNT:
            try:
                # Get or create Apollo contact ID
                apollo_contact_id = dm["apollo_contact_id"]
                if not apollo_contact_id:
                    firm = conn.execute("SELECT name FROM firms WHERE id = ?", (dm["firm_id"],)).fetchone()
                    name_parts = (dm["name"] or "").split(" ", 1)
                    contact = integrations.apollo_create_contact(
                        first_name=name_parts[0],
                        last_name=name_parts[1] if len(name_parts) > 1 else "",
                        email=dm["email"],
                        company_name=firm["name"] if firm else "",
                        title=dm["title"],
                    )
                    apollo_contact_id = contact.get("id")
                    if apollo_contact_id:
                        conn.execute(
                            "UPDATE decision_makers SET apollo_contact_id = ? WHERE id = ?",
                            (apollo_contact_id, dm["id"]),
                        )

                if apollo_contact_id:
                    integrations.apollo_enroll_contact(
                        apollo_contact_id, APOLLO_SEQUENCE_ID, APOLLO_EMAIL_ACCOUNT
                    )
                    conn.execute(
                        "UPDATE decision_makers SET enrollment_status = 'enrolled' WHERE id = ?",
                        (dm["id"],),
                    )
                    conn.execute(
                        "UPDATE email_drafts SET status = 'enrolled' WHERE id = ?", (draft_id,)
                    )
                    enrollment_result = {"enrolled": True, "apollo_contact_id": apollo_contact_id}
                else:
                    enrollment_result = {"enrolled": False, "reason": "could not create Apollo contact"}

            except Exception as e:
                enrollment_result = {"enrolled": False, "reason": str(e)}
        else:
            reason = "no decision maker email" if not (dm and dm["email"]) else "sequence not configured"
            enrollment_result = {"enrolled": False, "reason": reason}

        conn.commit()

    final_status = "enrolled" if enrollment_result.get("enrolled") else "approved"
    return {"draft_id": draft_id, "status": final_status, "enrollment": enrollment_result}


@app.post("/email-drafts/{draft_id}/reject")
def reject_draft(draft_id: int):
    with get_conn() as conn:
        cur = conn.execute("UPDATE email_drafts SET status = 'rejected' WHERE id = ?", (draft_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Draft not found")
    return {"draft_id": draft_id, "status": "rejected"}


# ── Alert scheduler ──────────────────────────────────────────────────────────

@app.post("/alerts/start")
def start_alerts():
    return alerts.start_scheduler()


@app.post("/alerts/stop")
def stop_alerts():
    return alerts.stop_scheduler()


@app.get("/alerts/status")
def alert_status():
    return alerts.scheduler_status()


@app.get("/alerts/log")
def alert_log():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_log ORDER BY sent_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
