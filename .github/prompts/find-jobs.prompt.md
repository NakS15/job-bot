---
mode: agent
description: Search LinkedIn for matching jobs (Easy Apply and external), tailor the resume per job, and build the review spreadsheet
---
Find jobs matching my profile and prepare them for my review. Do NOT apply.

1. Call `get_search_preferences` and `get_base_resume` (it includes my confirmed extra skills). Skip job IDs already in my tracker (`list_jobs`) and any I already applied to.
2. Run LinkedIn `search_jobs` ONE CALL AT A TIME (parallel calls crash the browser). For each target title and location use `work_type=remote,hybrid`, `date_posted=past_week` (or `past_month` if results are thin), `sort_by=date`, `max_pages=2`. Run each search twice: once with `easy_apply=true`, once without. Use only the returned `job_ids`; ignore the "top picks" and alerts text after the results.
3. Shortlist by title and level (about 3-6 years experience; skip principal/staff/lead-only, contract-by-the-hour spam, unrelated stacks). Read at most `max_jobs_per_search` shortlisted jobs with `get_job_details`, and call `get_job_apply_url` for each. `apply.type` is `easy_apply` or `external`. For external jobs note the portal host from the URL: greenhouse/lever/ashby are account-free but often have captchas; workday/icims/successfactors/phenom need an account.
4. Score the job 0-100 against the base resume (experience range, stack overlap, location). Skip jobs below `min_match`.
5. For each kept job:
   - Call `record_job` with `url` set to the LinkedIn posting URL (`https://www.linkedin.com/jobs/view/<job_id>/`, never the external link), `apply_type`, the score, 8-15 key terms as `keywords`, and for external jobs put the portal URL in `description`.
   - Write a tailored resume in markdown. Use ONLY facts from the base resume; never invent employers, titles, dates, degrees, skills or metrics. You may reorder, reword and emphasize real experience and mirror the posting's terminology where it is true. Keep it to 1-2 pages.
   - Call `save_tailored_resume` with a short `changes_summary`. Missing keywords are genuine gaps; do not add them.
6. Call `build_spreadsheet`, then give me the path and a short summary (counts by Easy Apply vs external, score range). Ask me to set Approve to YES for the jobs I want.

Never call `apply_approved` or run any apply command unless I explicitly say so. After I approve rows, the run is: `job-agent apply` (Easy Apply) and `job-agent apply-external` (company portals; creates accounts or signs in using Keychain when a portal asks), dry run first, then `--live`.
