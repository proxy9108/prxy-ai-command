// 1.4: the crew. Home (recap + what needs you + the break room), the org chart (the triangle), the break room and stand-ups.
// Server text is only ever written with textContent.
import {el, api, send, btn, panel, sevPill} from "./api.js";
import {portrait} from "./office.js";

const YOU = {skin: "#c68b59", hair: "#1b1b1b", style: "short", shirt: "#ffc857", acc: "cap"};
let LOOKS = null;
async function looks() {
  if (!LOOKS) { LOOKS = {}; try { (await api("/api/roster")).forEach((r) => { LOOKS[r.name] = r.look; }); (await crewInfo()); Object.values(INFO).forEach((m) => { if (m.look && !LOOKS[m.name]) LOOKS[m.name] = m.look; }); } catch { /* portraits fall back to blank */ } }
  return LOOKS;
}
export function pp(look, size = 32, title = "") {
  const c = portrait(el("canvas", "pp"), look); c.style.width = c.style.height = size + "px"; c.setAttribute("aria-hidden", "true"); if (title) c.title = title; return c;
}
const hm = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
const day = (t) => new Date(t * 1000).toLocaleDateString([], {weekday: "long", day: "numeric", month: "long"});
const TAG = {escalate: "escalated", nudge: "reminder", greet: "welcome", absent: "", standup: "", review: "second opinion", coach: "coaching", suggest: "needs your OK"};
const sqClass = (s) => "sq-" + (s || "none");
const ledFor = (a) => "led " + (!a.built || !a.enabled ? "off" : a.state);
const openN = (o) => Object.values(o || {}).reduce((x, y) => x + y, 0);
const hot = (o) => (o || {}).high || (o || {}).critical;

let INFO = null;
export const crewSync = () => INFO || {};          // filled once at startup by main.js
export async function crewInfo() {                 // name -> {callsign, look, squad} for pages that show who raised something
  if (!INFO) { INFO = {}; try { const o = await api("/api/org"); [o.command, ...o.staff, ...(o.advisors || []), ...o.squads.flatMap((s) => [s.lead, ...s.members])].forEach((m) => { INFO[m.name] = m; }); } catch { /* names fall back */ } }
  return INFO;
}

