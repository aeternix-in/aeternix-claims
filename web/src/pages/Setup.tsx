import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { can, useMe } from "../App";
import { ErrorNote, useAction, useLoad } from "../components/ui";

export default function Setup() {
  const me = useMe();
  const admin = can(me, "admin");
  return (
    <>
      <div className="page-head"><div><h1>Setup</h1><p>Payers, rate cards, the hospital's existing claims list, and people.</p></div></div>
      <ImportList />
      <Payers admin={admin} />
      <RuleCards admin={admin} />
      <Digest admin={admin} />
      <Email />
      <Unlinked />
      {admin && <Users />}
      <section className="block">
        <header><h2>Browser extension</h2></header>
        <p>The extension reads claim status on the portals and fills claim forms from the checked pack. It never clicks Submit.
          Install it from the <code>extension</code> folder (Chrome: Extensions, Developer mode, Load unpacked), then sign in from its options page.</p>
        <p className="small muted">Portals that are not set up yet show a notice instead of acting. Selectors for NHA TMS 2.0 are captured from a signed-in desk session; see docs/ADAPTERS.md.</p>
      </section>
    </>
  );
}

function ImportList() {
  const payers = useLoad(() => api.get<any[]>("/api/payers"));
  const [file, setFile] = useState<File | null>(null);
  const [payer, setPayer] = useState("");
  const [res, setRes] = useState<any>(null);
  const { busy, error, run } = useAction();
  return (
    <section className="block">
      <header><h2>Load the outstanding claims list</h2></header>
      <p className="small muted">The week-one baseline: any spreadsheet with claim no., patient, payer, dates, amounts and status. Each row becomes a case in the state its status describes.</p>
      <div className="row">
        <input type="file" accept=".csv,.xlsx" onChange={(e) => setFile(e.target.files?.[0] || null)} style={{ maxWidth: 300 }} />
        <select value={payer} onChange={(e) => setPayer(e.target.value)} style={{ width: 240 }} aria-label="Default payer">
          <option value="">Payer from the file</option>{payers.data?.map((p) => <option key={p.id} value={p.id}>Default: {p.name}</option>)}
        </select>
        <button className="primary" disabled={!file || busy} onClick={async () => {
          const f = new FormData(); f.append("file", file!); if (payer) f.append("default_payer_id", payer);
          const r = await run(() => api.upload("/api/import/outstanding", f)); if (r) setRes(r);
        }}>Load list</button>
      </div>
      {res && <p className="notice small">{res.created} cases created, {res.skipped_existing} already here.{res.errors.length ? ` ${res.errors.length} rows need attention: ${res.errors.slice(0, 3).join(" ")}` : ""}</p>}
      <ErrorNote error={error} />
    </section>
  );
}

function Payers({ admin }: { admin: boolean }) {
  const { data, error, reload } = useLoad(() => api.get<any[]>("/api/payers"));
  return (
    <section className="block">
      <header><h2>Payers</h2></header>
      <ErrorNote error={error} />
      <div className="table-scroll"><table>
        <thead><tr><th>Payer</th><th>Rules</th><th>Portal</th><th>Tax deducted at source</th><th>Words in bank narrations</th><th>Escalation contact</th></tr></thead>
        <tbody>{data?.map((p) => <PayerRow key={p.id} p={p} admin={admin} onSaved={reload} />)}</tbody>
      </table></div>
    </section>
  );
}

