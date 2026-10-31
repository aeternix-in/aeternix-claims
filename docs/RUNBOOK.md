# Runbook

## What runs

| Process | Command | Job |
| --- | --- | --- |
| API | `uvicorn app.main:app` | HTTP API, the web app, and (when `DEMO_MODE=true`) the mock portal |
| Worker | `python -m app.worker` | Every 60 seconds: fires claim windows that have passed, drafts escalation letters from the ageing rules, sends each hospital's digest once a day after `DIGEST_HOUR` (IST) |
| Postgres 16 | | Everything except files |
| Files | `DATA_DIR` | Claim documents (encrypted if `FILE_ENCRYPTION_KEY` is set) and uploaded rate cards |

Only one worker should run. Digest sending is idempotent per hospital per day (a row in `job`), but
letter drafting and window firing are not designed for two workers racing.

## Onboarding a hospital (first week)

1. `python -m app.manage create-hospital --name ... --code ... --schemes cghs,pmjay,echs` creates the hospital
   and one payer per scheme.
2. `python -m app.manage create-user ...` for the hospital admin; the admin adds the rest in Setup.
3. Setup, rate cards: upload the official list per scheme (CSV with `code, description, rate_inr`, optional
   `los_days, category`) with the date it takes effect. Until then the claim check compares against SAMPLE
   rates and says so on every result.
4. Setup, payers: check each payer's bank narration keywords against two or three real credits on the
   hospital's statement; this is what the amount-and-date matcher relies on.
5. Setup, import: load the hospital's current outstanding list (spreadsheet; columns matched by name).
   Imported cases are marked so the pilot measures leave them out.
6. Import the last 90 days of bank statements (Bank, *Add a statement*) and work through the matches.
7. Install the extension on the desk PCs. Real portals stay inactive until captured (ADAPTERS.md).

## Daily

- The desk works the Worklist top-down. Anything red in *Overdue or due now* is money at risk today.
- The accountant imports the bank statement and confirms matches. A credit confirmed against a claim is
  what closes the arc; nothing else does.
- The clinician clears the review queue the same day.
- The owner reads the digest. It carries case numbers and amounts only: never names or diagnoses.

## Updating a rule pack

Rule packs (`app/rulepacks/*.yaml`) hold scheme deadlines, required documents, condonation slabs and
interest rules, each with the circular it comes from.

1. Edit the YAML, cite the new circular in `source`, and bump `version`.
2. Run `make test`.
3. Deploy and restart the API and worker.

New cases record the version they were opened under. Clocks already running keep their due dates; if a
circular shortens a live deadline, review open cases by hand.

## Backups

- Database: `pg_dump -Fc` nightly, kept 30 days, one copy outside the server.
- Files: back up `DATA_DIR` (documents and rate cards) on the same schedule.
- **`FILE_ENCRYPTION_KEY` must be backed up separately from the files.** Without it every stored document
  is unreadable; with it next to the files, the encryption protects nothing.
- Restore drill once a quarter: restore both into a scratch environment and open a few documents.

## When something breaks

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Extension panel: *Not found on this page* | The portal changed its page | Update that portal's selector YAML, restart the API, *Refresh from Aeternix* in the extension |
| Extension: *Could not reach Aeternix* | Wrong address in extension settings, or API down | Check the address; check the API |
| Bank credits stay unmatched for one payer | Narration keywords out of date, or the payer pays in batches | Update keywords in Setup; use manual match for batches |
| No digest | Worker not running, or digest recipients empty | Check the worker log; Setup, hospital, digest recipients |
| Documents fail to read | Scanned at low resolution, or tesseract missing | Re-scan at 300 dpi; check `tesseract --version` on the server |
| Claim check flags every bill line | Rate card for the wrong city or tier | Upload the right one; the newest effective card wins |
