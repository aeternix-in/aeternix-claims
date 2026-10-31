// Helpers shared by the service worker, popup and options page (ES module).

export const DEFAULTS = { backend: "http://127.0.0.1:8000", token: null, hospitalId: null, user: null, autoRead: true };

export async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

export function base(url) {
  return String(url || "").trim().replace(/\/+$/, "");
}

/** Chrome match pattern -> RegExp, good enough to tell which adapter a URL belongs to. */
export function patternToRegExp(pattern) {
  const esc = pattern.split("*").map((s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*");
  return new RegExp(`^${esc}$`);
}

export function matches(patterns, url) {
  return (patterns || []).some((p) => patternToRegExp(p).test(url));
}

/** The origin form Chrome's permissions API wants for a match pattern: scheme://host/* (any port, any path). */
export function originPattern(pattern) {
  const m = /^(\*|https?):\/\/([^/:]+)(:[^/]*)?\//.exec(pattern);
  return m ? `${m[1]}://${m[2]}/*` : pattern;
}

export function errorText(data, status) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail)) return data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  return `Aeternix answered ${status}.`;
}
