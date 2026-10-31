import { useState } from "react";
import { api, openBlob } from "../../api";
import { ErrorNote, useAction } from "../../components/ui";
import { day, rupees } from "../../format";

const FIELD_LABELS: Record<string, string> = {
  patient_name: "Patient name", admission_date: "Admission date", discharge_date: "Discharge date",
  diagnosis: "Diagnosis", signed: "Signed by doctor", bill_no: "Bill no.", bill_date: "Bill date",
  total_amount: "Total (₹)", referral_no: "Referral no.", beneficiary_ref: "Card no.",
  implant_description: "Implant", report_date: "Report date", procedure_date: "Procedure date",
};

export default function Documents({ c, docTypes, onChange, canEdit }: {
  c: any; docTypes: Record<string, string>; onChange: (x: any) => void; canEdit: boolean;
}) {
  const [over, setOver] = useState(false);
  const { busy, error, run } = useAction();
  const docs = (c.documents as any[]).filter((d) => !d.voided);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    const form = new FormData();
    Array.from(files).forEach((f) => form.append("files", f));
    form.append("source", "upload");
    const r = await run(() => api.upload(`/api/cases/${c.id}/documents`, form));
    if (r) onChange(r.case);
  }

  return (
    <section className="block">
      <header>
        <h2>Documents</h2>
        <span className="muted small">{docs.length} on file; {docs.filter((d) => d.needs_verification.length).length} to confirm</span>
      </header>
      {canEdit && (
        <div className={`dropzone${over ? " over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }}
             onDragLeave={() => setOver(false)} onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}>
          <p>Drop scans or photos here, or</p>
          <label className="btn" style={{ display: "inline-flex", cursor: "pointer" }}>
            Choose files
            <input type="file" multiple accept="application/pdf,image/*" style={{ display: "none" }}
                   onChange={(e) => upload(e.target.files)} />
          </label>
          <p className="small muted" style={{ marginTop: 8 }}>{busy ? "Reading documents…" : "Each page is read, sorted by type, and its fields filled in for you to confirm."}</p>
        </div>
      )}
      <ErrorNote error={error} />
      <div style={{ marginTop: 14 }}>
        {docs.map((d) => <Doc key={d.id} d={d} docTypes={docTypes} onChange={onChange} canEdit={canEdit} />)}
      </div>
    </section>
  );
}

function initial(d: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const k of d.key_fields as string[]) {
    const v = d.verified_fields[k] ?? d.extracted[k]?.value ?? "";
    out[k] = k === "total_amount" && typeof v === "number" ? (v / 100).toFixed(2) : v;  // edited as rupees
  }
  if (d.doc_type === "final_bill") out.bill_lines = d.verified_fields.bill_lines ?? d.extracted.bill_lines?.value ?? [];
  return out;
}

function Doc({ d, docTypes, onChange, canEdit }: { d: any; docTypes: Record<string, string>; onChange: (x: any) => void; canEdit: boolean }) {
  const [vals, setVals] = useState<Record<string, any>>(() => initial(d));
  const [editing, setEditing] = useState(d.needs_verification.length > 0);
  const { busy, error, run } = useAction();
  const pending = d.needs_verification as string[];

  async function confirm() {
    const fields: Record<string, any> = { ...vals };  // total_amount goes as rupee text; the server converts it
    if (fields.bill_lines) fields.bill_lines = fields.bill_lines.map((l: any) => ({ ...l, qty: Number(l.qty) || 1, amount_paise: Math.round(Number(l.amount_paise)) }));
    const r = await run(() => api.post(`/api/documents/${d.id}/verify`, { fields }));
    if (r) { onChange(r); setEditing(false); }
  }
  async function reclassify(t: string) {
    const r = await run(() => api.patch(`/api/documents/${d.id}`, { doc_type: t }));
    if (r) onChange(r);
  }
  async function voidDoc() {
    const r = await run(() => api.post(`/api/documents/${d.id}/void`, { reason: "Removed by desk" }));
    if (r) onChange(r);
  }

  const shown = (k: string, v: any) => k === "total_amount" && typeof v === "number" ? rupees(v) : k === "signed" ? (v === true || v === "true" ? "Yes" : v === false || v === "false" ? "No" : "—") : String(v ?? "—");

  return (
    <div className={`doc${pending.length ? " need" : ""}`}>
      <header>
        <div className="row">
          {canEdit ? (
            <select value={d.doc_type} onChange={(e) => reclassify(e.target.value)} style={{ width: "auto" }} aria-label="Document type">
              {Object.entries(docTypes).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          ) : <strong>{d.doc_label}</strong>}
          <button className="link" onClick={() => openBlob(`/api/documents/${d.id}/file`)}>{d.filename}</button>
        </div>
        <div className="row small muted">
          {d.ocr_method === "ocr" ? "Read by OCR" : d.ocr_method === "pdf_text" ? "Digital PDF" : d.ocr_method}
          {d.classification_confidence !== null && d.classification_confidence < 0.6 && <span className="pill amber">Check the type</span>}
          {pending.length ? <span className="pill amber">{pending.length} to confirm</span> : d.key_fields.length ? <span className="pill green">Confirmed</span> : null}
        </div>
      </header>
      {d.key_fields.length > 0 && (editing && canEdit ? (
        <>
          <div className="fields">
            {(d.key_fields as string[]).map((k) => (
              <FieldInput key={k} k={k} v={vals[k]} onChange={(v) => setVals({ ...vals, [k]: v })} />
            ))}
          </div>
          {d.doc_type === "final_bill" && <BillLines lines={vals.bill_lines || []} onChange={(l) => setVals({ ...vals, bill_lines: l })} />}
          <ErrorNote error={error} />
          <div className="row" style={{ marginTop: 10 }}>
            <button className="primary" onClick={confirm} disabled={busy}>Confirm fields</button>
            <button className="quiet" onClick={voidDoc} disabled={busy}>Remove from case</button>
          </div>
        </>
      ) : (
        <div className="fields">
          {(d.key_fields as string[]).map((k) => (
            <FieldRow key={k} k={k} v={shown(k, d.verified_fields[k] ?? d.extracted[k]?.value)} />
          ))}
          {d.doc_type === "final_bill" && (d.verified_fields.bill_lines || []).length > 0 && (
            <><span className="k">Bill lines</span><span>{d.verified_fields.bill_lines.length} lines confirmed</span></>
          )}
          {canEdit && <><span /><span><button className="link" onClick={() => setEditing(true)}>Edit fields</button></span></>}
        </div>
      ))}
    </div>
  );
}

function FieldRow({ k, v }: { k: string; v: string }) {
  return <><span className="k">{FIELD_LABELS[k] || k}</span><span>{k.endsWith("_date") && v !== "—" ? day(v) : v}</span></>;
}

function FieldInput({ k, v, onChange }: { k: string; v: any; onChange: (v: any) => void }) {
  const label = FIELD_LABELS[k] || k;
  if (k === "signed") {
    return <><span className="k">{label}</span>
      <select value={v === true || v === "true" ? "true" : v === false || v === "false" ? "false" : ""} onChange={(e) => onChange(e.target.value === "true")}>
        <option value="" disabled>Choose</option><option value="true">Yes, signed</option><option value="false">No signature</option>
      </select></>;
  }
  if (k.endsWith("_date")) return <><span className="k">{label}</span><input type="date" value={v || ""} onChange={(e) => onChange(e.target.value)} /></>;
  if (k === "total_amount") {
    return <><span className="k">{label}</span><input inputMode="decimal" value={v ?? ""} onChange={(e) => onChange(e.target.value)} /></>;
  }
  return <><span className="k">{label}</span><input value={v ?? ""} onChange={(e) => onChange(e.target.value)} /></>;
}

function BillLines({ lines, onChange }: { lines: any[]; onChange: (l: any[]) => void }) {
  const set = (i: number, k: string, v: any) => onChange(lines.map((l, n) => (n === i ? { ...l, [k]: v } : l)));
  return (
    <div style={{ marginTop: 12 }}>
      <div className="small muted" style={{ marginBottom: 4 }}>Bill lines (amounts in ₹)</div>
      <div className="table-scroll">
        <table>
          <thead><tr><th>Code</th><th>Description</th><th className="num">Qty</th><th className="num">Amount</th><th /></tr></thead>
          <tbody>
            {lines.map((l, i) => (
              <tr key={i}>
                <td><input value={l.code} onChange={(e) => set(i, "code", e.target.value.toUpperCase())} /></td>
                <td><input value={l.description} onChange={(e) => set(i, "description", e.target.value)} /></td>
                <td style={{ width: 70 }}><input inputMode="numeric" value={l.qty} onChange={(e) => set(i, "qty", e.target.value)} /></td>
                <td style={{ width: 130 }}><input inputMode="decimal" value={(Number(l.amount_paise) / 100).toString()} onChange={(e) => set(i, "amount_paise", Math.round(Number(e.target.value) * 100))} /></td>
                <td><button className="link" onClick={() => onChange(lines.filter((_, n) => n !== i))}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="link" onClick={() => onChange([...lines, { code: "", description: "", qty: 1, amount_paise: 0 }])}>Add a line</button>
    </div>
  );
}
