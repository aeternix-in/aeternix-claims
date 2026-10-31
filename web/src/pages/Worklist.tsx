import { Link } from "react-router-dom";
import { api } from "../api";
import { Due, ErrorNote, useLoad } from "../components/ui";
import { rupees } from "../format";

type Item = {
  kind: string; priority: number; case_id: string | null; case_no: string | null; payer: string | null;
  title: string; detail: string | null; due_at: string | null; overdue: boolean; amount_paise: number | null;
  bank_txn_id?: string;
};

const GROUPS: { key: string; title: string; test: (i: Item) => boolean }[] = [
  { key: "now", title: "Overdue or due now", test: (i) => i.overdue || i.priority === 0 },
  { key: "today", title: "Do today", test: (i) => i.priority === 1 },
  { key: "next", title: "Next", test: (i) => i.priority === 2 },
  { key: "wait", title: "Waiting on someone else", test: (i) => i.priority >= 3 },
];

export default function Worklist() {
  const { data, error, reload } = useLoad(() => api.get<Item[]>("/api/worklist"));
  const items = data || [];
  const used = new Set<Item>();
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Worklist</h1>
          <p>{data ? `${items.length} things to move today, most urgent first.` : "Loading…"}</p>
        </div>
        <button className="quiet" onClick={() => reload()}>Refresh</button>
      </div>
      <ErrorNote error={error} />
      {data && items.length === 0 && <p className="empty">Nothing is due. Open a case for today's admissions from New case.</p>}
      {GROUPS.map((g) => {
        const rows = items.filter((i) => !used.has(i) && g.test(i));
        rows.forEach((r) => used.add(r));
        if (!rows.length) return null;
        return (
          <div key={g.key} className="work-group">
            <h2>{g.title}</h2>
            {rows.map((i, n) => (
              <Link key={n} className="work-item" to={i.case_id ? `/cases/${i.case_id}` : "/bank"}>
                <div>
                  <div className="title">{i.title}</div>
                  <div className="who">
                    {i.case_no ? <>{i.case_no}, {i.payer}</> : "Bank credit"}
                    {i.detail && <> — {i.detail}</>}
                  </div>
                </div>
                <div className="right">
                  <Due at={i.due_at} />
                  <span className="num small">{rupees(i.amount_paise)}</span>
                </div>
              </Link>
            ))}
          </div>
        );
      })}
    </>
  );
}