// ---------------------------------------------------------------- the chat feed (used by Home and the Break room)
function chatFeed(ctx, thread, {compose = true, height} = {}) {
  const box = el("div", "chatbox"), feed = el("div", "feed");
  feed.setAttribute("role", "log"); feed.setAttribute("aria-live", "polite"); feed.setAttribute("aria-label", "Crew chat");
  if (height) box.style.height = height;
  box.append(feed);
  let last = 0, prev = null, lk = {}, replyTo = null, setReply = () => {};
  const render = (m) => {
    const same = prev && prev.author === m.author && m.ts - prev.ts < 180 && prev.kind === m.kind;
    if (!prev || new Date(prev.ts * 1000).toDateString() !== new Date(m.ts * 1000).toDateString()) feed.append(el("div", "daymark", day(m.ts)));
    const row = el("div", `msg ${m.kind} ${sqClass(m.squad)}` + (m.author === "owner" ? " mine" : "") + (same ? " cont" : ""));
    const head = el("div", "mh");
    const who = el("span", "call", m.callsign);
    head.append(who);
    if (m.to_callsign) head.append(el("span", "to", "to " + m.to_callsign));
    if (TAG[m.kind]) head.append(el("span", "tag", TAG[m.kind]));
    const t = el("time", null, hm(m.ts)); t.dateTime = new Date(m.ts * 1000).toISOString(); head.append(t);
    row.append(pp(m.author === "owner" ? YOU : lk[m.author], 32), head);
    const q = m.data && m.data.quote;
    if (q) { const bq = el("div", "quote"); bq.append(el("b", null, q.callsign + ": "), q.text); row.append(bq); }
    row.append(el("div", "mt", m.text));
    if (compose && m.author !== "owner" && thread === "breakroom") { const rb = el("button", "mini ghost reply-btn", "Reply"); rb.type = "button"; rb.title = `Reply to ${m.callsign}`; rb.onclick = () => setReply(m); row.append(rb); }
    const sg = m.data && m.data.suggest;
    if (sg) {
      const act = el("div", "msg-act");
      if (sg.done) act.append(el("span", "dim", `Done: ${sg.changed} changed.`));
      else { const b = el("button", "primary", sg.label); b.type = "button"; b.onclick = async () => { b.disabled = true; try { const r = await send(`/api/chat/${m.id}/accept`, {}); act.replaceChildren(el("span", "dim", `Done: ${r.changed} changed.`)); } catch (e) { b.disabled = false; alert(e.message); } }; act.append(b); }
      row.append(act);
    }
    if (m.data && m.data.project) { const a = el("a", "msg-act", "Open the project"); a.href = "#/team/" + m.data.project; row.append(a); }
    if (m.data && m.data.mission) { const a = el("a", "msg-act", "Open the mission"); a.href = "#/missions"; row.append(a); }
    if (m.author === "owner" && m.data && m.data.intent && m.data.intent.kind !== "chat") { const k = {update: "understood as an update", instruction: "understood as a request", question: "understood as a question"}[m.data.intent.kind]; if (k) head.append(el("span", "tag", k)); }
    feed.append(row); prev = m; last = Math.max(last, m.id);
  };
  const atBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  const pull = async () => {
    const stick = atBottom();
    const d = await api(`/api/chat?thread=${encodeURIComponent(thread)}&after=${last}&limit=${last ? 100 : 120}`);
    d.messages.forEach(render);
    if (!last && !d.messages.length) feed.append(el("p", "empty", thread === "breakroom" ? "No chatter yet. The crew talks when something happens: a new finding, a failed run, a cleared problem." : "Nothing here yet."));
    if (stick || d.messages.length && d.messages.length === feed.querySelectorAll(".msg").length) feed.scrollTop = feed.scrollHeight;
  };
  const ready = looks().then((l) => { lk = l; return pull(); });
  if (compose) {
    const form = el("form", "compose"), input = el("input"), go = el("button", "primary", "Send"), chip = el("div", "replying");
    chip.hidden = true;
    setReply = (m) => {
      replyTo = m;
      if (!m) { chip.hidden = true; chip.replaceChildren(); input.placeholder = "Talk to the crew…"; return; }
      const x = el("button", "mini ghost", "✕"); x.type = "button"; x.setAttribute("aria-label", "Cancel the reply"); x.onclick = () => setReply(null);
      chip.replaceChildren(el("span", null, `Replying to ${m.callsign}: `), el("i", null, m.text.slice(0, 120) + (m.text.length > 120 ? "…" : "")), x);
      chip.hidden = false; input.placeholder = `Reply to ${m.callsign}…`; input.focus();
    };
    input.placeholder = "Talk to the crew…"; input.maxLength = 400; input.setAttribute("aria-label", "Message to the crew"); go.type = "submit";
    form.append(input, go);
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); const text = input.value.trim(); if (!text) return;
      go.disabled = true;
      try { await send("/api/chat", replyTo ? {text, reply_to: replyTo.id} : {text}); input.value = ""; setReply(null); await pull(); feed.scrollTop = feed.scrollHeight; }
      catch (err) { alert("Message not sent: " + err.message); }
      go.disabled = false; input.focus();
    });
    box.append(chip, form, el("p", "compose-hint", "Press Reply on any message to talk with that agent about it; follow-ups go to the same agent. Tell them what you changed (“I moved Kali to VLAN 60”), ask (“Vault, how are the backups?”), or give a job (“look into the thin pool”, “research Proxmox 9.2”, “stop warning me about MQTT”). Also: “stand-up”, “what did I miss”."));
  }
  let pending = null;
  const off = ctx.onEvent((ev) => { if (ev.type === "chat.message" && ev.thread === thread && !pending) pending = setTimeout(() => { pending = null; pull(); }, 300); });
  const timer = setInterval(pull, 30000);
  return {node: box, ready, stop: () => { off(); clearInterval(timer); if (pending) clearTimeout(pending); }};
}

