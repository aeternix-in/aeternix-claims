// Page engine. Runs only on portals whose selectors are captured (status: ready) and allowed.
// Reads claim lists, fills claim forms, reads the confirmation reference. It never clicks Submit.
(() => {
  if (window.__aeternixDesk) return;
  window.__aeternixDesk = true;

  const send = async (type, payload = {}) => {
    const r = await chrome.runtime.sendMessage({ type, ...payload });
    if (r && r.error) throw new Error(r.error);
    return r;
  };

  function waitFor(selector, ms = 10000) {
    const found = document.querySelector(selector);
    if (found) return Promise.resolve(found);
    return new Promise((resolve) => {
      const obs = new MutationObserver(() => {
        const el = document.querySelector(selector);
        if (el) { obs.disconnect(); resolve(el); }
      });
      obs.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => { obs.disconnect(); resolve(document.querySelector(selector)); }, ms);
    });
  }

  // ------------------------------------------------------------------ panel
  function panel(adapterName) {
    const host = document.createElement("div");
    host.id = "aeternix-desk";
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>
      :host { all: initial; }
      .box { width: 330px; max-height: 70vh; overflow: auto; background: #fff; color: #000; border: 1px solid #000;
             font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; box-shadow: 0 2px 10px rgba(0,0,0,.18); }
      .head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 9px 12px;
              background: #d5e8f7; border-bottom: 1px solid #000; }
      .head b { font: 600 15px/1.2 Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif; }
      .head small { color: #333; font-size: 11px; }
      .body { padding: 10px 12px 12px; }
      .min { border: 0; background: none; font: 600 16px/1 sans-serif; cursor: pointer; padding: 0 2px; }
      p { margin: 0 0 8px; }
      .muted { color: #4b5563; font-size: 12px; }
      .err { color: #9b1c1c; }
      .ok { color: #14532d; }
      button.act { font: 600 13px/1 inherit; font-family: inherit; padding: 8px 12px; border: 1px solid #000; background: #fff;
                   color: #000; cursor: pointer; border-radius: 6px; margin: 4px 6px 4px 0; }
      button.act.primary { background: #000; color: #fff; }
      button.act:disabled { opacity: .45; cursor: not-allowed; }
      button:focus-visible, input:focus-visible { outline: 2px solid #1d6fb8; outline-offset: 2px; }
      input { width: 100%; box-sizing: border-box; font: 13px inherit; font-family: inherit; padding: 7px 8px;
              border: 1px solid #6b7280; border-radius: 6px; margin-bottom: 6px; }
      ul { margin: 4px 0 8px; padding-left: 18px; }
      li { margin: 2px 0; }
      .pick { display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid #e5e7eb; background: none;
              padding: 7px 2px; cursor: pointer; font: 13px inherit; font-family: inherit; color: #000; }
      .pick:hover { background: #eef6fc; }
      .pill { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 9px; border: 1px solid #9ca3af; }
      .pill.green { background: #e7f6ec; border-color: #86c79b; } .pill.red { background: #fde8e8; border-color: #e7a3a3; }
      .pill.amber { background: #fdf3dc; border-color: #e6c27a; }
    </style>
    <div class="box" role="region" aria-label="Aeternix claims desk">
      <div class="head"><div><b>Claims to cash</b><br><small></small></div><button class="min" title="Hide" aria-label="Hide">–</button></div>
      <div class="body"></div>
    </div>`;
    root.querySelector("small").textContent = adapterName;
    const body = root.querySelector(".body");
    root.querySelector(".min").onclick = () => { body.hidden = !body.hidden; };
    document.documentElement.appendChild(host);
    const h = (tag, attrs = {}, text) => {
      const el = document.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "onclick") el.onclick = v; else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
      }
      if (text !== undefined) el.textContent = text;
      return el;
    };
    return {
      body, h,
      clear() { body.replaceChildren(); },
      add(...els) { els.forEach((e) => body.appendChild(e)); return els[0]; },
      say(text, cls = "") { return this.add(h("p", { class: cls }, text)); },
      button(text, fn, primary = false) {
        const b = h("button", { class: `act${primary ? " primary" : ""}`, type: "button" }, text);
        b.onclick = async () => { b.disabled = true; try { await fn(); } finally { b.disabled = false; } };
        return this.add(b);
      },
    };
  }

  // ------------------------------------------------------------------ claim list
  async function claimList(adapter, spec, ui) {
    const read = () => Array.from(document.querySelectorAll(spec.row)).map((tr) => {
      const row = {};
      for (const [k, sel] of Object.entries(spec.cells || {})) {
        const el = sel ? tr.querySelector(sel) : null;
        row[k] = el ? el.textContent.replace(/\s+/g, " ").trim() : "";
      }
      return row;
    }).filter((r) => r.portal_ref);

    async function sync() {
      ui.clear();
      const rows = read();
      if (!rows.length) { ui.say("No claims on this page to read."); ui.button("Read again", sync); return; }
      ui.say(`Reading ${rows.length} claim${rows.length === 1 ? "" : "s"}…`, "muted");
      try {
        const r = await send("snapshots", { portal: adapter.portal, rows });
        ui.clear();
        ui.say(`Read ${r.seen} claim${r.seen === 1 ? "" : "s"}: ${r.changed} changed since last read, ${r.unlinked} not linked to a case.`, "ok");
        const moved = (r.rows || []).filter((x) => x.changed && x.applied);
        if (moved.length) {
          const ul = ui.add(ui.h("ul"));
          moved.slice(0, 12).forEach((x) => ul.appendChild(ui.h("li", {}, `${x.portal_ref}: ${x.applied}`)));
        }
        if (r.unlinked) ui.say("Link the unlinked claims to their cases in Aeternix (Cases, then the portal reference).", "muted");
      } catch (e) {
        ui.clear(); ui.say(e.message, "err");
      }
      ui.button("Read again", sync);
    }

    await waitFor(spec.row, 8000);
    const cfg = await send("config");
    if (cfg.autoRead) await sync();
    else { ui.say("This claim list can be read into Aeternix."); ui.button("Read this list", sync, true); }
  }

  // ------------------------------------------------------------------ claim form
  function setValue(el, value) {
    const v = String(value ?? "");
    if (el.tagName === "SELECT") {
      const opt = Array.from(el.options).find((o) => o.value === v || o.textContent.trim().toLowerCase() === v.toLowerCase());
      if (!opt) return false;
      el.value = opt.value;
    } else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.style.outline = "2px solid #5aa0d8";
    el.style.backgroundColor = "#eef6fc";
    return true;
  }

  function b64ToFile(b64, name, mime) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], name, { type: mime });
  }

  async function claimForm(adapter, spec, ui) {
    let heartbeat = null;

    function showSearch() {
      ui.clear();
      ui.say("Which case is this claim for?");
      const input = ui.add(ui.h("input", { type: "search", placeholder: "Case no. or patient name", "aria-label": "Find a case" }));
      const results = ui.add(ui.h("div"));
      let timer = null;
      const run = async () => {
        results.replaceChildren(ui.h("p", { class: "muted" }, "Looking…"));
        try {
          const cases = await send("searchCases", { q: input.value });
          results.replaceChildren();
          if (!cases.length) results.appendChild(ui.h("p", { class: "muted" }, "No discharged cases match."));
          for (const c of cases) {
            const b = ui.h("button", { class: "pick", type: "button" });
            b.appendChild(ui.h("strong", {}, c.case_no));
            b.appendChild(document.createTextNode(` ${c.beneficiary_name || ""}, ${c.payer.code} `));
            b.appendChild(ui.h("span", { class: `pill ${c.check_status || ""}` },
              { green: "check passed", red: "red flags", amber: "waiting for review" }[c.check_status] || "not checked"));
            b.onclick = () => choose(c.id);
            results.appendChild(b);
          }
        } catch (e) { results.replaceChildren(ui.h("p", { class: "err" }, e.message)); }
      };
      input.oninput = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
      run();
      input.focus();
    }

    async function choose(caseId) {
      ui.clear();
      ui.say("Loading the case…", "muted");
      let p;
      try { p = await send("prefill", { caseId, portal: adapter.portal }); }
      catch (e) { ui.clear(); ui.say(e.message, "err"); ui.button("Pick another case", showSearch); return; }
      ui.clear();
      ui.add(ui.h("p", {}, "")).append(ui.h("strong", {}, p.case_no), ` ${p.fields.patient_name || ""}`);
      ui.say(p.state_label, "muted");
      if (!p.ready) {
        ui.say(p.blocked_reason, "err");
        ui.button("Pick another case", showSearch);
        return;
      }
      const nDocs = Object.values(p.uploads).reduce((s, x) => s + x.length, 0);
      ui.say(`${Object.keys(spec.fields || {}).length} fields and ${nDocs} confirmed document${nDocs === 1 ? "" : "s"} ready to fill.`);
      ui.button("Fill from Aeternix", () => fill(p), true);
      ui.button("Pick another case", showSearch);
    }

    async function fill(p) {
      ui.clear();
      ui.say("Filling…", "muted");
      const filled = [], empty = [], notFound = [], attached = [], failed = [];
      for (const [key, sel] of Object.entries(spec.fields || {})) {
        const el = sel ? document.querySelector(sel) : null;
        const value = p.fields[key];
        if (!el) { notFound.push(key); continue; }
        if (value === "" || value === null || value === undefined) { empty.push(key); continue; }
        (setValue(el, value) ? filled : failed).push(key);
      }
      for (const [slot, s] of Object.entries(spec.uploads || {})) {
        const docs = p.uploads[slot] || [];
        if (!docs.length) continue;
        const input = s.selector ? document.querySelector(s.selector) : null;
        if (!input) { notFound.push(slot); continue; }
        try {
          const files = [];
          for (const d of docs) {
            const f = await send("file", { documentId: d.document_id });
            files.push(b64ToFile(f.base64, d.filename, d.mime || f.mime));
          }
          const dt = new DataTransfer();
          files.forEach((f) => dt.items.add(f));
          input.files = dt.files;
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
          input.style.outline = "2px solid #5aa0d8";
          attached.push(...docs.map((d) => d.filename));
        } catch (e) { failed.push(`${slot} (${e.message})`); }
      }
      await send("pending:set", { caseId: p.case_id, caseNo: p.case_no, portal: adapter.portal });

      ui.clear();
      ui.say(`Filled ${filled.length} field${filled.length === 1 ? "" : "s"} and attached ${attached.length} document${attached.length === 1 ? "" : "s"} for ${p.case_no}.`, "ok");
      const label = (k) => k.replace(/_/g, " ");
      if (empty.length) ui.say(`No value in Aeternix: ${empty.map(label).join(", ")}.`, "muted");
      if (notFound.length) ui.say(`Not found on this page: ${notFound.map(label).join(", ")}. The portal may have changed; report it so the selectors are updated.`, "err");
      if (failed.length) ui.say(`Could not fill: ${failed.map(label).join(", ")}.`, "err");
      ui.say("Check every highlighted field against the claim pack, then press Submit yourself. The extension never submits.");
      const submit = spec.submit_button ? document.querySelector(spec.submit_button) : null;
      if (submit) {
        submit.style.outline = "3px solid #1d6fb8";
        submit.style.outlineOffset = "3px";
        submit.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      ui.button("Pick another case", showSearch);

      // Desk time on this claim, for the pilot's minutes-per-claim measure. Only while the tab is in view.
      clearInterval(heartbeat);
      heartbeat = setInterval(() => {
        if (document.visibilityState === "visible") send("activity", { caseId: p.case_id, seconds: 60 }).catch(() => {});
      }, 60000);
    }

    showSearch();
  }

  // ------------------------------------------------------------------ confirmation
  async function confirmation(adapter, spec, ui) {
    const el = await waitFor(spec.portal_ref, 8000);
    const ref = el ? el.textContent.trim() : "";
    if (!ref) { ui.say("Could not read the claim reference on this page. Record it on the case by hand.", "err"); return; }
    const pending = await send("pending:get");
    if (!pending || pending.portal !== adapter.portal) {
      ui.say(`Claim reference ${ref}. No case was filled from Aeternix in this browser, so record this reference on the case in Aeternix.`);
      return;
    }
    ui.say(`The portal gave claim reference ${ref}.`);
    ui.button(`Record it as submitted for ${pending.caseNo}`, async () => {
      try {
        const r = await send("action", { caseId: pending.caseId, event: "submit_claim", body: { portal_ref: ref } });
        await send("pending:clear");
        ui.clear();
        ui.say(`Recorded. ${pending.caseNo} is now "${(r && r.state_label) || "Claim submitted"}", and the payer's clocks have started.`, "ok");
      } catch (e) { ui.say(e.message, "err"); }
    }, true);
    ui.button("Not this case", async () => { await send("pending:clear"); ui.clear(); ui.say("Cleared. Record the reference on the right case in Aeternix."); });
  }

  // ------------------------------------------------------------------ start
  (async () => {
    let cfg;
    try { cfg = await send("config"); } catch (e) { return; }
    if (!cfg.adapter || !cfg.signedIn) return;
    const pages = cfg.adapter.pages || {};
    const href = location.href;
    const which = ["claim_form", "confirmation", "claim_list"].find((k) => pages[k] && pages[k].url_contains && href.includes(pages[k].url_contains));
    if (!which) return;
    const ui = panel(cfg.adapter.name);
    try {
      if (which === "claim_list") await claimList(cfg.adapter, pages.claim_list, ui);
      else if (which === "claim_form") await claimForm(cfg.adapter, pages.claim_form, ui);
      else await confirmation(cfg.adapter, pages.confirmation, ui);
    } catch (e) { ui.say(e.message, "err"); }
  })();
})();
