"""Email draft generation. All drafts are stored with status='pending_approval' —
nothing in this module sends mail; sending is a manual, separate step (Phase 1 requirement)."""

from datetime import datetime, timezone


def generate_job_found(firm, job, decision_maker, candidate=None):
    dm_name = decision_maker["name"] if decision_maker else "Hiring Team"
    subject = f"Re: {job['title']} opening at {firm['name']}"
    body = (
        f"Hi {dm_name.split()[0] if dm_name != 'Hiring Team' else 'there'},\n\n"
        f"I noticed {firm['name']} has an open {job['title']} role"
        f"{' in ' + job['location'] if job['location'] else ''}. "
        f"I work with experienced legal professionals in California and wanted to reach out directly "
        f"in case you're still building out the search.\n\n"
        + (
            f"I have a strong candidate, {candidate['name']}, with {candidate['years_experience'] or 'relevant'} "
            f"years of experience in {candidate['practice_areas'] or 'this practice area'} who could be a great fit.\n\n"
            if candidate else
            "I'd welcome a quick call to learn more about what you're looking for.\n\n"
        )
        + "Would you be open to a brief conversation this week?\n\nBest,\n"
    )
    return subject, body


def generate_hidden_signal(firm, decision_maker, candidate=None):
    dm_name = decision_maker["name"] if decision_maker else "Hiring Team"
    subject = f"Quick note for {firm['name']}"
    body = (
        f"Hi {dm_name.split()[0] if dm_name != 'Hiring Team' else 'there'},\n\n"
        f"I work with legal professionals across California and wanted to check in with {firm['name']} directly — "
        f"even without a posted opening, firms at your stage often have near-term hiring needs that aren't public yet.\n\n"
        + (
            f"I currently have {candidate['name']}, a {candidate['role_type']} with {candidate['years_experience'] or 'solid'} "
            f"years of experience, who is actively exploring new opportunities.\n\n"
            if candidate else ""
        )
        + "Happy to share more if it's useful — no pressure either way.\n\nBest,\n"
    )
    return subject, body


def generate_candidate_first(firm, candidate, decision_maker=None):
    dm_name = decision_maker["name"] if decision_maker else "Hiring Team"
    subject = f"Candidate introduction: {candidate['role_type']} for {firm['name']}"
    body = (
        f"Hi {dm_name.split()[0] if dm_name != 'Hiring Team' else 'there'},\n\n"
        f"I wanted to introduce {candidate['name']}, a {candidate['role_type']} with "
        f"{candidate['years_experience'] or 'relevant'} years of experience"
        f"{' in ' + candidate['practice_areas'] if candidate['practice_areas'] else ''}, currently exploring "
        f"opportunities {('in ' + candidate['location_preference']) if candidate['location_preference'] else 'in California'}.\n\n"
        f"Given {firm['name']}'s focus, I thought there might be a fit even if you're not actively posting. "
        f"Let me know if you'd like their background.\n\nBest,\n"
    )
    return subject, body


TEMPLATE_BUILDERS = {
    "job_found": generate_job_found,
    "hidden_signal": generate_hidden_signal,
    "candidate_first": generate_candidate_first,
}


def build_draft(conn, template_type, firm, job=None, decision_maker=None, candidate=None):
    builder = TEMPLATE_BUILDERS.get(template_type)
    if not builder:
        raise ValueError(f"Unknown template_type: {template_type}")

    if template_type == "job_found":
        subject, body = builder(firm, job, decision_maker, candidate)
    elif template_type == "hidden_signal":
        subject, body = builder(firm, decision_maker, candidate)
    elif template_type == "candidate_first":
        subject, body = builder(firm, candidate, decision_maker)
    else:
        raise ValueError(template_type)

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO email_drafts (firm_id, job_id, decision_maker_id, candidate_id, template_type, subject, body, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?)""",
        (
            firm["id"],
            job["id"] if job else None,
            decision_maker["id"] if decision_maker else None,
            candidate["id"] if candidate else None,
            template_type,
            subject,
            body,
            now,
        ),
    )
    return cur.lastrowid
