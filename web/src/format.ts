const IST = "Asia/Kolkata";

export function rupees(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  const r = paise / 100;
  return "₹" + r.toLocaleString("en-IN", { minimumFractionDigits: Number.isInteger(r) ? 0 : 2, maximumFractionDigits: 2 });
}

export function toDate(v: string | null | undefined): Date | null {
  if (!v) return null;
  const d = new Date(v.length === 10 ? v + "T00:00:00+05:30" : v);
  return isNaN(d.getTime()) ? null : d;
}

export function day(v: string | null | undefined): string {
  const d = toDate(v);
  return d ? d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: IST }) : "—";
}

export function dayTime(v: string | null | undefined): string {
  const d = toDate(v);
  return d ? d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: IST }) : "—";
}

/** "3 days left", "overdue by 2 days", "5 hours left". */
export function dueIn(v: string | null | undefined): { text: string; tone: "over" | "soon" | "" } {
  const d = toDate(v);
  if (!d) return { text: "", tone: "" };
  const ms = d.getTime() - Date.now();
  const abs = Math.abs(ms);
  const hours = Math.round(abs / 36e5);
  const days = Math.round(abs / 864e5);
  const span = hours < 48 ? `${hours} hour${hours === 1 ? "" : "s"}` : `${days} days`;
  if (ms < 0) return { text: `overdue by ${span}`, tone: "over" };
  return { text: `${span} left`, tone: hours <= 72 ? "soon" : "" };
}

/** datetime-local value in IST for an input. */
export function nowLocalInput(): string {
  const d = new Date(Date.now() + 5.5 * 36e5);
  return d.toISOString().slice(0, 16);
}

export function istInputToIso(v: string): string {
  return v ? `${v}:00+05:30` : "";
}

export function percent(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v * 1000) / 10}%`;
}
