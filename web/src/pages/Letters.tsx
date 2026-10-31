import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorNote, useAction, useLoad } from "../components/ui";
import { day, rupees } from "../format";

export default function Letters() {
  const [status, setStatus] = useState("draft");
  const { data, error, reload } = useLoad(() => api.get<any[]>(`/api/escalations?status=${status}`), [status]);
  const sweep = useAction();
  return (
    <>
      <div className="page-head">
        <div><h1>Letters</h1><p>Drafted from the ageing rules. Nothing is sent until you send it and record the reference.</p></div>
        <button className="quiet" disabled={sweep.busy} onClick={async () => { const r = await sweep.run(() => api.post("/api/escalations/sweep")); if (r) reload(); }}>Check for new letters</button>
      </div>
      <div className="row" role="tablist" style={{ marginBottom: 10 }}>
        {[["draft", "To send"], ["sent", "Sent"], ["responded,closed", "Answered"]].map(([k, l]) =>
          <button key={k} role="tab" aria-selected={status === k} className={status === k ? "primary" : "quiet"} onClick={() => setStatus(k)}>{l}</button>)}
      </div>
      <ErrorNote error={error || sweep.error} />
      {data && !data.length && <p className="empty">No letters here.</p>}
      {data?.map((e) => <Letter key={e.id} e={e} onDone={reload} />)}
    </>
  );
}

function Letter({ e, onDone }: { e: any; onDone: () => void }) {
  const [body, setBody] = useState(e.body);
  const [ref, setRef] = useState("");
  const [response, setResponse] = useState("");
  const { busy, error, run } = useAction();
  return (
    <section className="block">
      <header>
        <div><strong>{e.kind_label}</strong> <Link to={`/cases/${e.case_id}`}>{e.case_no}</Link> <span className="muted small">{e.payer}</span>
          {e.amount_paise ? <span className="pill sky" style={{ marginLeft: 8 }}>{rupees(e.amount_paise)}</span> : null}</div>
        <span className="small muted">Drafted {day(e.created_at)}{e.sent_at && `, sent ${day(e.sent_at)}`}{e.reference_no && `, ref ${e.reference_no}`}</span>
      </header>
      <p className="small"><strong>{e.subject}</strong></p>
      {e.status === "draft" ? (
        <>
          <textarea style={{ minHeight: 260, fontFamily: "ui-monospace, Consolas, monospace", fontSize: 13 }} value={body} onChange={(x) => setBody(x.target.value)} />
          <div className="row" style={{ marginTop: 8 }}>
            <input style={{ width: 280 }} placeholder="Grievance, diary or email reference" value={ref} onChange={(x) => setRef(x.target.value)} />
            <button className="primary" disabled={busy} onClick={async () => { if (await run(() => api.post(`/api/escalations/${e.id}/sent`, { reference_no: ref, body }))) onDone(); }}>I sent this letter</button>
            <button className="quiet" onClick={() => navigator.clipboard?.writeText(`${e.subject}\n\n${body}`)}>Copy text</button>
          </div>
        </>
      ) : (
        <>
          <pre className="digest">{e.body}</pre>
          {e.status === "sent" && (
            <div className="row">
              <input style={{ flex: 1, minWidth: 240 }} placeholder="What the payer said" value={response} onChange={(x) => setResponse(x.target.value)} />
              <button disabled={busy} onClick={async () => { if (await run(() => api.post(`/api/escalations/${e.id}/response`, { response, close: true }))) onDone(); }}>Record answer</button>
            </div>
          )}
          {e.response && <p className="small">Answer: {e.response}</p>}
        </>
      )}
      <ErrorNote error={error} />
    </section>
  );
}
