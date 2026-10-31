import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorNote, useAction, useLoad } from "../components/ui";
import { day, rupees } from "../format";

const TABS = [
  { key: "proposed", label: "To confirm", hint: "Matches the ladder found. Confirming one moves the claim to Credited." },
  { key: "unmatched", label: "Unmatched", hint: "Credits no claim explains yet. Unmatched money is invisible money." },
  { key: "matched,ignored", label: "Done", hint: "" },
];
const METHOD: Record<string, string> = { exact_ref: "reference in narration", amount_date: "amount, date and payer",
  split: "one credit, several claims", manual: "chosen by hand" };

export default function Bank() {
  const [tab, setTab] = useState("proposed");
  const list = useLoad(() => api.get<any[]>(`/api/bank/transactions?status=${tab}`), [tab]);
  const candidates = useLoad(() => api.get<any[]>("/api/bank/candidates"), [tab]);
  const [file, setFile] = useState<File | null>(null);
  const [account, setAccount] = useState("Main");
  const [result, setResult] = useState<any>(null);
  const { busy, error, run } = useAction();

  async function upload() {
    if (!file) return;
    const form = new FormData();
    form.append("file", file); form.append("account", account);
    const r = await run(() => api.upload("/api/bank/statements", form));
    if (r) { setResult(r); setFile(null); list.reload(); candidates.reload(); }
  }

  return (
    <>
      <div className="page-head"><div><h1>Bank</h1><p>Match every credit in the hospital's account to the claims it pays.</p></div></div>
      <section className="block">
        <header><h2>Add a statement</h2></header>
        <div className="row">
          <input type="file" accept=".csv,.xlsx,.pdf,text/csv" onChange={(e) => setFile(e.target.files?.[0] || null)} style={{ maxWidth: 320 }} />
          <input value={account} onChange={(e) => setAccount(e.target.value)} style={{ width: 140 }} aria-label="Account" />
          <button className="primary" disabled={!file || busy} onClick={upload}>{busy ? "Reading…" : "Read and match"}</button>
        </div>
        <p className="small muted" style={{ marginTop: 6 }}>CSV, Excel or PDF export from the bank. Lines already imported are skipped.</p>
        {result && <p className="notice small">{result.added} new credits read ({result.duplicates} already here).
          {" "}{Object.entries(result.results || {}).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")}.</p>}
        <ErrorNote error={error} />
      </section>
      <div className="row" style={{ margin: "8px 0 4px" }} role="tablist">
        {TABS.map((t) => <button key={t.key} role="tab" aria-selected={tab === t.key} className={tab === t.key ? "primary" : "quiet"} onClick={() => setTab(t.key)}>{t.label}</button>)}
      </div>
      <p className="small muted">{TABS.find((t) => t.key === tab)?.hint}</p>
      <ErrorNote error={list.error} />
      {list.data && !list.data.length && <p className="empty">Nothing here.</p>}
      {list.data?.map((t) => <Credit key={t.id} t={t} candidates={candidates.data || []} onChange={() => { list.reload(); candidates.reload(); }} />)}
    </>
  );
}

function Credit({ t, candidates, onChange }: { t: any; candidates: any[]; onChange: () => void }) {
  const [pick, setPick] = useState<Record<string, string>>({});
  const { busy, error, run } = useAction();
  const act = (path: string, body?: unknown) => run(() => api.post(path, body)).then((r) => r && onChange());
  const lines = Object.entries(pick).filter(([, v]) => v).map(([case_id, amount]) => ({ case_id, amount: Number(amount) }));
  return (
    <section className="block">
      <header>
        <div><strong className="num">{rupees(t.amount_paise)}</strong> <span className="muted">on {day(t.txn_date)}, {t.account}</span>
          <div className="small">{t.narration}{t.reference_no && <span className="muted">, ref {t.reference_no}</span>}</div></div>
        {t.match_note && <span className="small muted">{t.match_note}</span>}
      </header>
      {t.allocations.length > 0 && (
        <table><thead><tr><th>Claim</th><th>How it matched</th><th className="num">Net</th><th className="num">Tax</th><th /></tr></thead>
          <tbody>{t.allocations.map((a: any) => (
            <tr key={a.id}>
              <td><Link to={`/cases/${a.case_id}`}>{a.case_no}</Link> <span className="muted small">{a.payer}</span></td>
              <td className="small">{METHOD[a.method]}{a.method !== "manual" && `, ${Math.round(a.confidence * 100)}% sure`}</td>
              <td className="num">{rupees(a.amount_paise)}</td><td className="num">{rupees(a.tax_paise)}</td>
              <td className="row" style={{ justifyContent: "flex-end" }}>
                {a.status === "proposed" ? <>
                  <button className="primary" disabled={busy} onClick={() => act(`/api/bank/allocations/${a.id}/confirm`)}>Confirm match</button>
                  <button className="quiet" disabled={busy} onClick={() => act(`/api/bank/allocations/${a.id}/reject`)}>Not this</button>
                </> : <span className="pill green">Confirmed</span>}
              </td>
            </tr>))}
          </tbody></table>
      )}
      {t.status === "unmatched" && (
        <details style={{ marginTop: 10 }}>
          <summary>Match by hand</summary>
          <div className="table-scroll" style={{ marginTop: 8 }}>
            <table><thead><tr><th>Claim</th><th>Payer</th><th className="num">Expected net</th><th>Amount from this credit (₹)</th></tr></thead>
              <tbody>{candidates.map((c) => (
                <tr key={c.case_id}><td>{c.case_no}</td><td>{c.payer}</td><td className="num">{rupees(c.expected_net_paise)}</td>
                  <td><input inputMode="decimal" style={{ width: 140 }} value={pick[c.case_id] || ""} placeholder={(c.expected_net_paise / 100).toString()}
                             onChange={(e) => setPick({ ...pick, [c.case_id]: e.target.value })} /></td></tr>))}
              </tbody></table>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" disabled={busy || !lines.length} onClick={() => act(`/api/bank/transactions/${t.id}/allocate`, { lines })}>Match to {lines.length} claim{lines.length === 1 ? "" : "s"}</button>
            <button className="quiet" disabled={busy} onClick={() => act(`/api/bank/transactions/${t.id}/ignore`, { note: "Not a claim payment" })}>Not a claim payment</button>
          </div>
        </details>
      )}
      <ErrorNote error={error} />
    </section>
  );
}
