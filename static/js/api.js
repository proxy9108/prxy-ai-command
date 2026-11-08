// Small helpers shared by every page. Server text is only ever written with textContent.
export const $ = (id) => document.getElementById(id);
export const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
export const api = (url) => fetch(url).then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || r.status); return d; });
export const send = (url, body, method = "POST") => fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body ?? {})}).then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || r.status); return d; });
export const ago = (t) => { if (!t) return "never"; const s = Math.round(Date.now() / 1000 - t); return s < 90 ? s + " s ago" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; };
export const inn = (t) => { if (!t) return "on demand"; const s = Math.round(t - Date.now() / 1000); return s <= 0 ? "due now" : s < 90 ? "in " + s + " s" : s < 5400 ? "in " + Math.round(s / 60) + " min" : "in " + Math.round(s / 3600) + " h"; };
export const clock = (t) => new Date(t * 1000).toLocaleString();
export function tile(label, value, ok = true) { const t = el("div", "tile " + (ok ? "ok" : "bad")); t.append(el("b", null, label), el("span", null, value)); return t; }
export function panel(title, ...kids) { const p = el("section", "panel"); if (title) p.append(el("h2", null, title)); p.append(...kids); return p; }
export function btn(label, fn, cls = "") { const b = el("button", cls, label); b.type = "button"; b.addEventListener("click", async () => { b.disabled = true; try { await fn(b); } catch (e) { alert(e.message); } b.disabled = false; }); return b; }
export function sevPill(s) { return el("span", "sev " + s, s); }
export const GROUP_LABEL = {security: "Security", operations: "Operations", personal: "Personal", housekeeping: "Housekeeping", council: "Council", test: "Test"};

// Update a list in place: a card is only rebuilt when its own version changed, never while you are using it (typing, a ticked box, focus),
// and sections you opened stay open. render(item) -> node; key(item) / ver(item) -> strings.
export function keyedList(box, items, key, ver, render, empty) {
  const old = new Map([...box.children].filter((n) => n.dataset && n.dataset.k).map((n) => [n.dataset.k, n]));
  const busy = (n) => n.contains(document.activeElement) || [...n.querySelectorAll("input,textarea")].some((i) => (i.type === "checkbox" ? i.checked : i.value.trim() !== ""));
  const next = items.map((it) => {
    const k = String(key(it)), v = String(ver(it)), prev = old.get(k);
    if (prev && prev.dataset.v === v) return prev;
    if (prev && busy(prev)) {
      if (!prev.querySelector(".stale-note")) { const s = document.createElement("p"); s.className = "stale-note"; s.textContent = "This changed while you were working on it. It refreshes when you're done."; prev.prepend(s); }
      return prev;
    }
    const n = render(it); n.dataset.k = k; n.dataset.v = v;
    if (prev) { const was = [...prev.querySelectorAll("details")].map((d) => d.open); n.querySelectorAll("details").forEach((d, i) => { if (i < was.length) d.open = was[i]; }); }
    return n;
  });
  if (!items.length) { box.replaceChildren(empty || ""); return; }
  if (next.length !== box.children.length || next.some((n, i) => box.children[i] !== n)) box.replaceChildren(...next);
}
