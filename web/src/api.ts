// Small fetch wrapper. The token and the chosen hospital live in memory and, when the
// browser allows it, in localStorage so a refresh keeps the desk signed in.

let token: string | null = read("aeternix.token");
let hospitalId: string | null = read("aeternix.hospital");

function read(k: string): string | null {
  try { return window.localStorage.getItem(k); } catch { return null; }
}
function write(k: string, v: string | null) {
  try { v === null ? window.localStorage.removeItem(k) : window.localStorage.setItem(k, v); } catch { /* private mode */ }
}

export function setToken(t: string | null) { token = t; write("aeternix.token", t); }
export function getToken() { return token; }
export function setHospital(id: string | null) { hospitalId = id; write("aeternix.hospital", id); }
export function getHospital() { return hospitalId; }

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) };
  if (token) h["Authorization"] = `Bearer ${token}`;
  if (hospitalId) h["X-Hospital-Id"] = hospitalId;
  return h;
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("aeternix:signed-out"));
  }
  if (!r.ok) {
    let msg = `Request failed (${r.status}).`;
    try {
      const j = await r.json();
      if (typeof j.detail === "string") msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch { /* not json */ }
    throw new ApiError(r.status, msg);
  }
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("application/json") ? r.json() : r.blob()) as Promise<T>;
}

export const api = {
  get: <T = any>(path: string) => fetch(path, { headers: headers() }).then((r) => handle<T>(r)),
  post: <T = any>(path: string, body?: unknown) =>
    fetch(path, { method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(body ?? {}) })
      .then((r) => handle<T>(r)),
  patch: <T = any>(path: string, body: unknown) =>
    fetch(path, { method: "PATCH", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(body) })
      .then((r) => handle<T>(r)),
  upload: <T = any>(path: string, form: FormData) =>
    fetch(path, { method: "POST", headers: headers(), body: form }).then((r) => handle<T>(r)),
  blob: (path: string) => fetch(path, { headers: headers() }).then((r) => handle<Blob>(r)),
};

export async function openBlob(path: string, filename?: string) {
  const b = await api.blob(path);
  const url = URL.createObjectURL(b);
  if (filename) {
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
  } else {
    window.open(url, "_blank", "noopener");
  }
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
