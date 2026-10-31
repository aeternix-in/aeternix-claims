# Portal adapters: capturing a real portal

The extension knows nothing about any portal by itself. For each portal it reads a selector file from
Aeternix (`backend/app/adapters/selectors/<portal>.yaml`) that says where the claim list, the claim form
and the confirmation reference are on the page. A portal whose file says `status: not_captured` is left
alone: the extension does nothing there and says so in its popup.

As shipped, only `mock.yaml` (the test fixture) is captured. NHA TMS 2.0, UTI-ITSL ECHS and the TPA
template are empty, because their pages can only be read from a signed-in hospital desk session.

## Rules before you start

- Get the hospital's written go-ahead, and capture from the desk's own signed-in session, with the desk
  person present. Aeternix never stores portal user IDs, passwords or OTPs, and the extension never signs in.
- The extension reads pages and fills fields. It **never presses Submit, never solves a captcha, never
  answers an OTP** and never runs on a page without a person in front of it. Keep it that way: a person
  checking every filled claim before it goes is part of the product, not a stopgap.
- Read the portal's terms of use. If they forbid browser extensions or automated reading, stop and raise it.
- Nothing from a real patient goes into a selector file, a ticket or a screenshot. Redact screenshots.

## What to capture

Open the portal in Chrome, signed in, and use DevTools (right-click, *Inspect*). For each page:

| Page | What to record |
| --- | --- |
| Claim list | `url_contains`: a piece of the address only this page has. `row`: a selector matching one table row per claim. `cells`: inside a row, the element holding the claim reference, status text, approved amount, deduction, payment reference (UTR), payment date, and query text. Leave a cell `null` if the list does not show it. |
| Claim form | `url_contains`. `fields`: one selector per field Aeternix can fill (keys are listed below). `uploads`: each `input[type=file]`, and which document types go into it. `submit_button`: only so the extension can outline it. |
| Confirmation | `url_contains`, and `portal_ref`: the element holding the claim or case number the portal gives after submission. |

Field keys the extension can fill: `case_no, patient_name, beneficiary_ref, referral_no, age, sex,
admission_date, discharge_date, procedure_date, diagnosis, icd10, procedure, package_code, claim_amount,
bill_no, bill_date, treating_doctor, ward`.

Document types for uploads: `discharge_summary, final_bill, investigation_report, ot_notes,
implant_invoice, implant_sticker, referral_letter, emergency_certificate, beneficiary_card` (see
`app/documents.py`). Only documents a person has confirmed in Aeternix are attached.

Selector advice:

- Prefer `#id`, `[name=...]`, or a `data-*` attribute. Avoid generated class names (`.css-1x9f2a`) and
  position (`tr:nth-child(3)`): they change with every portal release.
- Check every selector in the DevTools console: `document.querySelectorAll('...')` should find exactly
  what you mean (one element per field; one per row for `row`).
- Note the date format the portal's date inputs expect, and the format of amounts.

`mock.yaml` is the worked example: copy its shape.

## Status wording

The tracker turns the status text it reads into an event using rules in `app/adapters/__init__.py`.
The generic rules cover common wording ("approved", "query raised", "payment done"...), but each portal
has its own phrases. Collect every status string the portal shows (from real claims, without patient
details), then for that portal's `Adapter(...)`:

1. add `extra_rules`: exact phrases first, each mapped to one of `submitted, query, approved,
   part_approved, rejected, paid`;
2. set `date_format` if the portal's dates are not `dd/mm/yyyy`;
3. add a test in `tests/test_modules.py` that feeds each captured phrase to `semantic_status` and checks the
   result. A phrase that maps to the wrong thing moves real money cases to the wrong state.

## Going live with a portal

1. Fill the YAML, set `status: ready`, and restart the API (selector files are cached in memory).
2. In the extension's settings, press *Refresh from Aeternix*, then *Allow* for the portal.
3. Switch off *Read claim lists into Aeternix as soon as they open* for the first week, and press *Read
   this list* by hand while you compare the panel's result with the portal.
4. For the first 20 claims filed through the extension, a second person checks every filled field
   against the claim pack before Submit.
5. Turn automatic reading back on.

## When a portal changes

The panel says *Not found on this page: ...* for any selector that no longer matches. Fix the YAML,
restart the API, and press *Refresh from Aeternix* in the extension's settings (it also refreshes on its
own every 15 minutes). No new extension release is needed.

## Known limits of the V1 engine

- It runs in the top frame only. A portal that puts its form inside an `<iframe>` needs the engine
  registered with `allFrames` and the selectors scoped to that frame.
- It uses `document.querySelector`, so it cannot reach inside a closed shadow DOM.
- It fills fields by setting their value and firing `input` and `change` events. Most forms accept that;
  some custom widgets (searchable dropdowns, date pickers that ignore typed values) need a per-field
  workaround, which is the most likely work when capturing NHA TMS 2.0.
- Portals that log out after a few minutes of inactivity will interrupt filling; the desk signs in again
  and presses *Fill from Aeternix* once more.