function PayerRow({ p, admin, onSaved }: { p: any; admin: boolean; onSaved: () => void }) {
  const [tds, setTds] = useState(p.expected_tds_bps === null ? "" : String(p.expected_tds_bps / 100));
  const [kw, setKw] = useState((p.narration_keywords || []).join(", "));
  const [contact, setContact] = useState(p.escalation_contacts?.[0]?.email || "");
  const { busy, error, run } = useAction();
  const dirty = tds !== (p.expected_tds_bps === null ? "" : String(p.expected_tds_bps / 100)) || kw !== (p.narration_keywords || []).join(", ") || contact !== (p.escalation_contacts?.[0]?.email || "");
  return (
    <tr>
      <td><strong>{p.code}</strong><div className="small muted">{p.name}</div></td>
      <td className="small">{p.scheme}</td><td className="small">{p.portal}</td>
      <td style={{ width: 120 }}>{admin ? <input value={tds} placeholder="not set" onChange={(e) => setTds(e.target.value)} aria-label="TDS percent" /> : tds || "not set"}{admin && <span className="small muted">%</span>}</td>
      <td>{admin ? <input value={kw} onChange={(e) => setKw(e.target.value)} /> : kw}</td>
      <td>{admin ? <input value={contact} placeholder="email" onChange={(e) => setContact(e.target.value)} /> : contact}
        {admin && dirty && <button className="primary" style={{ marginTop: 6 }} disabled={busy} onClick={async () => {
          const r = await run(() => api.patch(`/api/payers/${p.id}`, {
            expected_tds_bps: tds === "" ? null : Math.round(Number(tds) * 100),
            narration_keywords: kw.split(",").map((s: string) => s.trim().toUpperCase()).filter(Boolean),
            escalation_contacts: contact ? [{ ...(p.escalation_contacts?.[0] || {}), email: contact }] : [],
          })); if (r) onSaved();
        }}>Save</button>}
        <ErrorNote error={error} /></td>
    </tr>
  );
}

function RuleCards({ admin }: { admin: boolean }) {
  const { data, error, reload } = useLoad(() => api.get<any[]>("/api/rulepacks"));
  return (
    <section className="block">
      <header><h2>Scheme rules and rate cards</h2></header>
      <ErrorNote error={error} />
      <table><thead><tr><th>Scheme</th><th>Version</th><th>Rate card</th>{admin && <th>Load the official rates</th>}</tr></thead>
        <tbody>{data?.map((p) => <RuleRow key={p.key} p={p} admin={admin} onSaved={reload} />)}</tbody></table>
    </section>
  );
}

