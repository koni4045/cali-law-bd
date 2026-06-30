# CA Law Firm Recruiting / BD Automation — Phase 1 MVP

California-only. Manual approval required before any email send (no auto-send in this phase).

## Setup

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000 for the dashboard.

## Out of scope for this phase

Hidden hiring signal monitoring, auto follow-up sequences, LinkedIn growth tracking,
career-page change detection, reply classification, meeting booking, multi-state
expansion, revenue forecasting, predictive modeling. Also out of scope: scraping
Martindale, Chambers, Legal 500, Best Lawyers, Super Lawyers, NALP, or state/county
bar directories — these lack APIs or carry ToS/legal risk. Firm data is seeded from
Apollo's organization search only.

## Workflow

1. `POST /firms/seed` — seed CA firms from Apollo (form: `practice_area`, `pages`)
2. `POST /jobs/collect` — pull jobs from JSearch for target roles
3. `POST /jobs/collect-linkedin` — pull jobs from Apify LinkedIn actor (form: `keyword`, `location`)
4. `POST /firms/{id}/enrich` — find decision makers via Apollo people search
5. `POST /candidates/upload` — upload CSV/XLSX (multipart `file`)
6. `POST /match` — match candidates to active jobs
7. `POST /leads/recompute` — score and bucket firms (Hot/Warm/Nurture/Monitor)
8. `POST /email-drafts/generate` — generate a draft (form: `firm_id`, `template_type` = job_found|hidden_signal|candidate_first, optional `job_id`, `decision_maker_id`, `candidate_id`)
9. `GET /email-drafts` — review pending drafts
10. `POST /email-drafts/{id}/approve` — approve for manual sending elsewhere

## Candidate spreadsheet columns

`name, role_type, state, city, location_preference, practice_areas, years_experience, skills, certifications, salary_expectation, availability, status`
