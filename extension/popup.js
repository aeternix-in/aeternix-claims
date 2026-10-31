import { base, matches, originPattern, settings } from "./shared.js";

const $ = (id) => document.getElementById(id);
const button = (text, fn, primary) => {
  const b = document.createElement("button");
  b.textContent = text;
  if (primary) b.className = "primary";
  b.onclick = fn;
  $("buttons").appendChild(b);
};

(async () => {
  const s = await settings();
  if (!s.token) {
    $("who").textContent = "Not signed in.";
    $("page").textContent = "Sign in to Aeternix to read claim lists and fill claim forms.";
    button("Sign in", () => chrome.runtime.openOptionsPage(), true);
    return;
  }
  const hospital = ((await chrome.storage.local.get("hospitals")).hospitals || []).find((h) => h.id === s.hospitalId);
  $("who").textContent = `${s.user ? s.user.name : "Signed in"}${hospital ? `, ${hospital.name}` : ""}`;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = (tab && tab.url) || "";
  let { adapters } = await chrome.storage.local.get("adapters");
  if (!adapters) {
    try { adapters = (await chrome.runtime.sendMessage({ type: "sync" })).portals; } catch (e) { adapters = []; }
  }
  const a = (adapters || []).find((x) => matches(x.match, url));
  if (!a) {
    $("page").textContent = "This page is not a payer portal Aeternix knows.";
  } else if (a.status !== "ready") {
    $("page").textContent = `${a.name}: not set up yet.`;
    $("hint").textContent = "Its page selectors have not been captured, so the extension does nothing here. " +
      "Capture them from a signed-in desk session (docs/ADAPTERS.md), then refresh in settings.";
  } else {
    const allowed = await chrome.permissions.contains({ origins: a.match.map(originPattern) });
    $("page").textContent = `${a.name}: ready.`;
    if (allowed) {
      $("hint").textContent = "Open the claim list to read statuses, or the claim form to fill it from a case.";
    } else {
      $("hint").textContent = "The extension needs your permission to work on this portal.";
      button("Allow on this portal", async () => {
        if (await chrome.permissions.request({ origins: a.match.map(originPattern) })) {
          await chrome.runtime.sendMessage({ type: "sync" });
          if (tab) chrome.tabs.reload(tab.id);
          window.close();
        }
      }, true);
    }
  }
  button("Open Aeternix", () => chrome.tabs.create({ url: base(s.backend) + "/" }));
  button("Settings", () => chrome.runtime.openOptionsPage());
})();
