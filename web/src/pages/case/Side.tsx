import { Fragment, useState } from "react";
import { api, openBlob } from "../../api";
import { Due, ErrorNote, useAction } from "../../components/ui";
import { day, dayTime, rupees } from "../../format";

export function Clocks({ c }: { c: any }) {
  const running = (c.clocks as any[]).filter((k) => k.status === "running");
  const done = (c.clocks as any[]).filter((k) => k.status === "met" || k.status === "missed");
  return (
    <section className="block">
      <header><h2>Clocks</h2></header>
      {!running.length && <p className="small muted">No deadline is running.</p>}
      {running.map((k) => (
        <div key={k.id} style={{ marginBottom: 10 }}>
          <div>{k.label}</div>
          <div className="row small"><Due at={k.due_at} /><span className="muted">due {dayTime(k.due_at)}</span></div>
          <div className="small muted">{k.source}</div>
        </div>
      ))}
      {done.length > 0 && <details className="small"><summary>Past deadlines ({done.length})</summary>
        {done.map((k) => <div key={k.id} className={k.status === "missed" ? "clock over" : "muted"}>{k.label}: {k.status === "met" ? "met" : "missed"} on {day(k.stopped_at)}</div>)}
      </details>}
    </section>
  );
}

export function MoneyTrail({ c }: { c: any }) {
  const m = c.money;
  const line = (label: string, v: number | null | undefined, extra?: string) =>
    v === null || v === undefined ? null : <div className="line"><span>{label}{extra && <span className="small muted"> {extra}</span>}</span><span className="num">{rupees(v)}</span></div>;
  return (
    <section className="block">
      <header><h2>Money trail</h2></header>
      <div className="money-trail">
        {line("Billed", m.billed_paise)}
        {line("Claimed", m.claimed_paise)}
        {(m.deductions as any[]).map((d, i) => <div className="line" key={i}><span className="small">Deduction: {d.reason}</span><span className="num">−{rupees(d.amount_paise)}</span></div>)}
        {line("Approved", m.approved_paise)}
        {line("Paid by payer", m.paid_by_payer_paise)}
        {line("Tax deducted", m.tax_paise)}
        {m.credited_paise !== null && <div className="line total"><span>Credited to bank</span><span className="num">{rupees(m.credited_paise)}</span></div>}
        {m.short_paid_paise ? <div className="line" style={{ color: "var(--red)" }}><span>Short by</span><span className="num">{rupees(m.short_paid_paise)}</span></div> : null}
      </div>
      {c.interest && c.interest.weeks > 0 && (
        <p className="notice small" style={{ marginTop: 12 }}>
          Payment is {c.interest.weeks} week{c.interest.weeks > 1 ? "s" : ""} past the {c.interest.due_days}-day due date.
          Interest at {c.interest.rate_pct}% a week: <strong>{rupees(c.interest.interest_paise)}</strong>. A letter is drafted under Letters.
        </p>
      )}
    </section>
  );
}

export function Portal({ c }: { c: any }) {
  const [pre, setPre] = useState<any>(null);
  const { error, run } = useAction();
  const portal = c.rule_pack.portal;
  const show = ["DISCHARGED", "READY_TO_SUBMIT", "LATE_CONDONATION", "REGISTERED", "TREATING"].includes(c.state);
  if (!show) return null;
  return (
    <section className="block">
      <header><h2>Portal submission</h2></header>
      <p className="small muted">
        {portal === "paper" ? "This payer takes paper or emailed bills. Download the pack, submit it, and record the acknowledgement number."
          : "With the Aeternix extension, open the portal's claim form and press Fill from Aeternix. You review and click Submit yourself."}
      </p>
      <div className="row">
        <button onClick={() => openBlob(`/api/cases/${c.id}/pack`, `${c.case_no}_claim_pack.zip`)}>Download claim pack</button>
        <button className="quiet" onClick={() => run(() => api.get(`/api/cases/${c.id}/prefill?portal=${portal === "paper" ? "mock" : portal}`)).then(setPre)}>Show field values</button>
      </div>
      <ErrorNote error={error} />
      {pre && (
        <table style={{ marginTop: 10 }}>
          <tbody>{Object.entries(pre.fields as Record<string, string>).map(([k, v]) => (
            <tr key={k}><td className="small muted">{k.replace(/_/g, " ")}</td><td>{v || "—"}</td>
              <td>{v && <button className="link small" onClick={() => navigator.clipboard?.writeText(v)}>Copy</button>}</td></tr>
          ))}</tbody>
        </table>
      )}
    </section>
  );
}

