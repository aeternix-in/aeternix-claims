# Security notes

This system holds patient names, beneficiary IDs, diagnoses, bills and the hospital's bank credits.
These notes say what V1 does, and, as plainly, what it does not do yet.

## What V1 does

- **Hospital isolation, twice.** Every query is filtered by the signed-in user's hospital in the API. On
  Postgres, row-level security (`sql/rls.sql`) applies the same rule inside the database, and the API and
  worker connect as a restricted role (`aeternix_app`). `tests/rls_check.py` checks that a session scoped
  to one hospital cannot read another's rows.
- **Append-only audit log.** Every state change, document, reply, match and letter is written to
  `audit_log`. The application role has no UPDATE or DELETE on that table.
- **Passwords** are stored as PBKDF2-SHA256 with 200,000 iterations and a per-user salt. Sign-in tokens are
  HMAC-signed with `SECRET_KEY` and expire after `TOKEN_TTL_HOURS` (12 by default). The API refuses to start
  with the default key when `DEMO_MODE` is off.
- **Roles.** Desk, owner, accountant, hospital admin, clinician, Aeternix admin. Each route checks the role
  it needs; medical-necessity replies need a clinician's sign-off.
- **Files at rest** are encrypted with Fernet when `FILE_ENCRYPTION_KEY` is set. Every file is hashed
  (SHA-256) on arrival.
- **Outbound data.** Nothing leaves the server unless an integration is switched on. With
  `LLM_PROVIDER=anthropic`, text is redacted first (patient names, Aadhaar-like and long ID numbers, phone
  numbers). The digest carries case numbers and amounts only.
- **The extension** keeps its sign-in token in the extension's own storage; portal pages never see it.
  From a portal page it can do two things: send the claim-list rows it read (the tracker turns those into
  case events, only along moves the state machine allows, recorded with source "portal"), and record a
  submission the person confirmed. Every other change happens in the app. It never presses Submit and
  never handles portal credentials. It trusts what the portal page shows; a tampered page would feed it
  wrong statuses, which the audit log would show as coming from the portal.

## What V1 does not do yet

- **Row-level security guards against our own mistakes, not against a compromised server.** The worker
  needs to see every hospital, so the application role is allowed to switch the check off for a
  transaction (`app.bypass_rls`). Anyone who can run arbitrary SQL as that role can read every hospital.
  Next step: a separate database role for the worker, and no bypass for the API role.
- **Redaction is pattern-based.** It catches names Aeternix knows and number shapes; it will miss a
  relative's name in free text. Keep the LLM off until a data-processing agreement is in place and the
  hospital has agreed in writing.
- No two-factor sign-in, no single sign-on, no password reset by email, no lockout after failed sign-ins.
  Put the deployment behind a reverse proxy with rate limiting on `/api/auth/login`.
- No HTTPS of its own. Terminate TLS at the proxy; never expose port 8000 to the internet.
- Tokens cannot be revoked before they expire, except by rotating `SECRET_KEY` (which signs everyone out).
- File encryption is optional and off by default for the laptop demo. Turn it on for any real data.
- No automated retention or deletion schedule.

## India's data protection law

The Digital Personal Data Protection Act, 2023 applies to this data, with the hospital as the data
fiduciary and Aeternix processing on its behalf. This repository does not make a deployment compliant on
its own. Before real patient data goes in, the hospital and Aeternix need: a written processing
agreement, a decision on where the server and backups are hosted, a breach-notification procedure, and
a retention period for claim documents. The DPDP Rules were notified in November 2025 with most data
fiduciary obligations taking effect 18 months later (around May 2027), so a pilot that starts now will be
running when they bite. Take legal advice; this note is not that.

## Reporting a problem

Security issues go to the Aeternix team directly, not to a public issue tracker.