// ---------------------------------------------------------------- Home
export async function home(view, ctx) {
  ctx.setActions(btn("Start a stand-up", async () => { const r = await send("/api/standup", {}); location.hash = "#/chat/" + r.thread; }));
  const top = el("div"), grid = el("div", "home"), left = el("div"), right = el("div");
  grid.append(left, right); view.append(top, grid);
  const lk = await looks();
  const [rc, org, threads] = await Promise.all([api("/api/recap"), api("/api/org"), api("/api/chat/threads")]);

  // recap: only after an absence
  if (rc.recap) {
    const r = rc.recap, card = el("section", "recap");
    card.setAttribute("aria-label", "While you were away");
    const close = btn("Dismiss", async () => { await send("/api/recap/dismiss", {}); card.remove(); }, "ghost close");
    card.append(pp(lk.commander, 52), el("h2", null, `Welcome back. You were away ${r.away}.`), close, el("p", null, r.headline));
    if (r.top) {
      const one = el("p", "one", "The one thing for you: "); const a = el("a", null, r.top.title); a.href = "#/finding/" + r.top.id;
      one.append(a, ` (${r.top.severity}, from ${r.top.callsign}).`); card.append(one);
    }
    if (r.highlights && r.highlights.length) {
      const ul = el("ul"); r.highlights.slice(-4).forEach((h) => { const li = el("li"); li.append(el("b", null, h.callsign + ": "), h.text); ul.append(li); }); card.append(ul);
    }
    top.append(card);
  }

  // left: the break room
  const feed = chatFeed(ctx, "breakroom", {height: "min(64vh,700px)"});
  const p1 = panel(null); const h = el("h2", null, "Break room"); h.append(el("span", "hint", "the crew talks about what really happened")); p1.append(h, feed.node);
  left.append(p1);

  // right: what's next, on shift, stand-up
  const nextBox = el("div", "next");
  const pn = panel(null); const hn = el("h2", null, "What's next"); hn.append(el("span", "hint", "the Chief's list, most important first")); pn.append(hn, nextBox);
  right.append(pn);
  const loadNext = async () => { try { renderNext(nextBox, await api("/api/next"), loadNext); } catch (e) { nextBox.replaceChildren(el("p", "bad", "Couldn't load the list: " + e.message)); } };
  await loadNext();
  const offNext = ctx.onEvent((ev) => { if (/^(mission\.|finding\.status)/.test(ev.type)) loadNext(); });
  const tNext = setInterval(loadNext, 30000);

  const shift = el("div", "shift");
  const sqRow = (name, color, members, cls) => {
    const row = el("div", "sq " + cls), faces = el("div", "faces");
    const open = members.reduce((n, m) => n + openN(m.open_findings), 0), bad = members.filter((m) => m.built && m.enabled && m.state === "error").length;
    row.append(el("span", "nm", name), el("span", "meta", `${open} open` + (bad ? `, ${bad} with errors` : "")));
    members.forEach((m) => { const a = el("a", "face"); a.href = "#/agent/" + m.name; a.title = `${m.callsign} (${m.title}): ${m.built && m.enabled ? m.state : "off duty"}`; a.setAttribute("aria-label", a.title); a.append(pp(m.look, 30), el("span", ledFor(m))); faces.append(a); });
    row.append(faces); return row;
  };
  shift.append(sqRow("High Command", null, [org.command, ...org.staff], "sq-command"));
  org.squads.forEach((s) => shift.append(sqRow(s.name, s.color, [s.lead, ...s.members], "sq-" + s.key)));
  const ps = panel(null); const h2 = el("h2", null, "On shift"); const al = el("a", null, "See the crew"); al.href = "#/crew"; al.style.cssText = "float:right;font-weight:400;font-size:13px"; h2.append(al); ps.append(h2, shift);
  right.append(ps);

  const su = threads.filter((t) => t.thread.startsWith("standup:"))[0];
  const sp = panel("Latest stand-up");
  if (su) {
    const d = await api(`/api/chat?thread=${su.thread}&limit=1`); const m = d.messages[0];
    sp.append(el("p", "meta", day(su.last) + ", " + hm(su.last)));
    if (m) { const q = el("p"); q.style.margin = "6px 0 8px"; q.append(el("b", "call", m.callsign + ": "), m.text); sp.append(q); }
    const a = el("a", null, "Read the stand-up"); a.href = "#/chat/" + su.thread; sp.append(a);
  } else sp.append(el("p", "empty", "No stand-up yet. One runs every morning, or start one now."));
  right.append(sp);

  await feed.ready;
  return () => { feed.stop(); offNext(); clearInterval(tNext); };
}

