import os
import time
import requests

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
JSEARCH_API_KEY = os.getenv("JSEARCH_API_KEY")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

APOLLO_BASE = "https://api.apollo.io/v1"
DECISION_MAKER_PRIORITY = [
    "Hiring", "Recruiting", "HR", "Human Resources",
    "Managing Partner",
    "Office Administrator", "Firm Administrator",
    "Practice Group Leader",
]


def _api(method, url, *, headers=None, json=None, params=None, max_retries=4, timeout=30):
    """Generic HTTP wrapper with retry/backoff on 429s and transient 5xx errors."""
    backoff = 2
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, json=json, params=params, timeout=timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries - 1:
                resp.raise_for_status()
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Exhausted retries calling {url}")


# ---------------- Apollo ----------------

def apollo_org_search(practice_area_keywords=None, page=1, per_page=25):
    """Search CA law firms via Apollo organization search.

    Apollo's mixed_companies/search ignores free-text q_organization_keywords;
    it only filters on q_organization_keyword_tags (curated industry/keyword tags).
    """
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    tags = [practice_area_keywords] if practice_area_keywords else ["law firm"]
    body = {
        "q_organization_keyword_tags": tags,
        "organization_locations": ["California, US"],
        "page": page,
        "per_page": per_page,
    }
    return _api("POST", f"{APOLLO_BASE}/mixed_companies/search", headers=headers, json=body)


def apollo_resolve_domain(company_name):
    """Resolve a company name to a domain when a job source lacks a website."""
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    body = {"q_organization_name": company_name, "page": 1, "per_page": 1}
    data = _api("POST", f"{APOLLO_BASE}/mixed_companies/search", headers=headers, json=body)
    orgs = data.get("organizations") or data.get("accounts") or []
    if orgs:
        return orgs[0].get("primary_domain") or orgs[0].get("website_url")
    return None


def apollo_people_search(organization_domain=None, organization_id=None, page=1, per_page=10):
    """Find people at a firm. No title filter — Apollo's person_titles filter is too strict
    and returns 0 results for most firms. We sort by priority locally after fetching."""
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    body = {"page": page, "per_page": per_page}
    if organization_domain:
        body["q_organization_domains_list"] = [organization_domain]
    if organization_id:
        body["organization_ids"] = [organization_id]
    return _api("POST", "https://api.apollo.io/api/v1/mixed_people/api_search", headers=headers, json=body)


def apollo_people_match(person_id):
    """Reveal verified email/phone for a person found via apollo_people_search. Consumes Apollo credits."""
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    body = {"id": person_id}
    data = _api("POST", "https://api.apollo.io/api/v1/people/match", headers=headers, json=body)
    return data.get("person", {})


def categorize_role(title):
    if not title:
        return None
    t = title.lower()
    if any(k in t for k in ("hiring", "recruit", "hr", "human resources")):
        return "Hiring/Recruiting/HR"
    if "managing partner" in t:
        return "Managing Partner"
    if "administrator" in t:
        return "Office/Firm Administrator"
    if "practice group" in t:
        return "Practice Group Leader"
    return "Other"


# ---------------- JSearch (RapidAPI) ----------------

TARGET_ROLES = [
    "Paralegal", "Legal Assistant", "Legal Secretary", "Litigation Paralegal",
    "Immigration Paralegal", "Family Law Paralegal", "Case Manager",
    "Intake Specialist", "Attorney", "Associate Attorney",
    "Legal Billing Specialist", "Office Administrator", "Firm Administrator",
]

LAW_SCHOOL_ROLES = [
    "Paralegal law school", "Legal Assistant university",
    "Clinical Program Coordinator law school", "Law School Administrator",
    "Legal Research Assistant university", "Student Services Coordinator law school",
    "Law Library Assistant", "Legal Clinic Coordinator",
]


def jsearch_search(query, location="California, USA", page=1, num_pages=1):
    headers = {
        "X-RapidAPI-Key": JSEARCH_API_KEY,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    params = {"query": f"{query} in {location}", "page": str(page), "num_pages": str(num_pages)}
    return _api("GET", "https://jsearch.p.rapidapi.com/search", headers=headers, params=params)


def jsearch_collect_all_roles(location="California, USA"):
    results = []
    for role in TARGET_ROLES:
        try:
            data = jsearch_search(role, location=location)
            results.extend(data.get("data", []))
        except requests.HTTPError:
            continue
        time.sleep(1)
    return results


def jsearch_collect_law_school_roles(location="California, USA"):
    results = []
    for role in LAW_SCHOOL_ROLES:
        try:
            data = jsearch_search(role, location=location)
            results.extend(data.get("data", []))
        except requests.HTTPError:
            continue
        time.sleep(1)
    return results


# ---------------- Apify (LinkedIn Jobs) ----------------

APIFY_ACTOR = "curious_coder~linkedin-jobs-scraper"


def apify_linkedin_jobs(search_urls, count=10):
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
    params = {"token": APIFY_API_TOKEN}
    body = {"urls": search_urls, "count": max(count, 10)}
    return _api("POST", url, params=params, json=body, timeout=120)


def apollo_enroll_contact(contact_id, sequence_id, email_account_id):
    """Enroll an Apollo contact into a sequence. Returns campaign status dict."""
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    data = _api(
        "POST",
        f"https://api.apollo.io/api/v1/emailer_campaigns/{sequence_id}/add_contact_ids",
        headers=headers,
        json={
            "emailer_campaign_id": sequence_id,
            "contact_ids": [contact_id],
            "send_email_from_email_account_id": email_account_id,
        },
    )
    contacts = data.get("contacts", [])
    return contacts[0] if contacts else {}


def apollo_create_contact(first_name, last_name, email, company_name, title=None):
    """Create a contact in Apollo CRM. Returns the contact dict including its id."""
    headers = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
    body = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "organization_name": company_name,
    }
    if title:
        body["title"] = title
    data = _api("POST", f"{APOLLO_BASE}/contacts", headers=headers, json=body)
    return data.get("contact", {})


def build_linkedin_search_url(keyword, location="California"):
    from urllib.parse import quote
    return f"https://www.linkedin.com/jobs/search/?keywords={quote(keyword)}&location={quote(location)}"
