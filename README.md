# Aeternix Claims-to-Cash, V1

A ledger for every government-scheme claim a hospital files, from the counter to the bank credit:
**case → claim pack → query → payer → bank.** It checks the claim before it goes, keeps every deadline
on a clock, drafts query replies and escalation letters, reads payer portals through a browser
extension, and matches bank credits back to claims so the owner sees what is owed and where it is stuck.
It is built for CGHS, PM-JAY, ECHS, DGEHS and department panels first, with insurers/TPAs as a template.

This repository is the V1 described in *Aeternix Claims-to-Cash — Product Architecture v1*.

## What is real and what is not

| Part | State |
| --- | --- |
| Claim state machine (18 states, guarded moves, full audit trail) | Built and tested |
| Clocks for every scheme deadline, with the source circular shown | Built. Deadlines cite their circulars; **re-check each against the current circular before go-live** |
| Claim check: rules, clinical fit, clinician gate (red blocks, amber needs a note) | Built |
| Document intake: PDF text, local OCR (tesseract), type sorting, field extraction, human confirmation | Built. Accuracy on a real hospital's scans is **not measured yet** |
| Query desk: classify, draft from a reply library, clinician sign-off, reply clocks | Built |
| Bank reconciliation: reference, amount + date, one credit for many claims, manual | Built; tested on a synthetic statement only |
| Escalation letters (grievance, PM-JAY interest, condonation, reminder, dispute, appeal) | Built; drafts only, a person sends them |
| Owner view, worklist, payer scorecard, pilot measures, 8 am digest | Built |
| Chrome extension: reads claim lists, fills claim forms, records the portal reference | Built and tested **against the mock portal only** |
| Real portal selectors (NHA TMS 2.0, UTI-ITSL ECHS, TPA portals) | **Not captured.** They can only be read from a signed-in desk session. See [docs/ADAPTERS.md](docs/ADAPTERS.md) |
| CGHS and PM-JAY rate cards | **SAMPLE files only.** Upload the official lists per hospital in Setup |
| OCR vendor, LLM, WhatsApp keys | Pluggable, **off by default**. Everything runs without them |
| FHIR R4 export of a case (Coverage, Claim, ClaimResponse, PaymentNotice, PaymentReconciliation) | Built; **not validated against NHCX profiles** |

All demo data is synthetic. Every generated document says *SYNTHETIC RECORD - NOT A REAL PATIENT*.

## Run the demo (laptop, no accounts needed)

Needs **Python 3.11, 3.12 or 3.13** and **Chrome**. Node is not needed: the web app comes already built in
`web/dist`. Tesseract (OCR) is optional: with it, the photographed documents (referral letters, an implant sticker) are read by OCR;
without it, the demo confirms them by hand and everything else is the same
(`brew install tesseract` / `sudo apt install tesseract-ocr`).

Mac or Linux:

```sh
cd aeternix-claims
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
python -m app.seed --reset
python -m uvicorn app.main:app --port 8000
```

Windows (PowerShell):

