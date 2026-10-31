import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorNote, useAction, useLoad } from "../components/ui";
import { percent, rupees } from "../format";

const SWATCH: Record<string, string> = {
  A: "#000000", B: "#3d4a55", S: "#8a96a1", C: "#17599a", D: "#b42318",
  E: "repeating-linear-gradient(135deg, #a9cfee 0 6px, #ffffff 6px 9px)", F: "#d5e8f7",
};

export default function Owner() {
  const dash = useLoad(() => api.get("/api/dashboard"));
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const score = useLoad(() => api.get<any[]>(`/api/scorecard?month=${month}`), [month]);
  const metrics = useLoad(() => api.get("/api/metrics/pilot"));
  const digest = useLoad(() => api.get("/api/digest/preview"));
  const send = useAction();
  const d = dash.data;
  const total = d?.total_owed_paise || 0;

  return (
    <>
      <div className="page-head"><div><h1>Owner view</h1><p>Where the hospital's money is, and which part of it Aeternix can move.</p></div></div>
      <ErrorNote error={dash.error} />
      {d && (
        <>
          <section className="block">
            <div className="owed"><span className="big">{rupees(total)}</span><span className="muted">owed to the hospital across {d.buckets.reduce((s: number, b: any) => s + b.cases, 0)} claims</span></div>
            <div className="bar" role="img" aria-label="Money owed by bucket">
              {d.buckets.filter((b: any) => b.amount_paise).map((b: any) => (
                <span key={b.key} title={`${b.label}: ${rupees(b.amount_paise)}`} style={{ width: `${(b.amount_paise / total) * 100}%`, background: SWATCH[b.key] }} />
              ))}
            </div>
            <div className="legend">
              {d.buckets.map((b: any) => (
                <Link key={b.key} to={`/cases?bucket=${b.key}`} style={{ color: "inherit", textDecoration: "none" }}>
                  <span className="sw" style={{ background: SWATCH[b.key] }} />{b.label}: <strong className="num">{rupees(b.amount_paise)}</strong>
                  <span className="muted"> ({b.cases})</span>
                </Link>
              ))}
            </div>
            <p className="small muted" style={{ marginTop: 10 }}>The striped part, approved but unpaid, waits on the payer's sanction and funds. Aeternix tracks and escalates it; it cannot release it.</p>
            {d.unmatched_credits.count > 0 && <p className="notice small"><Link to="/bank">{d.unmatched_credits.count} bank credits ({rupees(d.unmatched_credits.amount_paise)})</Link> are not yet matched to claims.</p>}
          </section>

          <section className="block">
            <header><h2>By payer</h2></header>
            <div className="table-scroll"><table>
              <thead><tr><th>Payer</th>{d.buckets.map((b: any) => <th key={b.key} className="num" title={b.label}>{b.key}</th>)}<th className="num">Total</th></tr></thead>
              <tbody>{d.by_payer.map((p: any) => (
                <tr key={p.code}><td>{p.payer}</td>{d.buckets.map((b: any) => <td key={b.key} className="num">{p[b.key] ? rupees(p[b.key]) : ""}</td>)}<td className="num"><strong>{rupees(p.total)}</strong></td></tr>
              ))}</tbody>
            </table></div>
            <p className="small muted">{d.buckets.map((b: any) => `${b.key} ${b.label.toLowerCase()}`).join("; ")}.</p>
          </section>

          <section className="block">
            <header><h2>Age since discharge</h2></header>
            <div className="table-scroll"><table>
              <thead><tr><th>Bucket</th>{d.ageing[0].bands.map((b: any) => <th key={b.band} className="num">{b.band}</th>)}</tr></thead>
              <tbody>{d.ageing.filter((a: any) => a.bands.some((b: any) => b.amount_paise)).map((a: any) => (
                <tr key={a.bucket}><td>{a.label}</td>{a.bands.map((b: any) => <td key={b.band} className="num">{b.amount_paise ? rupees(b.amount_paise) : ""}</td>)}</tr>
              ))}</tbody>
            </table></div>
          </section>

          <section className="block">
            <header><h2>Oldest money</h2></header>
            <table><tbody>{d.oldest.map((o: any) => (
              <tr key={o.case_id}><td><Link to={`/cases/${o.case_id}`}>{o.case_no}</Link></td><td>{o.payer}</td><td>{o.state_label}</td>
                <td className="num">{o.age_days} days</td><td className="num">{rupees(o.amount_paise)}</td></tr>
            ))}</tbody></table>
          </section>
        </>
      )}

      <section className="block">
        <header><h2>Payer scorecard</h2><input type="month" value={month} onChange={(e) => setMonth(e.target.value)} style={{ width: 170 }} aria-label="Month" /></header>
        <ErrorNote error={score.error} />
        {score.data && !score.data.length && <p className="empty">No claims moved this month.</p>}
        {score.data && score.data.length > 0 && <div className="table-scroll"><table>
          <thead><tr><th>Payer</th><th className="num">Claims submitted</th><th className="num">Median days to pay</th><th className="num">Deduction rate</th><th className="num">Queries per claim</th><th className="num">Rejections</th></tr></thead>
          <tbody>{score.data.map((s) => (
            <tr key={s.code}><td>{s.payer}</td><td className="num">{s.claims_submitted}</td><td className="num">{s.median_days_to_pay ?? "—"}</td>
              <td className="num">{percent(s.deduction_rate)}</td><td className="num">{s.query_rate ?? "—"}</td><td className="num">{s.rejections}</td></tr>
          ))}</tbody></table></div>}
      </section>

      {metrics.data && (
        <section className="block">
          <header><h2>Pilot measures</h2><span className="small muted">Measured, not promised. Days to payment depend on the payer.</span></header>
          <div className="metric-grid">
            <M v={percent(metrics.data.share_within_window)} l="Claims submitted inside the window" />
            <M v={metrics.data.median_days_discharge_to_submission ?? "—"} l="Median days, discharge to submission" />
            <M v={metrics.data.median_query_reply_hours !== null ? `${metrics.data.median_query_reply_hours} h` : "—"} l="Median query reply time" />
            <M v={percent(metrics.data.share_queries_on_time)} l="Queries answered on time" />
            <M v={percent(metrics.data.first_pass_approval_rate)} l="First-pass approval rate" />
            <M v={rupees(metrics.data.recovered_over_90_days_paise)} l="Recovered from claims over 90 days old" />
            <M v={percent(metrics.data.share_credits_matched)} l="Bank credits matched to claims" />
            <M v={metrics.data.desk_minutes_per_claim !== null ? `${metrics.data.desk_minutes_per_claim} min` : "—"} l="Desk minutes per claim" />
          </div>
        </section>
      )}

      {digest.data && (
        <section className="block">
          <header><h2>Tomorrow's 8 am digest</h2>
            <button className="quiet" disabled={send.busy} onClick={() => send.run(() => api.post("/api/digest/send"))}>Send it now</button></header>
          <pre className="digest">{digest.data.text}</pre>
          <p className="small muted">Case numbers only. Names, diagnoses and amounts tied to names never leave the system in a message.</p>
          <ErrorNote error={send.error} />
        </section>
      )}
    </>
  );
}

function M({ v, l }: { v: React.ReactNode; l: string }) {
  return <div className="metric"><div className="v">{v}</div><div className="l">{l}</div></div>;
}
