import { base, errorText, originPattern, settings } from "./shared.js";

const $ = (id) => document.getElementById(id);
const bg = async (type, payload = {}) => {
  const r = await chrome.runtime.sendMessage({ type, ...payload });
  if (r && r.error) throw new Error(r.error);
  return r;
};

function isLocal(url) {
  try { return ["localhost", "127.0.0.1"].includes(new URL(url).hostname); } catch (e) { return false; }
}

async function render() {
  const s = await settings();
  $("backend").value = s.backend;
  $("signin").hidden = !!s.token;
  $("account").hidden = !s.token;
  $("portals").hidden = !s.token;
  if (!s.token) return;
  const { hospitals = [] } = await chrome.storage.local.get("hospitals");
  const hospital = hospitals.find((h) => h.id === s.hospitalId);
  $("who").textContent = `${s.user ? `${s.user.name} (${s.user.email})` : "Signed in"}${hospital && s.user && s.user.hospital_id ? `, ${hospital.name}` : ""}, at ${base(s.backend)}`;
  $("autoRead").checked = !!s.autoRead;
  await renderHospitals(s);
  await renderPortals();
}

async function renderHospitals(s) {
  const { hospitals = [] } = await chrome.storage.local.get("hospitals");
  const pickable = !(s.user && s.user.hospital_id) && hospitals.length > 1;
  $("hospitalWrap").hidden = !pickable;
  if (!pickable) return;
  $("hospital").replaceChildren(...hospitals.map((h) => new Option(h.name, h.id, false, h.id === s.hospitalId)));
}

async function renderPortals() {
  $("portalErr").textContent = "";
  let portals = [];
  try { portals = (await bg("sync")).portals; } catch (e) { $("portalErr").textContent = e.message; }
  const rows = portals.map((p) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = p.name;
    const status = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `tag ${p.status === "ready" ? "ready" : ""}`;
    tag.textContent = p.status === "ready" ? "Ready" : "Not captured yet";
    status.appendChild(tag);
    const access = document.createElement("td");
    if (p.status !== "ready") access.textContent = "—";
    else if (p.allowed) access.textContent = "Allowed";
    else {
      const b = document.createElement("button");
      b.textContent = "Allow";
      b.onclick = async () => {
        const ok = await chrome.permissions.request({ origins: p.match.map(originPattern) });
        if (ok) await renderPortals();
      };
      access.appendChild(b);
    }
    tr.append(name, status, access);
    return tr;
  });
  $("portalRows").replaceChildren(...rows);
}

$("signinForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("signinErr").textContent = "";
  const backend = base($("backend").value);
  $("signinBtn").disabled = true;
  try {
    if (!isLocal(backend)) {
      const ok = await chrome.permissions.request({ origins: [`${new URL(backend).origin}/*`] });
      if (!ok) throw new Error("The extension needs permission to reach your Aeternix address.");
    }
    const res = await fetch(`${backend}/api/auth/login`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("email").value, password: $("password").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errorText(data, res.status));
    const me = await (await fetch(`${backend}/api/auth/me`, { headers: { Authorization: `Bearer ${data.token}` } })).json();
    const hospitalId = me.user.hospital_id || (me.hospitals[0] && me.hospitals[0].id) || null;
    await chrome.storage.local.set({ backend, token: data.token, user: me.user, hospitals: me.hospitals, hospitalId,
                                     adapters: null, adaptersAt: 0 });
    $("password").value = "";
    await render();
  } catch (err) {
    $("signinErr").textContent = err.message || String(err);
  } finally { $("signinBtn").disabled = false; }
});

$("signout").onclick = async () => {
  await chrome.storage.local.set({ token: null, user: null, hospitals: [], hospitalId: null, pending: null });
  try { await bg("sync"); } catch (e) { /* signed out: nothing to sync */ }
  await render();
};
$("hospital").onchange = async () => { await chrome.storage.local.set({ hospitalId: $("hospital").value }); };
$("autoRead").onchange = async () => { await chrome.storage.local.set({ autoRead: $("autoRead").checked }); };
$("refresh").onclick = renderPortals;

render();
