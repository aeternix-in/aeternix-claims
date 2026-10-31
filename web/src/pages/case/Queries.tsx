import { useState } from "react";
import { api } from "../../api";
import { Due, ErrorNote, useAction } from "../../components/ui";
import { dayTime } from "../../format";

export default function Queries({ c, categories, onChange, canEdit, isClinician }: {
  c: any; categories: Record<string, string>; onChange: (x: any) => void; canEdit: boolean; isClinician: boolean;
}) {
  const [text, setText] = useState("");
  const { busy, error, run } = useAction();
  const canAdd = ["PREAUTH_SUBMITTED", "CLAIM_SUBMITTED", "PREAUTH_QUERY", "CLAIM_QUERY"].includes(c.state);
  const queries = c.queries as any[];
  if (!canAdd && !queries.length) return null;

  return (
    <section className="block">
      <header><h2>Payer queries</h2><span className="small muted">{queries.filter((q) => !["replied", "closed"].includes(q.status)).length} open</span></header>
      {canEdit && canAdd && (
        <form onSubmit={async (e) => { e.preventDefault(); const r = await run(() => api.post(`/api/cases/${c.id}/queries`, { text })); if (r) { onChange(r); setText(""); } }}
              style={{ display: "grid", gap: 8, marginBottom: 18 }}>
          <label>Paste a query from the portal or an email, exactly as written
            <textarea style={{ minHeight: 70 }} value={text} onChange={(e) => setText(e.target.value)} /></label>
          <ErrorNote error={error} />
          <div><button disabled={busy || text.trim().length < 5}>Add query and draft a reply</button></div>
        </form>
      )}
      {queries.map((q) => <QueryCard key={q.id} q={q} c={c} categories={categories} onChange={onChange} canEdit={canEdit} isClinician={isClinician} />)}
    </section>
  );
}

function QueryCard({ q, c, categories, onChange, canEdit, isClinician }: any) {
  const [draft, setDraft] = useState<string>(q.draft_reply || "");
  const [docs, setDocs] = useState<string[]>(q.draft_documents || []);
  const { busy, error, run } = useAction();
  const open = !["replied", "closed"].includes(q.status);
  const save = () => run(() => api.patch(`/api/queries/${q.id}`, { draft_reply: draft, draft_documents: docs })).then((r) => r && onChange(r));

  return (
    <div className="query">
      <div className="row">
        <span className="pill">{categories[q.category] || q.category}</span>
        <span className="small muted">{q.stage === "preauth" ? "Pre-auth" : "Claim"} query, raised {dayTime(q.raised_at)} via {q.source}</span>
        {open && <Due at={q.due_at} prefix="Reply:" />}
        {q.status === "awaiting_clinician" && <span className="pill amber">Waiting for clinician sign-off</span>}
        {q.status === "replied" && <span className="pill green">Replied {dayTime(q.replied_at)}</span>}
        {q.outcome && <span className="pill">{q.outcome.replace("_", " ")}</span>}
      </div>
      <blockquote>{q.text}</blockquote>
      {open ? (
        <>
          <label>Reply to send on the portal
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={() => canEdit || isClinician ? save() : undefined} readOnly={!canEdit && !isClinician} />
          </label>
          <div className="small muted" style={{ margin: "8px 0 4px" }}>Attach with the reply</div>
          <div className="row">
            {(c.documents as any[]).filter((d) => !d.voided).map((d) => (
              <label key={d.id} className="check small">
                <input type="checkbox" checked={docs.includes(d.id)} disabled={!canEdit}
                       onChange={(e) => setDocs(e.target.checked ? [...docs, d.id] : docs.filter((x) => x !== d.id))} />{d.doc_label}
              </label>
            ))}
          </div>
          <ErrorNote error={error} />
          <div className="row" style={{ marginTop: 10 }}>
            {isClinician && q.status === "awaiting_clinician" &&
              <button className="primary" disabled={busy} onClick={async () => { const r = await run(() => api.post(`/api/queries/${q.id}/signoff`, { text: draft })); if (r) onChange(r); }}>Sign off reply</button>}
            {canEdit && <button className="primary" disabled={busy || q.status === "awaiting_clinician"}
                    onClick={async () => { const r = await run(() => api.post(`/api/queries/${q.id}/reply`, { reply_text: draft, document_ids: docs })); if (r) onChange(r); }}>
              I sent this reply on the portal</button>}
            {canEdit && <button className="quiet" disabled={busy} onClick={async () => { const r = await run(() => api.post(`/api/queries/${q.id}/redraft`)); if (r) { onChange(r); } }}>Draft again</button>}
          </div>
        </>
      ) : (
        <>
          <div className="small muted">Reply sent</div>
          <p style={{ whiteSpace: "pre-wrap" }}>{q.reply_text}</p>
          {canEdit && !q.outcome && (
            <div className="row">
              <span className="small muted">What happened next?</span>
              {[["cleared", "Query cleared"], ["repeat_query", "Payer asked again"], ["rejected", "Claim rejected"]].map(([k, l]) => (
                <button key={k} className="quiet" disabled={busy} onClick={async () => { const r = await run(() => api.post(`/api/queries/${q.id}/outcome`, { outcome: k })); if (r) onChange(r); }}>{l}</button>
              ))}
            </div>
          )}
          <ErrorNote error={error} />
        </>
      )}
    </div>
  );
}
