import { useState } from "react";
import { api } from "../../api";
import { ErrorNote, useAction } from "../../components/ui";
import { istInputToIso, nowLocalInput } from "../../format";

type Field = { k: string; label: string; type?: string; required?: boolean };

const FIELDS: Record<string, Field[]> = {
  submit_preauth: [{ k: "portal_ref", label: "Portal reference shown after you submitted", required: true }],
  submit_enhancement: [{ k: "portal_ref", label: "Enhancement reference", required: true }, { k: "amount", label: "Enhancement amount (₹)", type: "number" }],
  submit_claim: [{ k: "portal_ref", label: "Claim reference from the portal", required: true }],
  appeal: [{ k: "portal_ref", label: "Appeal or reprocess reference", required: true }],
  discharge: [{ k: "discharged_at", label: "Discharged at (IST)", type: "datetime-local", required: true }],
  approve_preauth: [{ k: "approved_amount", label: "Approved amount (₹), if shown", type: "number" }],
  approve_enhancement: [{ k: "approved_amount", label: "Approved amount (₹), if shown", type: "number" }],
  approve: [{ k: "approved_amount", label: "Approved amount (₹)", type: "number", required: true }],
  part_approve: [{ k: "approved_amount", label: "Approved amount (₹)", type: "number", required: true },
                 { k: "deduction_reason", label: "Deduction reason, as the payer wrote it", required: true },
                 { k: "deduction_amount", label: "Deduction amount (₹)", type: "number", required: true }],
  reject: [{ k: "reason", label: "Rejection reason, as the payer wrote it", required: true }],
  reject_preauth: [{ k: "reason", label: "Rejection reason, as the payer wrote it", required: true }],
  payer_paid: [{ k: "payment_ref", label: "Payment reference (UTR or advice no.)", required: true },
               { k: "paid_on", label: "Paid on", type: "date" }, { k: "amount", label: "Amount paid (₹), if different", type: "number" }],
  dispute: [{ k: "reason", label: "What is being disputed", required: true }],
  write_off: [{ k: "reason", label: "Why this is written off", required: true }],
  grant_condonation: [{ k: "reference_no", label: "Condonation order or reference no.", required: true }],
  close: [{ k: "reason", label: "Reason (optional)" }],
};

const HIDDEN = new Set(["raise_preauth_query", "raise_claim_query", "reply_preauth_query", "reply_claim_query"]);
const PRIMARY = new Set(["submit_preauth", "submit_claim", "mark_ready", "discharge", "approve_preauth"]);

export default function Actions({ c, onChange, paper }: { c: any; onChange: (x: any) => void; paper: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [vals, setVals] = useState<Record<string, string>>({});
  const { busy, error, setError, run } = useAction();
  const actions = (c.allowed_actions as { event: string; label: string; button: string }[]).filter((a) => !HIDDEN.has(a.event));
  const checkGreen = c.check?.status === "green";
  const blocked = (ev: string) => ev === "mark_ready" && !checkGreen;

  function start(ev: string) {
    setError(null);
    const fields = FIELDS[ev];
    if (!fields) { go(ev, {}); return; }
    setVals(ev === "discharge" ? { discharged_at: nowLocalInput() } : {});
    setOpen(ev);
  }

  async function go(ev: string, v: Record<string, string>) {
    const body: Record<string, unknown> = { ...v };
    if (v.discharged_at) body.discharged_at = istInputToIso(v.discharged_at);
    if (ev === "part_approve") {
      body.deductions = [{ reason: v.deduction_reason, amount: Number(v.deduction_amount) }];
      delete body.deduction_reason; delete body.deduction_amount;
    }
    for (const k of ["amount", "approved_amount"]) if (body[k] === "" || body[k] === undefined) delete body[k];
    const r = await run(() => api.post(`/api/cases/${c.id}/actions/${ev}`, body));
    if (r) { onChange(r); setOpen(null); }
  }

  if (!actions.length) return <p className="muted small">No actions: this case is closed.</p>;
  return (
    <div className="actions">
      {actions.map((a) => (
        <div key={a.event}>
          {open === a.event ? (
            <form className="action-form" onSubmit={(e) => { e.preventDefault(); go(a.event, vals); }}>
              <strong>{a.button}</strong>
              {FIELDS[a.event].map((f) => (
                <label key={f.k}>{f.k === "portal_ref" && paper && a.event === "submit_claim" ? "Acknowledgement or diary no." : f.label}
                  <input type={f.type || "text"} step={f.type === "number" ? "0.01" : undefined} required={f.required}
                         value={vals[f.k] || ""} onChange={(e) => setVals({ ...vals, [f.k]: e.target.value })} />
                </label>
              ))}
              <ErrorNote error={error} />
              <div className="row">
                <button className="primary" disabled={busy}>Record</button>
                <button type="button" className="quiet" onClick={() => setOpen(null)}>Cancel</button>
              </div>
            </form>
          ) : (
            <>
              <button className={PRIMARY.has(a.event) && !blocked(a.event) ? "primary" : ""}
                      style={{ width: "100%", justifyContent: "flex-start" }}
                      onClick={() => start(a.event)} disabled={busy || blocked(a.event)}>{a.button}</button>
              {blocked(a.event) && <p className="small muted" style={{ margin: "4px 0 0" }}>
                {c.check?.status ? "Opens when the claim check is green." : "Run the claim check first."}</p>}
            </>
          )}
        </div>
      ))}
      {!open && <ErrorNote error={error} />}
    </div>
  );
}