// One row per next action: who does it, what, why, and the button that does it.
export function renderNext(box, d, reload) {
  const items = d.items || [];
  if (!items.length) { box.replaceChildren(el("p", "empty", "Nothing is waiting: no approvals, no open fixes, nothing for you to do.")); return; }
  const ol = el("ol", "next-list");
  items.forEach((x) => {
    const li = el("li", "nx nx-" + x.who), head = el("div", "nx-head");
    head.append(el("span", "nx-who " + x.who, x.who === "you" ? "You" : "Team"), el("b", "nx-title", x.title));
    li.append(head, el("p", "nx-why", x.why));
    const bar = el("div", "nx-actions"), msg = el("span", "dim");
    (x.actions || []).forEach((a) => {
      if (a.href) { const l = el("a", "btnlink " + (a.style || ""), a.label); l.href = a.href; bar.append(l); return; }
      bar.append(btn(a.label, async () => {
        if (a.confirm && !confirm(a.confirm)) return;
        try { const r = await send(a.post, a.body || {}); msg.textContent = r.id ? `Planning PRX-${String(r.id).padStart(4, "0")}…` : "Done."; await reload(); } catch (e) { msg.textContent = e.message; }
      }, a.style || "ghost"));
    });
    bar.append(msg); li.append(bar); ol.append(li);
  });
  const foot = el("p", "meta", d.why_by ? `Reasons written by ${(crewSync()[d.why_by] || {callsign: d.why_by}).callsign}.` : "");
  box.replaceChildren(ol, foot);
}

// ---------------------------------------------------------------- the crew (org chart)
function card(m, cls = "") {
  const a = el("a", `card ${cls} sq-${m.squad || "none"}`); a.href = "#/agent/" + m.name;
  const cs = el("span", "cs"); cs.append(el("span", ledFor(m)), m.callsign);
  a.append(pp(m.look, cls === "chief" ? 64 : 46), cs, el("span", "ti", m.title), el("span", "pe", m.personality));
  const n = openN(m.open_findings); if (n) a.append(el("span", "oc" + (hot(m.open_findings) ? " hot" : ""), `${n} open`));
  a.setAttribute("aria-label", `${m.callsign}, ${m.title}. ${m.personality}`);
  return a;
}
function member(m) {
  const a = el("a", "member" + (m.built && m.enabled ? "" : " off")); a.href = "#/agent/" + m.name;
  const mid = el("span"); mid.append(el("span", "cs", m.callsign), el("span", "ti", m.title));
  const r = el("span", "r"); const n = openN(m.open_findings); if (n) r.append(el("span", null, `${n} open`)); r.append(el("span", ledFor(m)));
  a.append(pp(m.look, 34), mid, r); a.title = m.personality; return a;
}
export async function crewPage(view, ctx) {
  const draw = async () => {
    const o = await api("/api/org");
    const org = el("div", "org");
    const t1 = el("div", "tier"); t1.append(card({...o.command, squad: "command"}, "chief"));
    const t2 = el("div", "tier"); o.staff.forEach((m) => t2.append(card({...m, squad: "command"}, "staff")));
    const t2b = el("div", "tier"); (o.advisors || []).forEach((m) => { const c = card({...m, squad: "command"}, "staff advisor"); if (!m.enabled) c.classList.add("off"); t2b.append(c); });
    const squads = el("div", "squads");
    o.squads.forEach((s) => {
      const box = el("section", "squad sq-" + s.key), head = el("header");
      head.append(el("h3", null, s.name), el("p", null, s.about));
      const mem = el("div", "members"); s.members.forEach((m) => mem.append(member(m)));
      box.append(head, card(s.lead, "lead"), el("p", "meta", `Reports to ${s.lead.callsign}`), mem);
      squads.append(box);
    });
    org.append(el("p", "tier-label", "High Command"), t1, el("div", "link"), el("p", "tier-label", "Council staff: they advise the Chief and keep everyone honest"), t2,
               el("p", "tier-label", "Outside advisors: hosted models that plan, review and coach the local crew (text only, never act). Switch them on in Settings."), t2b, el("div", "link"),
               el("p", "tier-label", "Squads: each lead reports to the Chief, the members report to their lead"), squads);
    const intro = el("p", "lede", "Workers report to their squad lead, leads escalate to the Chief, and the Chief brings what matters to you. Nothing in this chain changes the lab on its own: they read, report and propose.");
    view.replaceChildren(intro, org);
  };
  let t = null;
  await draw();
  const off = ctx.onEvent((ev) => { if (/^agent\.(finished|error)$/.test(ev.type)) { clearTimeout(t); t = setTimeout(draw, 1500); } });
  return () => { off(); clearTimeout(t); };
}

