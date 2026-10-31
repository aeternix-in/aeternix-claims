// Mock payer portal store. Lives in this browser's localStorage only; a test fixture, not a payer system.
const MOCK_KEY = "mockPortal.claims.v1";

function mockLoad() {
  try { return JSON.parse(window.localStorage.getItem(MOCK_KEY) || "[]"); } catch (e) { return []; }
}
function mockSave(rows) {
  try { window.localStorage.setItem(MOCK_KEY, JSON.stringify(rows)); } catch (e) { /* storage blocked: rows live until reload */ }
}
function mockRef() {
  return "MCK" + String(Math.floor(1e6 + Math.random() * 9e6));
}
function mockToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function mockMoney(v) {
  if (v === "" || v === null || v === undefined || isNaN(Number(v))) return "";
  return Number(v).toFixed(2);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// The statuses this fixture can show. Wording is generic on purpose; real portals' exact strings are
// captured per adapter (docs/ADAPTERS.md) and mapped in the adapter's rules.
const MOCK_STATUSES = ["Claim Submitted", "Under Process", "Query Raised", "Approved", "Partially Approved",
                       "Rejected", "Payment Done"];
