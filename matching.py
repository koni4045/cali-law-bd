from datetime import datetime, timezone
import json

from db import get_conn


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _split_terms(value):
    if not value:
        return set()
    return {t.strip().lower() for t in value.replace(";", ",").split(",") if t.strip()}


def match_candidate_to_job(candidate, job):
    """Score a candidate-job pair on title, location, practice area, experience, salary.
    Returns (score 0-100, reasons list)."""
    score = 0
    reasons = []

    role = (candidate["role_type"] or "").lower()
    title = (job["title"] or "").lower()
    if role and (role in title or title in role):
        score += 30
        reasons.append("title match")
    elif role and any(w in title for w in role.split()):
        score += 15
        reasons.append("partial title match")

    loc_pref = _split_terms(candidate["location_preference"]) | _split_terms(candidate["city"]) | _split_terms(candidate["state"])
    job_loc = (job["location"] or "").lower()
    if loc_pref and any(term in job_loc for term in loc_pref):
        score += 25
        reasons.append("location match")
    elif "remote" in job_loc or (job["work_mode"] or "").lower() == "remote":
        score += 15
        reasons.append("remote eligible")

    cand_practice = _split_terms(candidate["practice_areas"])
    job_desc = (job["description"] or "").lower() + " " + title
    if cand_practice and any(p in job_desc for p in cand_practice):
        score += 25
        reasons.append("practice area match")

    years = candidate["years_experience"] or 0
    if years and ("senior" in title or "lead" in title) and years >= 5:
        score += 10
        reasons.append("seniority match")
    elif years and ("junior" in title or "entry" in title) and years <= 2:
        score += 10
        reasons.append("seniority match")
    elif years:
        score += 5

    return min(score, 100), reasons


def run_matching():
    """Match every candidate against every active job, persist matches."""
    inserted = 0
    with get_conn() as conn:
        candidates = conn.execute("SELECT * FROM candidates").fetchall()
        jobs = conn.execute("SELECT * FROM jobs WHERE active = 1").fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for cand in candidates:
            for job in jobs:
                score, reasons = match_candidate_to_job(cand, job)
                if score < 30:
                    continue
                conn.execute(
                    """INSERT INTO matches (candidate_id, job_id, firm_id, match_score, match_reasons, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(candidate_id, job_id) DO UPDATE SET
                         match_score=excluded.match_score, match_reasons=excluded.match_reasons, created_at=excluded.created_at""",
                    (cand["id"], job["id"], job["firm_id"], score, json.dumps(reasons), now),
                )
                inserted += 1
        conn.commit()
    return inserted


def bucket_for_score(score):
    if score >= 80:
        return "Hot"
    if score >= 50:
        return "Warm"
    if score >= 25:
        return "Nurture"
    return "Monitor"


def compute_lead_score(conn, firm_id):
    """Weighted scoring per Module 6:
    active job +40, multiple active jobs +25, job posted <7 days +20,
    candidate match +30, no decision maker -10, job >45 days old -15."""
    components = {}
    score = 0

    jobs = conn.execute("SELECT * FROM jobs WHERE firm_id = ?", (firm_id,)).fetchall()
    active_jobs = [j for j in jobs if j["active"]]

    if active_jobs:
        score += 40
        components["active_job"] = 40

    if len(active_jobs) > 1:
        score += 25
        components["multiple_active_jobs"] = 25

    now = datetime.now(timezone.utc)
    recent = False
    stale = False
    for j in active_jobs:
        posted = _parse_date(j["posted_date"])
        if not posted:
            continue
        age_days = (now - posted).days
        if age_days < 7:
            recent = True
        if age_days > 45:
            stale = True
    if recent:
        score += 20
        components["job_under_7_days"] = 20
    if stale:
        score -= 15
        components["job_over_45_days"] = -15

    match_count = conn.execute(
        "SELECT COUNT(*) c FROM matches WHERE firm_id = ? AND match_score >= 50", (firm_id,)
    ).fetchone()["c"]
    if match_count > 0:
        score += 30
        components["candidate_match"] = 30

    dm_count = conn.execute(
        "SELECT COUNT(*) c FROM decision_makers WHERE firm_id = ?", (firm_id,)
    ).fetchone()["c"]
    if dm_count == 0:
        score -= 10
        components["no_decision_maker"] = -10

    return score, components


def recompute_all_leads():
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        firms = conn.execute("SELECT id FROM firms").fetchall()
        for firm in firms:
            score, components = compute_lead_score(conn, firm["id"])
            bucket = bucket_for_score(score)
            conn.execute(
                """INSERT INTO leads (firm_id, score, bucket, components, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(firm_id) DO UPDATE SET
                     score=excluded.score, bucket=excluded.bucket, components=excluded.components, updated_at=excluded.updated_at""",
                (firm["id"], score, bucket, json.dumps(components), now),
            )
        conn.commit()
    return len(firms)