// ---------------------------------------------------------------- the break room and stand-ups
export async function chatPage(view, ctx, thread) {
  thread = thread || "breakroom";
  ctx.setActions(btn("Start a stand-up", async () => { const r = await send("/api/standup", {}); location.hash = "#/chat/" + r.thread; }));
  const wrap = el("div", "chatpage"), list = el("nav", "threads"), main = el("div");
  list.setAttribute("aria-label", "Conversations");
  wrap.append(list, main); view.append(wrap);
  const th = await api("/api/chat/threads");
  const link = (key, label, sub) => { const a = el("a"); a.href = "#/chat/" + key; a.append(label); if (sub) a.append(el("small", null, sub)); if (key === thread) a.setAttribute("aria-current", "page"); list.append(a); };
  const br = th.find((x) => x.thread === "breakroom");
  link("breakroom", "Break room", br ? `${br.n} messages` : "");
  th.filter((x) => x.thread.startsWith("standup:")).slice(0, 14).forEach((x) => link(x.thread, "Stand-up", day(x.last)));
  const isStandup = thread.startsWith("standup:");
  ctx.setSub(isStandup ? "Stand-up notes, " + day(th.find((x) => x.thread === thread)?.last || Date.now() / 1000) : "The crew talking. Every line comes from something that really happened.");
  const feed = chatFeed(ctx, thread, {compose: !isStandup});
  const p = panel(null, feed.node); p.style.marginTop = "0"; main.append(p);
  await feed.ready;
  return () => feed.stop();
}

// ---------------------------------------------------------------- persona header on an agent's page
export async function personaHeader(view, name) {
  try {
    const o = await api("/api/org");
    const all = [o.command, ...o.staff, ...o.squads.flatMap((s) => [s.lead, ...s.members])];
    const m = all.find((x) => x.name === name); if (!m) return;
    const sq = o.squads.find((s) => s.lead.name === name || s.members.some((x) => x.name === name));
    const box = el("div", "persona sq-" + (m.squad || "none"));
    const boss = all.find((x) => x.name === m.reports_to);
    box.append(pp(m.look, 60), el("span", "cs call", m.callsign),
               el("p", null, `${m.personality} ${sq ? (sq.lead.name === name ? "Leads the " + sq.name + "." : "Works in the " + sq.name + ".") : "Part of High Command."}${boss ? " Reports to " + boss.callsign + "." : ""}`));
    view.prepend(box);
  } catch { /* the page works without it */ }
}
