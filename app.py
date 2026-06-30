import io
import json
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

import integrations
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
            """SELECT f.name, f.city, l.score, l.bucket FROM leads l
               JOIN firms f ON f.id = l.firm_id ORDER BY l.score DESC LIMIT 25"""
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
        cur = conn.execute("UPDATE email_drafts SET status = 'approved' WHERE id = ?", (draft_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Draft not found")
    return {"draft_id": draft_id, "status": "approved"}


@app.post("/email-drafts/{draft_id}/reject")
def reject_draft(draft_id: int):
    with get_conn() as conn:
        cur = conn.execute("UPDATE email_drafts SET status = 'rejected' WHERE id = ?", (draft_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Draft not found")
    return {"draft_id": draft_id, "status": "rejected"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
