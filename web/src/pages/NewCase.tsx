import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { ErrorNote, useAction, useLoad } from "../components/ui";
import { istInputToIso, nowLocalInput } from "../format";

type Payer = { id: string; code: string; name: string; scheme: string };

export default function NewCase() {
  const nav = useNavigate();
  const payers = useLoad(() => api.get<Payer[]>("/api/payers"));
  const [f, setF] = useState<Record<string, any>>({ admitted_at: nowLocalInput(), sex: "", surgical: true });
  const [rates, setRates] = useState<{ code: string; description: string }[]>([]);
  const { busy, error, run } = useAction();
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));

  async function pickPayer(id: string) {
    set("payer_id", id);
    const p = payers.data?.find((x) => x.id === id);
    if (p) {
      const pack = await api.get(`/api/rulepacks/${p.scheme}`).catch(() => null);
      setRates(pack?.rate_entries?.filter((e: any) => e.category === "package") || []);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body = { ...f, admitted_at: istInputToIso(f.admitted_at), age: f.age ? Number(f.age) : null };
    const c = await run(() => api.post("/api/cases", body));
    if (c) nav(`/cases/${c.id}`);
  }

  return (
    <>
      <div className="page-head"><div><h1>New case</h1><p>Open the case at admission. The scheme's clocks start when you save.</p></div></div>
      <form onSubmit={submit} style={{ maxWidth: 760, display: "grid", gap: 14 }}>
        <div className="grid2">
          <label>Payer
            <select required value={f.payer_id || ""} onChange={(e) => pickPayer(e.target.value)}>
              <option value="" disabled>Choose the scheme or insurer</option>
              {payers.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label>Admitted at (IST)<input type="datetime-local" required value={f.admitted_at} onChange={(e) => set("admitted_at", e.target.value)} /></label>
          <label>Patient name, as on the card<input required value={f.beneficiary_name || ""} onChange={(e) => set("beneficiary_name", e.target.value)} /></label>
          <label>Card or beneficiary ID<input value={f.beneficiary_ref || ""} onChange={(e) => set("beneficiary_ref", e.target.value)} /></label>
          <label>Referral or permission no.<input value={f.referral_no || ""} onChange={(e) => set("referral_no", e.target.value)} /></label>
          <div className="grid2">
            <label>Age<input inputMode="numeric" value={f.age || ""} onChange={(e) => set("age", e.target.value.replace(/\D/g, ""))} /></label>
            <label>Sex<select value={f.sex} onChange={(e) => set("sex", e.target.value)}><option value="">—</option><option>M</option><option>F</option><option>O</option></select></label>
          </div>
          <label>Provisional diagnosis<input value={f.provisional_diagnosis || ""} onChange={(e) => set("provisional_diagnosis", e.target.value)} /></label>
          <label>ICD-10 code<input placeholder="e.g. N20.0" value={f.icd10 || ""} onChange={(e) => set("icd10", e.target.value.toUpperCase())} /></label>
          <label>Planned procedure<input value={f.planned_procedure || ""} onChange={(e) => set("planned_procedure", e.target.value)} /></label>
          <label>Package code
            <input list="packages" value={f.package_code || ""} onChange={(e) => set("package_code", e.target.value.toUpperCase())} />
            <datalist id="packages">{rates.map((r) => <option key={r.code} value={r.code}>{r.description}</option>)}</datalist>
          </label>
          <label>Treating doctor<input value={f.treating_doctor || ""} onChange={(e) => set("treating_doctor", e.target.value)} /></label>
          <label>Ward<input value={f.ward || ""} onChange={(e) => set("ward", e.target.value)} /></label>
        </div>
        <div className="row">
          <label className="check"><input type="checkbox" checked={!!f.emergency} onChange={(e) => set("emergency", e.target.checked)} />Emergency admission</label>
          <label className="check"><input type="checkbox" checked={!!f.surgical} onChange={(e) => set("surgical", e.target.checked)} />Surgical</label>
          <label className="check"><input type="checkbox" checked={!!f.implant_used} onChange={(e) => set("implant_used", e.target.checked)} />Implant or stent used</label>
          <label className="check"><input type="checkbox" checked={!!f.portability} onChange={(e) => set("portability", e.target.checked)} />Portability (PM-JAY)</label>
        </div>
        <ErrorNote error={error} />
        <div><button className="primary" disabled={busy}>{busy ? "Opening…" : "Open case"}</button></div>
      </form>
    </>
  );
}