export function Details({ c, onChange, canEdit }: { c: any; onChange: (x: any) => void; canEdit: boolean }) {
  const [edit, setEdit] = useState(false);
  const [f, setF] = useState<Record<string, any>>({});
  const { busy, error, run } = useAction();
  const fields: [string, string][] = [["beneficiary_name", "Patient"], ["beneficiary_ref", "Card no."], ["referral_no", "Referral no."],
    ["provisional_diagnosis", "Diagnosis"], ["icd10", "ICD-10"], ["planned_procedure", "Procedure"], ["package_code", "Package"],
    ["treating_doctor", "Doctor"], ["ward", "Ward"]];
  const flags: [string, string][] = [["emergency", "Emergency"], ["surgical", "Surgical"], ["implant_used", "Implant used"], ["portability", "Portability"]];
  return (
    <section className="block">
      <header><h2>Case details</h2>{canEdit && !edit && <button className="link" onClick={() => { setF({}); setEdit(true); }}>Edit</button>}</header>
      {edit ? (
        <form onSubmit={async (e) => { e.preventDefault(); const r = await run(() => api.patch(`/api/cases/${c.id}`, f)); if (r) { onChange(r); setEdit(false); } }} style={{ display: "grid", gap: 8 }}>
          {fields.map(([k, l]) => <label key={k}>{l}<input defaultValue={c[k] || ""} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>)}
          {flags.map(([k, l]) => <label key={k} className="check"><input type="checkbox" defaultChecked={c[k]} onChange={(e) => setF({ ...f, [k]: e.target.checked })} />{l}</label>)}
          <ErrorNote error={error} />
          <div className="row"><button className="primary" disabled={busy}>Save</button><button type="button" className="quiet" onClick={() => setEdit(false)}>Cancel</button></div>
        </form>
      ) : (
        <div className="fields" style={{ gridTemplateColumns: "110px 1fr" }}>
          {fields.map(([k, l]) => <Fragment key={k}><span className="k">{l}</span><span>{c[k] || "—"}</span></Fragment>)}
          <span className="k">Flags</span><span>{flags.filter(([k]) => c[k]).map(([, l]) => l).join(", ") || "None"}</span>
          <span className="k">Rules</span><span className="small">{c.rule_pack.name}, version {c.rule_pack.version}</span>
        </div>
      )}
    </section>
  );
}

export function Counter({ c, onChange, canEdit }: { c: any; onChange: (x: any) => void; canEdit: boolean }) {
  const { busy, error, run } = useAction();
  const checks = c.rule_pack.counter_checks as { key: string; label: string }[];
  const showReg = c.state === "REGISTERED" || c.clocks.some((k: any) => k.kind === "portal_registration");
  if (!checks.length && !showReg) return null;
  return (
    <section className="block">
      <header><h2>At the counter</h2></header>
      {showReg && (
        <div className="row" style={{ marginBottom: 8 }}>
          {c.portal_registered_at ? <span className="small">Registered on the portal {dayTime(c.portal_registered_at)}</span>
            : canEdit && <button disabled={busy} onClick={async () => { const r = await run(() => api.post(`/api/cases/${c.id}/portal-registered`, {})); if (r) onChange(r); }}>I registered this case on the portal</button>}
        </div>
      )}
      {checks.map((k) => (
        <div key={k.key} className="row" style={{ marginBottom: 6 }}>
          {c.counter_checks[k.key] ? <span className="pill green">Done</span> : <span className="pill amber">Not done</span>}
          <span>{k.label}</span>
          {!c.counter_checks[k.key] && canEdit && <button className="link" disabled={busy} onClick={async () => { const r = await run(() => api.post(`/api/cases/${c.id}/counter-checks`, { key: k.key })); if (r) onChange(r); }}>Record</button>}
        </div>
      ))}
      <ErrorNote error={error} />
    </section>
  );
}

export function Timeline({ c }: { c: any }) {
  return (
    <section className="block">
      <header><h2>History</h2><button className="link small" onClick={() => openBlob(`/api/cases/${c.id}/fhir`)}>FHIR view</button></header>
      <ul className="timeline">
        {(c.timeline as any[]).slice(0, 40).map((t, i) => (
          <li key={i}><span className="muted">{dayTime(t.at)}</span> {t.label || t.action}{t.actor ? <span className="muted">, {t.actor}</span> : null}</li>
        ))}
      </ul>
    </section>
  );
}
