import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Arc, CheckPill, ErrorNote, useLoad } from "../components/ui";
import { day, rupees } from "../format";

export type CaseRow = {
  id: string; case_no: string; payer: { code: string; name: string }; beneficiary_name: string; state: string;
  state_label: string; station: number; admitted_at: string; discharged_at: string | null; billed_paise: number | null;
  portal_case_id: string | null; check_status: string | null; buckets: string[];
};

export default function Cases() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const bucket = params.get("bucket") || "";
  const closed = params.get("closed") === "1";
  const nav = useNavigate();
  const { data, error } = useLoad(
    () => api.get<CaseRow[]>(`/api/cases?q=${encodeURIComponent(params.get("q") || "")}&bucket=${bucket}&open_only=${!closed}`),
    [params.toString()]);
  const set = (k: string, v: string) => { const p = new URLSearchParams(params); v ? p.set(k, v) : p.delete(k); setParams(p); };

  return (
    <>
      <div className="page-head">
        <div><h1>Cases</h1><p>{data ? `${data.length} cases` : "Loading…"}</p></div>
        <form className="row" onSubmit={(e) => { e.preventDefault(); set("q", q); }}>
          <input style={{ width: 260 }} placeholder="Case no, patient, card or portal ref" value={q}
                 onChange={(e) => setQ(e.target.value)} aria-label="Search cases" />
          <select value={bucket} onChange={(e) => set("bucket", e.target.value)} aria-label="Money bucket" style={{ width: 230 }}>
            <option value="">Every bucket</option>
            <option value="A">A. Not yet submitted</option>
            <option value="B">B. Late, needs condonation</option>
            <option value="S">With payer, awaiting decision</option>
            <option value="C">C. In query</option>
            <option value="D">D. Deducted, short-paid or rejected</option>
            <option value="E">E. Approved but unpaid</option>
            <option value="F">F. Paid, not matched in bank</option>
          </select>
          <label className="check"><input type="checkbox" checked={closed} onChange={(e) => set("closed", e.target.checked ? "1" : "")} />Include closed</label>
        </form>
      </div>
      <ErrorNote error={error} />
      <div className="table-scroll">
        <table>
          <thead><tr><th>Case</th><th>Patient</th><th>Payer</th><th>Where it is</th><th>Check</th><th>Admitted</th><th className="num">Amount</th></tr></thead>
          <tbody>
            {data?.map((c) => (
              <tr key={c.id} className="clickable" onClick={() => nav(`/cases/${c.id}`)}>
                <td><a href={`/cases/${c.id}`} onClick={(e) => e.preventDefault()}>{c.case_no}</a>
                  {c.portal_case_id && <div className="small muted">{c.portal_case_id}</div>}</td>
                <td>{c.beneficiary_name}</td>
                <td>{c.payer.code}</td>
                <td><Arc compact station={c.station} closed={c.state === "CLOSED"} /><div className="small">{c.state_label}</div></td>
                <td>{["DISCHARGED", "READY_TO_SUBMIT", "LATE_CONDONATION"].includes(c.state) ? <CheckPill status={c.check_status} /> : null}</td>
                <td className="num">{day(c.admitted_at)}</td>
                <td className="num">{rupees(c.billed_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && !data.length && <p className="empty">No cases match. Clear the search or open a new case.</p>}
      </div>
    </>
  );
}
