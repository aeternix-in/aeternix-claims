import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { can, useMe } from "../App";
import { Arc, ErrorNote, useLoad } from "../components/ui";
import { day, rupees } from "../format";
import Actions from "./case/Actions";
import Check from "./case/Check";
import Documents from "./case/Documents";
import Queries from "./case/Queries";
import { Clocks, Counter, Details, MoneyTrail, Portal, Timeline } from "./case/Side";

let metaCache: any = null;

export default function CaseDetail() {
  const { id } = useParams();
  const me = useMe();
  const { data: c, setData, error } = useLoad(() => api.get(`/api/cases/${id}`), [id]);
  const meta = useLoad(() => (metaCache ? Promise.resolve(metaCache) : api.get("/api/meta").then((m) => (metaCache = m))));
  const desk = can(me, "desk");
  const clin = can(me, "clinical");

  // Desk minutes per claim: count time this screen is open and visible.
  useEffect(() => {
    if (!id || !["desk", "owner", "hospital_admin"].includes(me.user.role)) return;
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") api.post(`/api/cases/${id}/activity`, { seconds: 30 }).catch(() => undefined);
    }, 30_000);
    return () => window.clearInterval(t);
  }, [id, me.user.role]);

  if (error) return <ErrorNote error={error} />;
  if (!c || !meta.data) return <p className="muted">Loading case…</p>;

  const m = c.money;
  const subs = [
    day(c.admitted_at),
    c.discharged_at ? `discharged ${day(c.discharged_at)}` : null,
    c.queries.length ? `${c.queries.filter((q: any) => !["replied", "closed"].includes(q.status)).length} open` : null,
    m.approved_paise !== null ? `${rupees(m.approved_paise)} approved` : c.claims.find((x: any) => x.stage === "final") ? "submitted" : null,
    m.credited_paise ? `${rupees(m.credited_paise)} credited` : null,
  ];

  return (
    <>
      <div className="case-head">
        <div>
          <p className="small"><Link to="/cases">Cases</Link></p>
          <h1>{c.case_no}</h1>
          <div className="meta">{c.beneficiary_name}, {c.payer.name}{c.portal_case_id ? `, portal ref ${c.portal_case_id}` : ""}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <span className="pill ink">{c.state_label}</span>
          {c.buckets.length > 0 && <div className="small muted" style={{ marginTop: 6 }}>Money bucket {c.buckets.join(" and ")}</div>}
          {c.imported && <div className="small muted">Imported from the hospital's list</div>}
        </div>
      </div>
      <Arc station={c.station} state={c.state_label} closed={c.state === "CLOSED"} subs={subs} />
      <div className="case-cols">
        <div>
          {(c.discharged_at || c.documents.length > 0) && <Documents c={c} docTypes={meta.data.doc_types} onChange={setData} canEdit={desk} />}
          {(c.discharged_at || c.check.results.length > 0) && <Check c={c} onChange={setData} canRun={desk} isClinician={clin} />}
          <Queries c={c} categories={meta.data.query_categories} onChange={setData} canEdit={desk} isClinician={clin} />
          <MoneyTrail c={c} />
          {c.escalations.length > 0 && (
            <section className="block">
              <header><h2>Letters</h2><Link to="/letters">All letters</Link></header>
              {c.escalations.map((e: any) => <p key={e.id}><span className="pill">{e.status}</span> {e.subject}</p>)}
            </section>
          )}
          <Timeline c={c} />
        </div>
        <aside className="side">
          <section className="block"><header><h2>Next step</h2></header>
            {desk ? <Actions c={c} onChange={setData} paper={c.rule_pack.portal === "paper"} /> : <p className="small muted">Your role can view this case.</p>}
          </section>
          <Counter c={c} onChange={setData} canEdit={desk} />
          <Clocks c={c} />
          <Portal c={c} />
          <Details c={c} onChange={setData} canEdit={desk} />
        </aside>
      </div>
    </>
  );
}
