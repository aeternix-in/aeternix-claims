import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { can, useMe } from "../App";
import { Due, ErrorNote, useAction, useLoad } from "../components/ui";

export default function Review() {
  const me = useMe();
  const { data, error, reload } = useLoad(() => api.get<{ flags: any[]; replies: any[] }>("/api/review"));
  const clin = can(me, "clinical");
  return (
    <>
      <div className="page-head"><div><h1>Clinical review</h1>
        <p>Flags only a clinician can clear, and medical-necessity replies waiting for sign-off. Aim to clear each the same day.</p></div></div>
      <ErrorNote error={error} />
      <section className="block">
        <header><h2>Flags</h2><span className="small muted">{data?.flags.length ?? "…"}</span></header>
        {data && !data.flags.length && <p className="empty">No clinical flags are waiting.</p>}
        {data?.flags.map((f) => <Flag key={f.id} f={f} clin={clin} onDone={reload} />)}
      </section>
      <section className="block">
        <header><h2>Replies to sign off</h2><span className="small muted">{data?.replies.length ?? "…"}</span></header>
        {data && !data.replies.length && <p className="empty">No replies are waiting.</p>}
        {data?.replies.map((q) => <Reply key={q.id} q={q} clin={clin} onDone={reload} />)}
      </section>
    </>
  );
}

function Flag({ f, clin, onDone }: { f: any; clin: boolean; onDone: () => void }) {
  const [note, setNote] = useState("");
  const { busy, error, run } = useAction();
  return (
    <div className="query">
      <div className="row"><Link to={`/cases/${f.case_id}`}>{f.case_no}</Link><span className="small muted">{f.payer}, {f.procedure || "procedure not set"}, {f.icd10 || "no ICD-10"}</span></div>
      <p style={{ marginTop: 6 }}>{f.message}</p>
      {clin && (
        <form className="row" onSubmit={async (e) => { e.preventDefault(); if (await run(() => api.post(`/api/checks/${f.id}/clear`, { note }))) onDone(); }}>
          <input style={{ flex: 1, minWidth: 220 }} placeholder="Clinical note: why this is acceptable" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="primary" disabled={busy}>Clear flag</button>
          <Link to={`/cases/${f.case_id}`} className="btn quiet">Open case to fix instead</Link>
        </form>
      )}
      <ErrorNote error={error} />
    </div>
  );
}

function Reply({ q, clin, onDone }: { q: any; clin: boolean; onDone: () => void }) {
  const [text, setText] = useState(q.draft_reply || "");
  const { busy, error, run } = useAction();
  return (
    <div className="query">
      <div className="row"><Link to={`/cases/${q.case_id}`}>{q.case_no}</Link><span className="small muted">{q.payer}</span><Due at={q.due_at} prefix="Reply:" /></div>
      <blockquote>{q.text}</blockquote>
      <textarea value={text} onChange={(e) => setText(e.target.value)} readOnly={!clin} />
      {clin && <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" disabled={busy} onClick={async () => { if (await run(() => api.post(`/api/queries/${q.id}/signoff`, { text }))) onDone(); }}>Sign off reply</button>
      </div>}
      <ErrorNote error={error} />
    </div>
  );
}