```powershell
cd aeternix-claims
py -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass    # only if the next line is blocked
.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
cd backend
python -m app.seed --reset
python -m uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**. To stop, press Ctrl+C. Next time, activate the environment and run only the
last two lines. Run the seed again on the morning of a demo: it dates everything from today, so the
clocks read right. (Developers on Mac or Linux can use `make demo` instead, which also rebuilds the web app.)

Sign in with any of these (password `demo-password-123`):

| Who | Email | Sees |
| --- | --- | --- |
| Insurance desk | desk@demo.local | Worklist, cases, letters |
| Owner | owner@demo.local | Owner view plus everything the desk sees |
| Accountant | accounts@demo.local | Bank matching |
| Hospital admin | admin@demo.local | Setup: payers, users, rate cards, imports |
| Aeternix clinician | clinician@aeternix.local | Clinical review queue |
| Aeternix ops | ops@aeternix.local | Any hospital |

### A five-minute walk-through

1. **Owner view** (owner): money owed, split by why it is stuck, by payer and by age. The striped part is approved but unpaid: it waits on the payer.
2. **Worklist** (desk): what to move today, most urgent first.
3. **DEMO-2610-0005**: a claim pack with five planted defects (missing implant invoice and sticker, a date mismatch, a rate above the card, an unsigned summary). The check is red and *Mark ready to submit* stays locked.
4. **DEMO-2610-0004**: the clean pack. Green, so it can be marked ready and filed through the extension.
5. **DEMO-2610-0006**: the patient's name differs between the card and the summary (red), and the diagnosis code does not fit the procedure (amber: only a clinician can clear it; sign in as the clinician). **DEMO-2610-0007**: a PM-JAY pack whose documents nobody has confirmed yet. **DEMO-2610-0008**: an ECHS pack waiting on an amber flag.
6. **Bank** (accountant): five credits from this week's statement: matched by reference, by amount and date, one PFMS credit split across three claims, and a cash deposit that matches nothing.
7. **Letters**: escalation drafts the ageing rules produced, including PM-JAY interest at 1% a week.
8. **The extension on the mock portal**: below.

### The extension, on the mock portal

1. In Chrome, open `chrome://extensions`, switch on *Developer mode*, choose *Load unpacked*, pick the `extension/` folder.
2. In the extension's settings, sign in as `desk@demo.local` (address `http://127.0.0.1:8000`).
3. Sign in to the app in the same browser, open `http://127.0.0.1:8000/mock-portal/claims.html` and press *Load claims from the demo app*. The extension reads the list into Aeternix.
4. Use the simulator panel to play the payer: raise a query, approve with a deduction, pay with a UTR. Reload: the cases move.
5. Mark DEMO-2610-0004 ready in the app, open the mock claim form, pick the case in the panel and press *Fill from Aeternix*. Eleven fields fill and six documents attach. **You** press Submit; the extension then offers to record the claim reference on the case.

`make e2e` runs all of this in a real Chromium with the extension loaded (12 checks) and takes screenshots.

## Layout

```
backend/
  app/
    statemachine.py   the 18 states and every allowed move
    clocks.py         deadline clocks; the worker fires missed windows
    rulepacks/        one YAML per scheme: deadlines (with sources), documents, condonation, interest; rate cards
    scrubber.py       the claim check
    documents.py      storage (optional encryption), text, OCR, type sorting, field extraction
    queries.py        query desk: classify, draft, sign-off, reply
    tracker.py        portal snapshots and payer emails into case events
    recon.py          bank statement import and the matching ladder
    escalation.py     letter drafting and the ageing sweep
    reporting.py      money buckets, worklist, scorecard, pilot measures, digest
    adapters/         per-portal status mapping (Python) and page selectors (YAML, served to the extension)
    fhir.py, notify.py, importer.py, worker.py, manage.py, seed.py
    api/              HTTP routes (63 paths); /docs has the OpenAPI page
    static/mock-portal/  the test-fixture payer portal (served only when DEMO_MODE is on)
  sql/rls.sql         Postgres row-level security
  tests/              pytest; tests/rls_check.py checks hospital isolation on Postgres
web/                  React app (desk, bank, clinical review, letters, owner view, setup)
extension/            Chrome MV3 extension
tools/                screenshots and the extension end-to-end check
docs/                 ADAPTERS, RUNBOOK, SECURITY
```

## Deploying for a hospital

```sh
cp .env.example .env          # set the passwords, SECRET_KEY and FILE_ENCRYPTION_KEY
docker compose up --build     # Postgres 16, migration (tables + row-level security), API on :8000, worker
docker compose run --rm api python -m app.manage create-hospital --name "Hospital name" --code HSP --schemes cghs,pmjay,echs
docker compose run --rm api python -m app.manage create-user --hospital HSP --email admin@hospital.in --name "Admin" --role hospital_admin
```

Then, in Setup: upload the official rate cards, check each payer's bank narration keywords against a real
statement, add users, and import the current outstanding list (any spreadsheet; columns are matched by name)
so week one starts from the real backlog. Day-to-day operation, backups and updates are in
[docs/RUNBOOK.md](docs/RUNBOOK.md). Put it behind HTTPS (a reverse proxy) before real patient data goes in.

The Docker files are written but **were not run here** (no Docker in the build environment). The same
migration script was run against Postgres 16 directly, twice, and the API, worker and row-level security
were checked under the restricted database role.

## Tests

```sh
make test                                                       # 19 tests on SQLite
cd backend && DATABASE_URL=postgresql+psycopg://... python -m pytest -q   # same tests on Postgres
make e2e                                                        # browser checks against a running demo
```

## Logo

The interface loads the Aeternix logo from `web/public/logo.svg`. That file is not in the repository yet;
until it is added, the app shows the product name in plain text. Add the real artwork there; do not
recreate the wordmark in a font.
