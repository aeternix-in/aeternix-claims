import { useState } from "react";
import { api, openBlob } from "../../api";
import { CheckPill, ErrorNote, useAction } from "../../components/ui";
import { dayTime } from "../../format";

const LAYER = { hard: "Rule", model: "Clinical fit", clinician: "Clinician" } as Record<string, string>;
const ORDER = { red: 0, amber: 1, green: 2 } as Record<string, number>;

export default function Check({ c, onChange, canRun, isClinician }: { c: any; onChange: (x: any) => void; canRun: boolean; isClinician: boolean }) {
  const { busy, error, run } = useAction();
  const results = [...(c.check.results as any[])].sort((a, b) => ORDER[a.result] - ORDER[b.result]);
  const runAt = results[0]?.created_at;

  return (
    <section className="block">
      <header>
        <h2>Claim check</h2>
        <div className="row">
          <CheckPill status={c.check.status} />
          {canRun && <button className={c.check.status ? "quiet" : "primary"} disabled={busy}
                              onClick={async () => { const r = await run(() => api.post(`/api/cases/${c.id}/check`)); if (r) onChange(r); }}>
            {busy ? "Checking…" : c.check.status ? "Run the check again" : "Run the check"}</button>}
        </div>
      </header>
      {runAt && <p className="small muted">Last run {dayTime(runAt)}. Red blocks submission until fixed; amber needs a reviewer's note.</p>}
      {!results.length && <p className="empty">Upload the documents, confirm their fields, then run the check.</p>}
      <ErrorNote error={error} />
      {results.map((r) => <Row key={r.id} r={r} onChange={onChange} isClinician={isClinician} canClear={canRun} />)}
    </section>
  );
}

function Row({ r, onChange, isClinician, canClear }: { r: any; onChange: (x: any) => void; isClinician: boolean; canClear: boolean }) {
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);
  const { busy, error, run } = useAction();
  const cleared = !!r.cleared_at;
  const mayClear = r.result === "amber" && !cleared && (isClinician || (!r.needs_clinician && canClear));
  return (
    <div className="check-row">
      <span className={`pill ${cleared ? "" : r.result}`}>{cleared ? "Cleared" : r.result === "red" ? "Red" : r.result === "amber" ? "Amber" : "Green"}</span>
      <div>
        <div>{r.message}</div>
        <div className="small muted">{LAYER[r.layer] || r.layer}{r.details?.sample ? ", checked against SAMPLE rates" : ""}
          {cleared && <>, cleared: “{r.clear_note}”</>}
          {r.result === "amber" && !cleared && r.needs_clinician && !isClinician && <>, waiting for an Aeternix clinician</>}</div>
        {open && (
          <form onSubmit={async (e) => { e.preventDefault(); const x = await run(() => api.post(`/api/checks/${r.id}/clear`, { note })); if (x) onChange(x); }}
                style={{ display: "grid", gap: 8, marginTop: 8 }}>
            <input autoFocus placeholder="Why this is acceptable (kept in the audit trail)" value={note} onChange={(e) => setNote(e.target.value)} />
            <ErrorNote error={error} />
            <div className="row"><button className="primary" disabled={busy}>Clear flag</button><button type="button" className="quiet" onClick={() => setOpen(false)}>Cancel</button></div>
          </form>
        )}
      </div>
      <div>{mayClear && !open && <button className="quiet" onClick={() => setOpen(true)}>Clear</button>}
        {r.details?.document_id && <button className="link small" onClick={() => openBlob(`/api/documents/${r.details.document_id}/file`)}>Open page</button>}</div>
    </div>
  );
}
