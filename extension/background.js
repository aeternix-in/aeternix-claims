// Service worker. The only part of the extension that holds the sign-in token or talks to Aeternix.
// Portal pages talk to it through messages; they never see the token.
import { base, errorText, matches, originPattern, settings } from "./shared.js";

// The only case actions a portal page may ask for. Everything else is done in the Aeternix app.
const PAGE_ACTIONS = new Set(["submit_claim"]);
const PENDING_MAX_AGE_MS = 6 * 60 * 60 * 1000;

async function api(method, path, body) {
  const s = await settings();
  if (!s.token) throw new Error("Not signed in. Open the extension's settings and sign in to Aeternix.");
  const headers = { Authorization: `Bearer ${s.token}` };
  if (s.hospitalId) headers["X-Hospital-Id"] = s.hospitalId;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(base(s.backend) + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new Error(`Could not reach Aeternix at ${base(s.backend)}. Check the address in settings.`);
  }
  if (res.status === 401) {
    await chrome.storage.local.set({ token: null });
    throw new Error("The sign-in has expired. Sign in again from the extension's settings.");
  }
  const json = (res.headers.get("content-type") || "").includes("json");
  const data = json ? await res.json() : await res.text();
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

async function fileAsBase64(documentId) {
  const s = await settings();
  const headers = { Authorization: `Bearer ${s.token}` };
  if (s.hospitalId) headers["X-Hospital-Id"] = s.hospitalId;
  const res = await fetch(`${base(s.backend)}/api/documents/${encodeURIComponent(documentId)}/file`, { headers });
  if (!res.ok) throw new Error(`Could not fetch a document from Aeternix (${res.status}).`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return { base64: btoa(bin), mime: res.headers.get("content-type") || "application/octet-stream" };
}

/** Fetch selector configs and register the page engine on every portal that is ready and allowed. */
export async function syncPortals() {
  const s = await settings();
  const old = await chrome.scripting.getRegisteredContentScripts();
  if (old.length) await chrome.scripting.unregisterContentScripts({ ids: old.map((x) => x.id) });
  if (!s.token) return [];  // signed out: the engine runs nowhere
  const configs = await api("GET", "/api/adapters");
  await chrome.storage.local.set({ adapters: configs, adaptersAt: Date.now() });
  const out = [];
  for (const c of configs) {
    const row = { portal: c.portal, name: c.name, status: c.status, match: c.match || [], allowed: false };
    if (c.status === "ready" && row.match.length) {
      row.allowed = await chrome.permissions.contains({ origins: row.match.map(originPattern) });
      if (row.allowed) {
        await chrome.scripting.registerContentScripts([{
          id: `portal-${c.portal}`, matches: row.match, js: ["content.js"], runAt: "document_idle",
          persistAcrossSessions: true,
        }]);
      }
    }
    out.push(row);
  }
  return out;
}

async function adapters() {
  let { adapters: list, adaptersAt } = await chrome.storage.local.get(["adapters", "adaptersAt"]);
  if (!list || !adaptersAt || Date.now() - adaptersAt > 15 * 60 * 1000) {
    try { await syncPortals(); ({ adapters: list } = await chrome.storage.local.get("adapters")); } catch (e) { /* keep cache */ }
  }
  return list || [];
}

async function handle(msg, sender) {
  switch (msg.type) {
    case "config": {
      const list = await adapters();
      const url = sender.tab ? sender.tab.url : msg.url;
      const s = await settings();
      return { adapter: list.find((c) => c.status === "ready" && matches(c.match, url)) || null,
               autoRead: s.autoRead, signedIn: !!s.token, user: s.user ? s.user.name : null };
    }
    case "snapshots":
      return api("POST", "/api/tracker/snapshots", { portal: msg.portal, rows: (msg.rows || []).slice(0, 500) });
    case "searchCases": {
      const q = encodeURIComponent(String(msg.q || "").slice(0, 80));
      return api("GET", `/api/cases?state=READY_TO_SUBMIT,LATE_CONDONATION,DISCHARGED&q=${q}&limit=20`);
    }
    case "prefill":
      return api("GET", `/api/cases/${encodeURIComponent(msg.caseId)}/prefill?portal=${encodeURIComponent(msg.portal)}`);
    case "file":
      return fileAsBase64(msg.documentId);
    case "pending:set":
      await chrome.storage.local.set({ pending: { caseId: msg.caseId, caseNo: msg.caseNo, portal: msg.portal, at: Date.now() } });
      return { ok: true };
    case "pending:get": {
      const { pending } = await chrome.storage.local.get("pending");
      return pending && Date.now() - pending.at < PENDING_MAX_AGE_MS ? pending : null;
    }
    case "pending:clear":
      await chrome.storage.local.remove("pending");
      return { ok: true };
    case "action":
      if (!PAGE_ACTIONS.has(msg.event)) throw new Error("That action is done in the Aeternix app, not from a portal page.");
      return api("POST", `/api/cases/${encodeURIComponent(msg.caseId)}/actions/${msg.event}`, msg.body || {});
    case "activity":
      return api("POST", `/api/cases/${encodeURIComponent(msg.caseId)}/activity`, { seconds: Math.min(300, Math.max(1, msg.seconds | 0)) });
    case "sync":
      return { portals: await syncPortals() };
    default:
      throw new Error(`Unknown request: ${msg.type}`);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  handle(msg || {}, sender).then(reply, (e) => reply({ error: e.message || String(e) }));
  return true;
});

chrome.runtime.onInstalled.addListener(() => { syncPortals().catch(() => {}); });
chrome.runtime.onStartup.addListener(() => { syncPortals().catch(() => {}); });