function RuleRow({ p, admin, onSaved }: { p: any; admin: boolean; onSaved: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [eff, setEff] = useState("");
  const { busy, error, run } = useAction();
  const rc = p.rate_card;
  return (
    <tr>
      <td>{p.name}</td><td className="small">{p.version}</td>
      <td className="small">{rc.loaded ? <>{rc.file}, from {rc.effective_from} {rc.sample && <span className="pill amber">SAMPLE</span>}</> : <span className="pill amber">None loaded</span>}</td>
      {admin && <td><div className="row">
        <input type="file" accept=".csv" onChange={(e) => setFile(e.target.files?.[0] || null)} style={{ maxWidth: 220 }} />
        <input type="date" value={eff} onChange={(e) => setEff(e.target.value)} style={{ width: 150 }} aria-label="Effective from" />
        <button disabled={!file || !eff || busy} onClick={async () => {
          const f = new FormData(); f.append("file", file!); f.append("effective_from", eff);
          if (await run(() => api.upload(`/api/rulepacks/${p.key}/rate-card`, f))) onSaved();
        }}>Load</button></div><ErrorNote error={error} /></td>}
    </tr>
  );
}

function Digest({ admin }: { admin: boolean }) {
  const { data, reload } = useLoad(() => api.get("/api/hospital"));
  const [to, setTo] = useState("");
  const [channel, setChannel] = useState("whatsapp");
  const { busy, error, run } = useAction();
  if (!data) return null;
  const save = (list: any[]) => run(() => api.patch("/api/hospital", { digest_recipients: list })).then((r) => r && reload());
  return (
    <section className="block">
      <header><h2>Who gets the 8 am digest</h2></header>
      {(data.digest_recipients as any[]).map((r, i) => (
        <div key={i} className="row"><span className="pill">{r.channel}</span>{r.to}
          {admin && <button className="link" onClick={() => save(data.digest_recipients.filter((_: any, n: number) => n !== i))}>Remove</button>}</div>
      ))}
      {admin && <div className="row" style={{ marginTop: 8 }}>
        <select value={channel} onChange={(e) => setChannel(e.target.value)} style={{ width: 140 }}><option value="whatsapp">WhatsApp</option><option value="smtp">Email</option><option value="console">Server log</option></select>
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder={channel === "whatsapp" ? "Phone with country code, e.g. 9198…" : "Email address"} style={{ width: 280 }} />
        <button disabled={!to || busy} onClick={() => save([...(data.digest_recipients || []), { channel, to }]).then(() => setTo(""))}>Add</button>
      </div>}
      <ErrorNote error={error} />
    </section>
  );
}

function Email() {
  const [raw, setRaw] = useState("");
  const [res, setRes] = useState<any>(null);
  const { busy, error, run } = useAction();
  return (
    <section className="block">
      <header><h2>Paste a payer email</h2></header>
      <p className="small muted">Approvals, queries and settlement advices from insurers and TPAs. The reference is matched to a case and the suggested step waits for you on the case.</p>
      <textarea value={raw} onChange={(e) => setRaw(e.target.value)} style={{ minHeight: 90 }} />
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={busy || raw.length < 10} onClick={async () => { const f = new FormData(); f.append("raw", raw); const r = await run(() => api.upload("/api/inbound/email", f)); if (r) { setRes(r); setRaw(""); } }}>Read email</button>
        {res && (res.case_id ? <span className="small">Matched to a case: <Link to={`/cases/${res.case_id}`}>open it</Link>. Looks like a {res.parsed.kind}.
          {res.parsed.suggestions?.length > 0 && <button className="link" onClick={() => run(() => api.post(`/api/inbound/emails/${res.id}/apply`, { index: 0 })).then((x) => x && setRes({ ...res, applied: true }))}>{res.applied ? "Applied" : "Apply the suggested step"}</button>}</span>
          : <span className="small muted">No case reference found in this email.</span>)}
      </div>
      <ErrorNote error={error} />
    </section>
  );
}

function Unlinked() {
  const { data } = useLoad(() => api.get<any[]>("/api/tracker/unlinked"));
  if (!data?.length) return null;
  return (
    <section className="block">
      <header><h2>Portal claims not linked to a case</h2></header>
      <p className="small muted">The extension saw these on a portal but no case carries the reference. Open the case and set its portal ref, or load the outstanding list.</p>
      <table><tbody>{data.map((s) => <tr key={s.id}><td>{s.portal_ref}</td><td className="small">{s.portal}</td><td>{s.status_text}</td></tr>)}</tbody></table>
    </section>
  );
}

function Users() {
  const { data, reload } = useLoad(() => api.get<any[]>("/api/users"));
  const [f, setF] = useState({ name: "", email: "", role: "desk", password: "" });
  const { busy, error, run } = useAction();
  return (
    <section className="block">
      <header><h2>People</h2></header>
      <table><tbody>{data?.map((u) => <tr key={u.id}><td>{u.name}</td><td>{u.email}</td><td>{u.role}</td></tr>)}</tbody></table>
      <form className="row" style={{ marginTop: 10 }} onSubmit={async (e) => { e.preventDefault(); if (await run(() => api.post("/api/users", f))) { setF({ name: "", email: "", role: "desk", password: "" }); reload(); } }}>
        <input placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} style={{ width: 160 }} />
        <input placeholder="Email" type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} style={{ width: 200 }} />
        <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })} style={{ width: 150 }}>
          <option value="desk">Desk</option><option value="accountant">Accountant</option><option value="owner">Owner</option><option value="hospital_admin">Hospital admin</option>
        </select>
        <input placeholder="Temporary password (10+ characters)" type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} style={{ width: 240 }} />
        <button disabled={busy}>Add person</button>
      </form>
      <ErrorNote error={error} />
    </section>
  );
}
