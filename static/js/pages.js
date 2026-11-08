import {$, el, api, send, ago, inn, clock, tile, panel, btn, sevPill, GROUP_LABEL, keyedList} from "./api.js";
import {createOffice} from "./office.js";
import {crewInfo, crewSync, pp} from "./crew.js";
import {missionCard} from "./missions.js";

const SEVS = ["critical", "high", "medium", "low", "info"];
const openText = (o) => Object.entries(o || {}).map(([k, v]) => v + " " + k).join(", ") || "no open findings";

export function criticBlock(c) {
  const b = el("div", "ev critic " + (c ? c.verdict : "none"));
  if (!c) { b.append(el("b", null, "Second opinion (the Critic)"), el("div", "dim", "Not reviewed yet: the Critic checks new proposals and high-severity findings every 30 minutes.")); return b; }
  b.append(el("b", null, "Second opinion: " + ({agree: "no objection", caution: "proceed with caution", disagree: "do NOT act yet"}[c.verdict] || c.verdict)));
  c.blockers.forEach((x) => b.append(el("div", "bad", "Blocker: " + x)));
  c.concerns.forEach((x) => b.append(el("div", null, "Concern: " + x)));
  c.harmless_explanations.forEach((x) => b.append(el("div", "dim", "Possibly harmless: " + x)));
  c.verify_first.forEach((x) => b.append(el("div", null, "Verify first: " + x)));
  if (c.devils_advocate) b.append(el("div", "dim", "Model's devil's advocate (can be wrong): " + c.devils_advocate));
  if (c.outside) b.append(el("div", c.outside.verdict === "agree" ? "ok" : "bad", `Outside review (${c.outside.model}): ${c.outside.verdict}${(c.outside.concerns || []).length ? " - " + c.outside.concerns.join(" ") : ""}`));
  return b;
}

const ROLE = {correlator: "Analyst (Correlator)", critic: "Defender (Critic)", commander: "Judge (Commander)"};
const STANCE_CLS = {escalate: "high", watch: "medium", dismiss: "low"};

export function debateBlock(th) {
  const b = el("div", "ev debate");
  b.append(el("b", null, `Council deliberation (${th.debate.mode === "deep" ? "deep: council model" : "quick: default model"}${th.debate.consensus ? ", both sides agreed" : ", after a rebuttal round"})`));
  th.debate.rounds.forEach((r) => {
    const rb = el("div", "round"); rb.append(el("div", "dim", `Round ${r.n}: ${r.title}`));
    r.turns.forEach((t) => { const x = el("div", "turn"); x.append(el("span", "sev " + (STANCE_CLS[t.stance] || "info"), t.stance), el("b", null, " " + (ROLE[t.role] || t.role) + ": "), t.text, el("span", "dim", "  [" + t.source + "]")); rb.append(x); });
    b.append(rb);
  });
  b.append(el("div", null, "Verdict: " + th.debate.verdict + ". " + ({escalate: "Keeps the computed severity.", watch: "Severity is capped at medium: keep an eye on it.", dismiss: "Treated as low: probably harmless."}[th.debate.verdict] || "")));
  return b;
}

export function incidentBlock(f) {
  const th = f.thread || {}, b = el("div", "ev incident");
  b.append(el("b", null, `Incident on ${th.entity || "?"}: ${(th.agents || []).join(", ")}`));
  if (th.change) b.append(el("div", "dim", "A configuration change was recorded on this machine recently; that can explain part of this."));
  if (th.dispatch && th.dispatch.length) { b.append(el("div", "dim", "The Commander asked these agents to look again:")); th.dispatch.forEach((x) => b.append(el("div", null, `• ${who(x.agent).callsign}: ${x.ask}  →  ${x.result || "no answer yet"}`))); }
  if (th.debate) { if (th.debate_sig !== th.sig) b.append(el("div", "dim", "This debate covered an earlier picture; the Commander convenes the council again when it has been 30 minutes.")); b.append(debateBlock(th)); } else b.append(el("div", "dim", "The council has not discussed this yet (the Commander convenes it within minutes for medium and above)."));
  return b;
}

function traceTable(rows, cols) {
  const t = el("table", "tbl"), hd = el("tr"); cols.forEach(([h]) => hd.append(el("th", null, h))); t.append(hd);
  rows.forEach((r) => { const tr = el("tr"); cols.forEach(([, fn]) => tr.append(el("td", "dim", String(fn(r) ?? "")))); t.append(tr); });
  return t;
}

export function traceBlock(id) {
  const d = el("details", "trace"); d.append(el("summary", null, "Trace: why is this here? (run, data read, model calls)"));
  let loaded = false;
  d.addEventListener("toggle", async () => {
    if (!d.open || loaded) return; loaded = true;
    try {
      const t = await api("/api/trace/finding/" + encodeURIComponent(id));
      const run = (title, r) => { if (!r) return; const x = el("div", "ev"); x.append(el("b", null, `${title}: ${who(r.agent).callsign} run #${r.id} (${r.trigger}, ${r.status}) ${clock(r.started)}`), el("div", "dim", `${r.summary || ""}${r.checked != null ? ` · examined ${r.checked} item(s)` : ""}`));
        if (r.queries.length) { x.append(el("div", "dim", "Data it read:")); x.append(traceTable(r.queries, [["Kind", (q) => q.kind], ["What", (q) => q.ref], ["Result", (q) => q.detail], ["ms", (q) => q.ms]])); } else x.append(el("div", "dim", "Query details are gone (kept 24 h by the retention rule)."));
        if (r.llm.length) { x.append(el("div", "dim", "Model calls:")); x.append(traceTable(r.llm, [["Who", (q) => q.agent], ["Model", (q) => q.model], ["Tokens", (q) => (q.prompt_tokens || 0) + (q.completion_tokens || 0)], ["ms", (q) => q.ms], ["OK", (q) => q.ok ? "yes" : "NO " + (q.error || "")]])); }
        d.append(x); };
      run("Raised by", t.raised_by); run("Latest look", t.latest_run);
      if (!t.raised_by) d.append(el("p", "dim", "No run is linked to this finding (it predates tracing)."));
      if (t.critic) d.append(el("div", "ev", `Second opinion: ${t.critic.verdict} (${clock(t.critic.reviewed)})`));
      if (t.members.length) { const m = el("div", "ev"); m.append(el("b", null, "Members of this incident:")); t.members.forEach((x) => { const a = el("a", null, `${who(x.agent).callsign}: ${x.title}`); a.href = "#/finding/" + x.id; const r = el("div"); r.append(a); m.append(r); }); d.append(m); }
      if (t.briefings.length) { const m = el("div", "ev"); m.append(el("b", null, "Cited in briefings: ")); t.briefings.forEach((x) => { const a = el("a", null, x.date); a.href = "#/briefing/" + x.id; m.append(a, " "); }); d.append(m); }
      if (t.audit.length) { const m = el("div", "ev"); m.append(el("b", null, "Audit trail:")); t.audit.slice(0, 12).forEach((a) => m.append(el("div", "dim", `${clock(a.ts)} · ${a.actor} · ${a.action} · ${a.detail}`))); d.append(m); }
      d.append(el("p", "dim", t.note));
    } catch (e) { d.append(el("p", "bad", "Could not load the trace: " + e.message)); }
  });
  return d;
}

const STATUS_WORD = {new: "new", seen: "you've seen it", approved: "approved", rejected: "marked not a problem", done: "you fixed it", resolved: "cleared by itself", expired: "expired"};
export function who(name) { const m = crewSync()[name]; return m || {callsign: name, title: name, squad: null, look: null}; }
export function byline(name, extra) {
  const m = who(name), p = el("span", "byline");
  if (m.look) p.append(pp(m.look, 22));
  const a = el("a", "call sq-" + (m.squad || "none"), m.callsign); a.href = "#/agent/" + name; p.append(a);
  if (m.title && m.title !== m.callsign) p.append(el("span", "dim", " · " + m.title));
  if (extra) p.append(el("span", "dim", " · " + extra));
  return p;
}
export function missionButton(f) {
  const b = btn("Plan a mission to fix this", async () => { await send("/api/missions", {goal: `Fix this: ${f.title}. ${f.summary || ""}`.slice(0, 600), ref: "finding:" + f.id}); location.hash = "#/missions"; }, "ghost");
  b.title = "Sage plans the steps (with checks and undo), the reviewers check them, you approve, the team carries it out.";
  return b;
}
export function findingCard(f, onChange) {
  const d = el("article", "fnd sev-" + f.severity);
  const t = el("div", "t"); t.append(sevPill(f.severity), el("span", "ft", f.title)); d.append(t);
  d.append(byline(f.agent, `${STATUS_WORD[f.status] || f.status}`));
  if (f.summary) d.append(el("div", "s", f.summary));
  const facts = [`first noticed ${ago(f.ts)}`, f.count > 1 ? `seen ${f.count} times` : "seen once", `${Math.round(f.confidence * 100)}% sure`];
  if (f.chronic) facts.push("unchanged for 2 weeks"); else if (f.changed_at && f.changed_at - f.ts > 60) facts.push("last changed " + ago(f.changed_at));
  d.append(el("div", "meta", facts.join(" · ")));
  if (f.thread && f.thread.tune && (f.status === "new" || f.status === "seen")) {
    const tn = f.thread.tune, row = el("div", "row");
    row.append(btn(`Make ${who(f.agent).callsign} quieter: ${tn.param} ${tn.from} to ${tn.to}`, async () => { await send(`/api/tune/${f.id}/accept`, {}); onChange && onChange(); }), el("span", "dim", "You can undo this in Settings."));
    d.append(row);
  }
  if (f.agent === "correlator" && f.thread) d.append(incidentBlock(f));
  if (f.proposed_action && (f.status === "new" || f.status === "seen")) { const a = el("a", "need-link", "A fix is suggested: decide on the Approvals page"); a.href = "#/approvals"; d.append(a); }
  if (f.status === "new" || f.status === "seen") d.append(missionButton(f));
  const det = el("details", "fold"); det.append(el("summary", null, `Evidence and second opinion (${f.evidence.length})`));
  if (!f.evidence.length) det.append(el("p", "dim", "The raw evidence was deleted after 24 hours (privacy rule). The summary above is what's kept."));
  for (const e of f.evidence) { const x = el("div", "ev"); x.append(el("b", null, `${e.type} · ${e.ref}`), el("pre", null, e.excerpt || "")); det.append(x); }
  det.append(criticBlock(f.critic));
  d.append(det);
  d.append(traceBlock(f.id));
  if (f.status === "new" || f.status === "seen") {
    const row = el("div", "row");
    const acts = [["Got it", "seen", "Mark as seen. It stays open."], ["Fixed it", "done", "You dealt with it. It closes."], ["Not a problem", "rejected", "The crew stops raising this for 30 days."]];
    for (const [label, st, tip] of acts) { if (st === "seen" && f.status === "seen") continue; const b = btn(label, async () => { await send(`/api/findings/${f.id}/status`, {status: st}); onChange && onChange(); }, st === "done" ? "" : "ghost"); b.title = tip; row.append(b); }
    d.append(row);
  }
  return d;
}

export async function office(view, ctx) {
  const wrap = el("div", "officewrap"), cv = el("canvas", "office"), tip = el("div", "tip"); tip.hidden = true;
  const legend = el("div", "legend");
  legend.append(...[["working", "#f6a04a", "working"], ["idle", "#5fbf7a", "idle"], ["error", "#e06a5f", "error"], ["off", "#555", "disabled or not hired yet"]].map(([k, col, t]) => { const s = el("span"); const i = el("i"); i.style.background = col; s.append(i, t); return s; }));
  const stats = el("div", "tiles"), ticker = el("div", "events"), chips = el("div", "chips"), bar = el("div", "row");
  wrap.append(cv, tip); view.append(stats, wrap, legend, bar, chips, panel("Live activity", ticker));
  let layout = {}; try { layout = await api("/api/office/layout"); } catch { /* defaults */ }
  const of = createOffice(cv, {
    layout,
    onLayout: async (l) => { try { await send("/api/office/layout", {layout: l}, "PUT"); } catch (e) { alert("Layout not saved: " + e.message); } },
    onSelect: (name) => { location.hash = "#/agent/" + name; },
    onHover: (ch, e) => { if (!ch) { tip.hidden = true; return; } tip.hidden = false; tip.style.left = e.offsetX + 14 + "px"; tip.style.top = e.offsetY + 10 + "px"; tip.replaceChildren(el("b", null, ch.title), el("div", null, `${GROUP_LABEL[ch.group] || ch.group} · ${ch.built ? (ch.enabled ? ch.state : "disabled") : "not hired yet (" + ch.session + ")"}`), el("div", "dim", ch.built ? openText(ch.open) : "")); },
  });
  let editing = false;
  const editBtn = btn("Edit desk layout", async () => { editing = !editing; of.setEdit(editing); editBtn.textContent = editing ? "Done editing" : "Edit desk layout"; msg.textContent = editing ? "Drag a desk to a free spot in its own room. Green = allowed. Changes save when you drop." : ""; }, "ghost");
  const msg = el("span", "dim");
  bar.append(editBtn, btn("Reset layout", async () => { if (confirm("Put every desk back in its original place?")) of.resetLayout(); }, "ghost"), btn("Start a stand-up", async () => { of.meeting(); await send("/api/standup", {}); msg.textContent = "Stand-up started: the crew is meeting at the council table. The notes appear in the Break room."; }, "ghost"), msg);
  const load = async () => {
    const r = await api("/api/roster"); of.setRoster(r);
    const sev = {}; r.forEach((a) => Object.entries(a.open_findings || {}).forEach(([k, v]) => { sev[k] = (sev[k] || 0) + v; }));
    stats.replaceChildren(tile("Team", `${r.filter((a) => a.built).length} live · ${r.filter((a) => !a.built).length} not hired yet`), tile("Open findings", SEVS.filter((k) => sev[k]).map((k) => sev[k] + " " + k).join(", ") || "none", !(sev.high || sev.critical)));
    chips.replaceChildren(...r.map((a) => { const c = el("a", "chip"); c.href = "#/agent/" + a.name; const led = el("span", "led " + (!a.built || !a.enabled ? "off" : a.state)); c.append(led, a.title); const n = Object.values(a.open_findings || {}).reduce((x, y) => x + y, 0); if (n) c.append(el("b", null, String(n))); return c; }));
  };
  await load();
  const who = await crewInfo(), nm = (a) => (who[a] || {}).callsign || a || "System";
  const say = (ev) => ({
    "agent.started": () => `${nm(ev.agent)} started a run`, "agent.finished": () => `${nm(ev.agent)} finished: ${ev.summary || "ok"}`, "agent.error": () => `${nm(ev.agent)} had a problem: ${ev.summary || ev.status}`,
    "finding.created": () => `${nm(ev.agent)} noticed something new (${ev.severity}): ${ev.title}`, "finding.status": () => `${nm(ev.agent)}: a finding is now ${ev.status}`,
    "finding.updated": () => `${nm(ev.agent)} saw a known problem again (${ev.count}×)`, "chat.message": () => `${nm(ev.agent)}: ${ev.text}`,
    "llm.done": () => `${nm(ev.agent)} got an answer from the local model`, "project.created": () => `New team project: ${ev.title}`, "project.done": () => `A team project ended (${ev.status})`,
    "council.verdict": () => `The council decided: ${ev.verdict}`, "council.incident": () => `Thread linked several reports into one incident`,
  }[ev.type] || null);
  const log = (ev) => { const f = say(ev); if (!f) return; const l = el("div"); l.append(el("b", null, new Date(ev.ts * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) + "  "), f()); ticker.prepend(l); while (ticker.children.length > 25) ticker.lastChild.remove(); };
  ctx.events.slice(-25).forEach(log);
  const off = ctx.onEvent((ev) => { of.handleEvent(ev); log(ev); if (/^(agent\.(finished|error|registered)|finding\.|council\.)/.test(ev.type)) load(); });
  const t = setInterval(load, 20000);
  return () => { off(); clearInterval(t); of.destroy(); };
}

export async function overview(view, ctx) {
  const h = await api("/api/health"), g = await api("/api/gateway"), r = await api("/api/roadmap");
  const sys = el("div", "tiles");
  sys.append(tile("Server", `v${h.version} · up ${Math.floor(h.uptime_s / 60)} min`), tile("Data directory", h.data_dir_writable ? "writable" : "not writable", h.data_dir_writable),
    tile("Ollama", h.ollama.ok ? h.ollama.models + " models" : "unreachable", h.ollama.ok), tile("Agents", String(h.agents_registered)),
    tile("Config", "v" + h.config_version), tile("Audit chain", h.audit.ok ? "intact" : "BROKEN at #" + h.audit.broken_at, h.audit.ok));
  view.append(panel("System", sys));
  const w = h.watch, nt = await api("/api/notify").catch(() => null), src = h.sources || {};
  const wt = el("div", "tiles");
  wt.append(tile("Scheduler heartbeat", w.loop_age_s + " s ago", w.loop_age_s < 90), tile("Agents behind schedule", String(w.late_agents), w.late_agents === 0), tile("Agents failing", String(w.failing_agents), w.failing_agents === 0),
    tile("Language model", w.gateway_ok ? "answering" : "failing most calls", w.gateway_ok), tile("Notifications", !nt ? "unknown" : !(nt.discord || nt.ntfy) ? "not set up" : nt.fails >= 3 ? "cannot deliver" : nt.policy.enabled ? "on" : "off", !!nt && (nt.discord || nt.ntfy) && nt.fails < 3 && nt.policy.enabled));
  Object.entries(src).forEach(([k, v]) => wt.append(tile(k, v.ok ? `up · ${v.ms} ms` : "DOWN", v.ok)));
  view.append(panel("Is the system itself healthy? (the Watchdog checks every 5 minutes)", wt));
  const gt = el("div", "tiles"); gt.append(tile("Queue", g.queue.length + " waiting" + (g.current ? " · running " + g.current.agent : "")), tile("Loaded models", g.loaded_models.join(", ") || "none seen"),
    tile("Model swaps", g.quiet_now ? "allowed now (quiet hours)" : "only in " + g.quiet_hours), tile("Tokens today", String(g.usage_today.reduce((s, u) => s + (u.tokens || 0), 0))));
  view.append(panel("LLM gateway", gt));
  const road = el("ol", "road"); r.sessions.forEach((s) => { const li = el("li", s.status); li.append(el("span", "id", s.id), el("span", null, s.title), el("span", "stt", s.status)); road.append(li); });
  view.append(panel("Build progress", road));
}

const AUTONOMY = {R: "Reads and reports only", P: "Can suggest steps for you to approve", A: "Can carry out allowed actions after you approve"};
const every = (s) => !s ? "on a schedule" : s < 90 ? `every ${s} s` : s < 5400 ? `every ${Math.round(s / 60)} min` : s < 129600 ? `every ${Math.round(s / 3600)} h` : `every ${Math.round(s / 86400)} days`;
function stateWords(a, live) {
  if (!a.built) return "not hired yet";
  if (!a.enabled) return "off duty";
  if (a.state === "working") return "working now";
  if (live && live.last_status && live.last_status !== "ok") return `last run had a problem (${live.last_status})`;
  return live && live.next_run ? `idle, next run ${inn(live.next_run)}` : "idle";
}
export async function agents(view, ctx) {
  const [list, live, org] = await Promise.all([api("/api/roster"), api("/api/agents"), api("/api/org")]);
  const L = Object.fromEntries(live.map((a) => [a.name, a])), R = Object.fromEntries(list.map((a) => [a.name, a]));
  view.append(el("p", "lede", "Everyone on the crew, grouped the way they work. Open someone to see what they found, run them now, or change how often they work."));
  const section = (title, about, names, cls) => {
    const p = panel(null); p.classList.add(cls); const h = el("h2", null, title); if (about) h.append(el("span", "hint", about)); p.append(h);
    const grid = el("div", "roster");
    names.forEach((n) => {
      const a = R[n]; if (!a) return; const m = who(n), lv = L[n];
      const c = el("a", "rcard" + (a.built && a.enabled ? "" : " off")); c.href = "#/agent/" + n;
      const top = el("div", "rtop"); top.append(pp(a.look, 40)); const nm = el("div"); nm.append(el("b", "call", m.callsign), el("span", "dim", a.title)); top.append(nm);
      const open = Object.values(a.open_findings || {}).reduce((x, y) => x + y, 0);
      if (open) top.append(el("span", "oc" + ((a.open_findings || {}).high ? " hot" : ""), `${open} open`));
      const st = el("div", "rstate"); st.append(el("span", "led " + (!a.built || !a.enabled ? "off" : a.state)), stateWords(a, lv));
      c.append(top, el("p", "rjob", a.job), st);
      if (lv) c.append(el("p", "meta", `${every(lv.interval_s)}${a.quality && a.quality.precision != null ? ` · you agreed with ${Math.round(a.quality.precision * 100)}% of its findings` : ""}`));
      grid.append(c);
    });
    p.append(grid); return p;
  };
  view.append(section("High Command", "the Chief and the Council staff", [org.command.name, ...org.staff.map((m) => m.name)], "sq-command"));
  org.squads.forEach((s) => view.append(section(s.name, s.about, [s.lead.name, ...s.members.map((m) => m.name)], "sq-" + s.key)));
  const rest = list.filter((a) => !crewSync()[a.name] && a.name !== "demo").map((a) => a.name);
  if (rest.length) view.append(section("Others", "not part of a squad", rest, "sq-none"));
}

export async function agentPage(view, ctx, name) {
  const r = (await api("/api/roster")).find((a) => a.name === name);
  if (!r) { view.append(panel("Not found", el("p", null, "There's no crew member with that name. Go back to Agents to pick one."))); return; }
  const m = who(name);
  document.getElementById("title").textContent = `${m.callsign}, ${r.title}`;
  view.append(panel(null, el("p", "lede", r.job)));
  if (!r.built) { view.append(panel("Not hired yet", el("p", null, `This agent arrives in roadmap session ${r.session}. Its desk is waiting in the Office.`))); return; }
  const live = (await api("/api/agents")).find((a) => a.name === name);
  const facts = el("div", "facts");
  const fact = (k, v, cls) => { const d = el("div"); d.append(el("span", "k", k), el("span", "v " + (cls || ""), v)); facts.append(d); };
  fact("Right now", stateWords(r, live), live.last_status && live.last_status !== "ok" ? "bad" : "");
  fact("Works", live.at ? "daily at " + live.at.join(", ") : every(live.interval_s));
  fact("Last run", `${ago(live.last_run)}${live.last_summary ? ": " + live.last_summary : ""}`);
  fact("Allowed to", AUTONOMY[live.autonomy] || live.autonomy);
  const ctl = el("div", "row");
  ctl.append(btn("Run now", async () => { await send(`/api/agents/${name}/run`, {}); setTimeout(() => location.reload(), 1500); }, "primary"),
    btn(live.enabled ? "Put off duty" : "Put back on duty", async () => { if (!live.enabled || confirm(`Stop ${m.callsign} from running until you turn it back on?`)) { await send(`/api/agents/${name}/config`, {enabled: !live.enabled}); location.reload(); } }, "ghost"));
  if (live.interval_s) {
    const iv = el("input"); iv.type = "number"; iv.min = 1; iv.value = Math.max(1, Math.round(live.interval_s / 60)); iv.setAttribute("aria-label", "Minutes between runs");
    ctl.append(el("label", "inline", "Run every"), iv, el("span", "inline dim", "minutes"), btn("Save", async () => { await send(`/api/agents/${name}/config`, {interval_s: Math.max(30, Math.round(Number(iv.value) * 60))}); location.reload(); }, "ghost"));
  }
  view.append(panel("At a glance", facts, ctl));
  const fl = el("div"); const reload = async () => { const f = await api(`/api/findings?agent=${name}&limit=30`); const open = f.filter((x) => x.status === "new" || x.status === "seen"), old = f.filter((x) => !(x.status === "new" || x.status === "seen"));
    fl.replaceChildren(...(open.length ? open.map((x) => findingCard(x, reload)) : [el("p", "dim", `${m.callsign} has nothing open right now.`)]));
    if (old.length) { const d = el("details", "fold"); d.append(el("summary", null, `Closed recently (${old.length})`), ...old.map((x) => findingCard(x, reload))); fl.append(d); } };
  await reload(); view.append(panel("What it found", fl));
  const runs = await api(`/api/runs?agent=${name}&limit=20`);
  const t = el("table", "tbl"); const hd = el("tr"); ["When", "Took", "Result", "Summary"].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  runs.forEach((x) => { const tr = el("tr"); tr.append(el("td", null, ago(x.started)), el("td", null, x.finished ? (x.finished - x.started).toFixed(1) + " s" : "running"), el("td", x.status === "ok" ? "" : "bad", x.status === "ok" ? (x.findings ? `${x.findings} new` : "fine") : x.status + (x.error ? ": " + x.error : "")), el("td", "dim", x.summary || "")); t.append(tr); });
  const rd = el("details", "howto"); rd.append(el("summary", null, `Recent runs (${runs.length})`), t); view.append(rd);
}

export async function findings(view, ctx) {
  const org = await api("/api/org");
  const crew = crewSync();
  const bar = el("div", "filters"), seg = el("div", "seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Show");
  let mode = "open";
  [["open", "Needs a look"], ["closed", "Dealt with"], ["all", "Everything"]].forEach(([k, label]) => { const b = el("button", k === mode ? "on" : "", label); b.type = "button"; b.setAttribute("aria-pressed", String(k === mode)); b.onclick = () => { mode = k; [...seg.children].forEach((x) => { x.className = x === b ? "on" : ""; x.setAttribute("aria-pressed", String(x === b)); }); load(); }; seg.append(b); });
  const sel = (label, opts) => { const s = el("select"); s.setAttribute("aria-label", label); opts.forEach(([v, t]) => s.append(Object.assign(el("option", null, t), {value: v}))); return s; };
  const fsq = sel("Squad", [["", "Whole crew"], ["command", "High Command"], ...org.squads.map((s) => [s.key, s.name])]);
  const fa = sel("Crew member", [["", "Anyone"], ...Object.values(crew).map((m) => [m.name, `${m.callsign} (${m.title})`])]);
  const fv = sel("Severity", [["", "Any severity"], ...SEVS.map((x) => [x, x[0].toUpperCase() + x.slice(1) + " and up"])]);
  bar.append(seg, fsq, fa, fv);
  const sum = el("p", "meta"), list = el("div");
  const rank = Object.fromEntries(SEVS.map((x, i) => [x, i]));
  const load = async () => {
    const q = new URLSearchParams({limit: 500}); if (fa.value) q.set("agent", fa.value);
    let f = await api("/api/findings?" + q);
    f = f.filter((x) => mode === "all" || (mode === "open") === (x.status === "new" || x.status === "seen"));
    if (fsq.value) f = f.filter((x) => (crew[x.agent] || {}).squad === fsq.value);
    if (fv.value) f = f.filter((x) => rank[x.severity] <= rank[fv.value]);
    f.sort((a, b) => rank[a.severity] - rank[b.severity] || b.last_seen - a.last_seen);
    const c = {}; f.forEach((x) => { c[x.severity] = (c[x.severity] || 0) + 1; });
    sum.textContent = f.length ? `${f.length} shown: ` + SEVS.filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(", ") : "";
    list.replaceChildren(...(f.length ? f.slice(0, 150).map((x) => findingCard(x, load)) : [el("p", "empty", mode === "open" ? "Nothing needs a look. The crew will add things here as they notice them." : "Nothing matches these filters.")]));
  };
  [fsq, fa, fv].forEach((s) => s.addEventListener("change", load));
  await load();
  view.append(bar, sum, list);
}

export async function audit(view, ctx) {
  const rows = await api("/api/audit?limit=200"), v = await api("/api/audit/verify");
  const t = el("table", "tbl"); const hd = el("tr"); ["#", "Time", "Who", "Action", "Target", "Detail"].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  rows.forEach((x) => { const tr = el("tr"); tr.append(el("td", null, String(x.id)), el("td", null, clock(x.ts)), el("td", null, x.actor), el("td", null, x.action), el("td", null, x.target), el("td", "dim", x.detail)); t.append(tr); });
  view.append(panel("Audit log", el("p", v.ok ? "ok" : "bad", v.ok ? "Hash chain intact: no row has been edited or removed." : "HASH CHAIN BROKEN at row #" + v.broken_at), t));
}

export async function settings(view, ctx) {
  const c = await api("/api/config"), g = await api("/api/gateway"), hist = await api("/api/config/history");
  const [ntf, mt, ags] = await Promise.all([api("/api/notify"), api("/api/maintenance"), api("/api/agents")]);
  const [frs, acs, acl] = await Promise.all([api("/api/frontier"), api("/api/actions/status"), api("/api/actions")]);
  view.append(await advisorsPanel(c), chatPanel(c), notifyPanel(ntf, c.effective), frontierPanel(frs), actionsPanel(acs, acl), maintenancePanel(mt, ags.map((a) => a.name)), tuningPanel(ags, c.effective));
  const gt = el("div", "tiles"); gt.append(tile("Queue", g.queue.length + " waiting" + (g.current ? " · running " + g.current.agent : "")), tile("Loaded models", g.loaded_models.join(", ") || "none seen"), tile("Model swaps", g.quiet_now ? "allowed now" : "only in " + g.quiet_hours));
  const ut = el("table", "tbl"); const uh = el("tr"); ["Agent", "Calls", "Tokens", "Avg ms", "Errors"].forEach((x) => uh.append(el("th", null, x))); ut.append(uh);
  g.usage_today.forEach((u) => { const tr = el("tr"); [u.agent, u.calls, u.tokens, u.avg_ms, u.errors].forEach((x) => tr.append(el("td", null, String(x ?? 0)))); ut.append(tr); });
  view.append(panel("LLM gateway", gt, el("p", "dim", "Token use today per agent"), ut));
  const ta = el("textarea"); ta.rows = 16; ta.spellcheck = false; const {version, updated_at, note, ...body} = c.config; ta.value = JSON.stringify(Object.keys(body).length ? body : {}, null, 2);
  const nt = el("input"); nt.placeholder = "What did you change? (optional)"; const msg = el("p", "dim");
  const row = el("div", "row"); row.append(btn("Save config", async () => { try { await send("/api/config", {config: JSON.parse(ta.value), note: nt.value}, "PUT"); location.reload(); } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } }));
  const ht = el("div"); hist.forEach((h) => { const r = el("div", "hist"); r.append(el("b", null, "v" + h.version), el("span", "dim", (h.updated_at || "").replace("T", " ").slice(0, 19) + " " + (h.note || "")), btn("roll back", async () => { if (confirm(`Roll back to v${h.version}? It becomes a new version.`)) { await send("/api/config/rollback", {version: h.version}); location.reload(); } }, "mini")); ht.append(r); });
  const adv = el("details", "howto"); adv.append(el("summary", null, `Advanced: raw settings file, defaults and history (version ${version || 0})`),
    el("p", "dim", "Only the keys you set are stored; everything else uses the defaults. Every save is a new version you can roll back to."), ta, nt, row, msg,
    el("h3", null, "Defaults"), el("pre", "code", JSON.stringify(c.defaults, null, 2)), el("h3", null, "History"), ht);
  view.append(adv);
}

async function advisorsPanel(c) {
  const s = await api("/api/advisors"), {version, updated_at, note, ...body} = c.config, cur = c.effective.advisors;
  const p = panel(null); const h = el("h2", null, "Outside advisors"); h.append(el("span", "hint", "hosted models that plan, review and coach the local crew")); p.append(h);
  p.append(el("p", "dim", "They only ever answer with text: no keys, no shell, no way to change the lab. What they see is filtered (outside addresses masked, secrets removed) and every call is logged below with its cost. Keys live only in the server's env file (OPENAI_API_KEY, GEMINI_API_KEY); Claude is set up in its own panel further down."));
  const grid = el("div", "advgrid"), edits = {};
  for (const a of s.advisors) {
    const m = who(a.name), card = el("div", "advcard" + (a.enabled ? "" : " off"));
    const top = el("div", "rtop"); if (m.look) top.append(pp(m.look, 40)); const nm = el("div"); nm.append(el("b", "call", m.callsign), el("span", "dim", a.provider)); top.append(nm); card.append(top);
    const spent = a.budget_usd ? Math.min(100, (a.spent_usd / a.budget_usd) * 100) : 0;
    const bar = el("div", "budget"); const fill = el("span"); fill.style.width = spent.toFixed(1) + "%"; if (spent > 85) fill.className = "hot"; bar.append(fill);
    card.append(el("div", "meta", a.budget_usd != null ? `$${a.spent_usd.toFixed(2)} of $${Number(a.budget_usd).toFixed(2)} this month · ${a.calls_month} calls` : `${a.calls_month} calls this month`), bar,
                el("div", "meta", `${a.calls_today} of ${a.daily_cap ?? "?"} calls today · model ${a.model || "?"}`));
    card.append(el("p", a.blocked_because ? "bad small" : "ok small", a.blocked_because ? "Not available: " + a.blocked_because + (a.last_error ? ` (last error: ${a.last_error})` : "") : "Ready"));
    const row = el("div", "row");
    if (a.name !== "claude") {
      const on = el("input"); on.type = "checkbox"; on.checked = cur[a.name].enabled; const l = el("label", "inline"); l.append(on, " switched on");
      const model = el("input"); model.value = cur[a.name].model; model.setAttribute("aria-label", "Model"); model.style.width = "150px";
      const bud = el("input"); bud.type = "number"; bud.min = 0; bud.step = 1; bud.value = cur[a.name].monthly_budget_usd; bud.setAttribute("aria-label", "Monthly budget in dollars"); bud.style.width = "80px";
      edits[a.name] = {on, model, bud};
      row.append(l, model, el("span", "inline dim", "$/month"), bud);
      if (!a.key_set) card.append(el("p", "small dim", `Add ${a.name === "openai" ? "OPENAI_API_KEY" : "GEMINI_API_KEY"}=... to /etc/prxy-command/env on LXC 139, then restart the service.`));
    } else row.append(el("span", "dim small", "Claude is configured in the “Outside model (Claude)” panel below."));
    const msg = el("span", "small dim");
    row.append(btn("Test", async () => { msg.textContent = "Testing…"; try { const r = await send(`/api/advisors/${a.name}/test`, {}); msg.textContent = `Works (${r.model}).`; } catch (e) { msg.textContent = "Failed: " + e.message; } }, "ghost"), msg);
    card.append(row); grid.append(card);
  }
  p.append(grid);
  const JOBS = [["plan", "Plans investigations for the crew"], ["review", "Second opinion before you approve"], ["verify", "Checks a fix really worked"], ["coach", "Daily coaching: starts investigations"], ["research", "Fact-checks research answers"]];
  const opts = [["claude", "Sage (Claude)"], ["openai", "Atlas (OpenAI)"], ["gemini", "Nova (Gemini)"], ["panel", "All of them (panel)"]];
  const jg = el("div", "form-grid"), jsel = {};
  JOBS.forEach(([k, label]) => { const sel = el("select"); opts.forEach(([v, t]) => { if (v === "panel" && k !== "review") return; sel.append(Object.assign(el("option", null, t), {value: v})); }); sel.value = cur.jobs[k]; jsel[k] = sel; const f = el("label", "field"); f.append(el("span", null, label), sel); jg.append(f); });
  const ct = el("input"); ct.type = "time"; ct.value = cur.coach_at; const cn = el("input"); cn.type = "number"; cn.min = 0; cn.max = 5; cn.value = cur.coach_max_projects;
  const f1 = el("label", "field"); f1.append(el("span", null, "Coaching time"), ct, el("small", null, "Clear to turn daily coaching off.")); const f2 = el("label", "field"); f2.append(el("span", null, "Investigations per coaching (max)"), cn); jg.append(f1, f2);
  p.append(el("h3", null, "Who does what"), jg);
  const msg = el("span", "dim");
  const save = btn("Save advisor settings", async () => {
    const advisors = {...(body.advisors || {}), jobs: Object.fromEntries(Object.entries(jsel).map(([k, s2]) => [k, s2.value])), coach_at: ct.value || "", coach_max_projects: Number(cn.value)};
    for (const [n, e] of Object.entries(edits)) advisors[n] = {...((body.advisors || {})[n] || {}), enabled: e.on.checked, model: e.model.value.trim(), monthly_budget_usd: Number(e.bud.value)};
    try { await send("/api/config", {config: {...body, advisors}, note: "advisor settings"}, "PUT"); msg.textContent = "Saved."; } catch (e) { msg.textContent = "Not saved: " + e.message; }
  }, "primary");
  const coachNow = btn("Coach the crew now", async () => { try { const r = await send("/api/advisors/coach", {}); msg.textContent = `${who(r.advisor).callsign} started ${r.projects.length} investigation(s). See Projects.`; } catch (e) { msg.textContent = e.message; } }, "ghost");
  const r2 = el("div", "row"); r2.append(save, coachNow, msg); p.append(r2);
  const ut = el("table", "tbl"), uh = el("tr"); ["Advisor", "Job", "Calls", "Tokens", "Cost this month"].forEach((x) => uh.append(el("th", null, x))); ut.append(uh);
  s.usage.sort((a, b) => b.cost_usd - a.cost_usd).forEach((u) => { const tr = el("tr"); [who(u.advisor).callsign, u.job, u.calls, u.tokens, "$" + u.cost_usd.toFixed(4)].forEach((x) => tr.append(el("td", null, String(x)))); ut.append(tr); });
  const rt = el("table", "tbl"), rh = el("tr"); ["When", "Advisor", "Job", "Model", "Tokens", "Cost", "Result"].forEach((x) => rh.append(el("th", null, x))); rt.append(rh);
  s.recent.forEach((x) => { const tr = el("tr"); [ago(x.ts), who(x.advisor).callsign, x.job, x.model || "", x.tokens, "$" + x.cost_usd.toFixed(5), x.ok ? "ok" : (x.error || "failed")].forEach((v, i) => tr.append(el("td", i === 6 && !x.ok ? "bad" : "", String(v)))); rt.append(tr); });
  const d = el("details", "fold"); d.append(el("summary", null, "How the money was used"), s.usage.length ? ut : el("p", "dim", "No calls this month yet."), el("h3", null, "Last 25 calls"), rt);
  p.append(d);
  return p;
}

function chatPanel(c) {
  const cur = c.effective.chat, {version, updated_at, note, ...body} = c.config;
  const field = (label, input, help) => { const l = el("label", "field"); l.append(el("span", null, label), input); if (help) l.append(el("small", null, help)); return l; };
  const tog = (v) => { const i = el("input"); i.type = "checkbox"; i.checked = v; return i; };
  const num = (v, min, max) => { const i = el("input"); i.type = "number"; i.min = min; i.max = max; i.value = v; return i; };
  const on = tog(cur.enabled), voice = tog(cur.voice), su = el("input"); su.type = "time"; su.value = cur.standup_at;
  const ci = num(cur.checkin_min, 0, 1440), rc = num(cur.recap_after_h, 1, 168);
  const msg = el("span", "dim");
  const save = btn("Save crew chat settings", async () => {
    const chat = {...(body.chat || {}), enabled: on.checked, voice: voice.checked, standup_at: su.value || "", checkin_min: Number(ci.value), recap_after_h: Number(rc.value)};
    try { await send("/api/config", {config: {...body, chat}, note: "crew chat settings"}, "PUT"); msg.textContent = "Saved."; } catch (e) { msg.textContent = "Not saved: " + e.message; }
  }, "primary");
  const grid = el("div", "form-grid");
  grid.append(field("The crew talks in the break room", on, "Lines come only from real events. Nothing is invented."),
    field("Let the local model add personality", voice, "Only when the model is already loaded. A line that changes a fact is thrown away."),
    field("Daily stand-up", su, "Clear the time to turn it off."),
    field("Squad leads check in every (minutes)", ci, "0 turns check-ins off."),
    field("Welcome-back recap after (hours away)", rc));
  const row = el("div", "row"); row.append(save, msg);
  return panel("Crew chat", grid, row);
}

const VERDICT = {agree: ["ok", "no objection"], caution: ["warn", "go carefully"], disagree: ["stop", "wait"]};
const firstSentence = (t, n = 170) => { const s = (t || "").split(/(?<=[.!?])\s/)[0]; return s.length > n ? s.slice(0, n - 1) + "…" : s; };

function proposalCard(f, who) {
  const a = f.proposed_action, c = f.critic, m = who[f.agent] || {callsign: f.agent, title: f.agent};
  const card = el("article", "prop sev-" + f.severity);
  const head = el("div", "prop-head");
  const h = el("h3"); h.append(sevPill(f.severity), f.title);
  const by = el("p", "prop-by"); if (m.look) by.append(pp(m.look, 22)); by.append(el("span", "call sq-" + (m.squad || "none"), m.callsign), ` (${m.title}) suggests this · risk ${a.risk || "unknown"} · first raised ${ago(f.ts)}`);
  head.append(h, by); card.append(head);
  card.append(el("p", "prop-sum", firstSentence(f.summary)));
  if (c) {
    const [cls, word] = VERDICT[c.verdict] || ["", c.verdict];
    const v = el("div", "verdict " + cls);
    const why = (c.blockers[0] || c.concerns[0] || c.verify_first[0] || "");
    v.append(el("b", null, `${who.critic ? who.critic.callsign : "The Critic"} says: ${word}.`), " " + why);
    if (c.outside) {
      const panel = c.outside.panel || [{advisor: "claude", verdict: c.outside.verdict}];
      const row = el("div", "advrow"); panel.forEach((x) => { const a = crewSync()[x.advisor] || {callsign: x.advisor}, chip = el("span", "advchip " + (VERDICT[x.verdict] || [""])[0]); if (a.look) chip.append(pp(a.look, 18)); chip.append(`${a.callsign}: ${(VERDICT[x.verdict] || ["", x.verdict])[1]}`); chip.title = (x.concerns || []).join(" "); row.append(chip); });
      v.append(row);
    }
    card.append(v);
  }
  const fold = (title, kids, open = false) => { const d = el("details", "fold"); d.open = open; d.append(el("summary", null, title), ...kids); return d; };
  const list = (items) => { const ol = el("ol"); (items || []).forEach((x) => ol.append(el("li", null, x))); return ol; };
  card.append(fold(`What you would do (${(a.steps || []).length} steps)`, [list(a.steps)]),
              fold(`How to undo it (${(a.rollback || []).length} steps)`, [list(a.rollback)]));
  if (c) card.append(fold("Second opinions in full", [criticBlock(c)]));
  const more = el("a", null, "Open the finding, its evidence and history"); more.href = "#/finding/" + f.id;
  card.append(fold("Full details", [el("p", "dim", f.summary), more]));
  if (a.execute) { const slot = el("div"); card.append(slot); executeBlock(f, () => location.reload()).then((x) => slot.append(x)); }
  const bar = el("div", "prop-actions"), ack = el("input"); ack.type = "checkbox"; ack.id = "ack-" + f.id;
  const lab = el("label", "inline"); lab.htmlFor = ack.id; lab.append(ack, " I've read the steps and how to undo them");
  const note = el("input"); note.placeholder = "Note (optional)"; note.setAttribute("aria-label", "Note");
  const go = btn("Approve: I'll do it", async () => { if (!ack.checked) { alert("Tick the box first: it confirms you read how to undo it."); return; } await send(`/api/findings/${f.id}/approve`, {ack: true, note: note.value}); location.reload(); }, c && c.verdict === "disagree" ? "" : "primary");
  bar.append(missionButton(f));
  bar.append(lab, note, go, btn("Not needed", async () => { if (confirm("Reject this? The crew stops suggesting it for 30 days.")) { await send(`/api/findings/${f.id}/reject`, {note: note.value}); location.reload(); } }, "ghost"));
  card.append(bar);
  return card;
}

export async function approvals(view) {
  const [d, who, ms] = await Promise.all([api("/api/approvals"), crewInfo(), api("/api/missions")]);
  const waiting = ms.filter((m) => m.status === "ready" || m.status === "needs_revision");
  const running = ms.filter((m) => m.status === "running" || m.status === "approved" || m.status === "planning");
  const reloadM = () => location.reload();
  if (waiting.length) view.append(panel(`Missions waiting for your approval (${waiting.length})`, el("p", "dim", "Sage (Claude) leads each mission, Atlas (OpenAI) and Nova (Gemini) review it. Approve once and the team carries it out, checking every step and undoing it if a check fails."), ...waiting.map((m) => missionCard(m, reloadM))));
  if (running.length) { const det = el("details", "howto"); det.open = true; det.append(el("summary", null, `Missions in progress (${running.length})`), ...running.map((m) => missionCard(m, reloadM))); view.append(det); }
  const intro = el("details", "howto"); intro.append(el("summary", null, "How approvals work"),
    el("p", "dim", "The crew never changes anything on its own. Each card is a suggestion with the exact steps and a way to undo them. Approving means you'll carry the steps out yourself (or, for the few actions allowed in Settings, the crew does it after you approve, checks the result and undoes it if the check fails). The Critic and, if switched on, Claude give a second opinion first."));
  view.append(intro);
  const rank = {disagree: 2, caution: 1, agree: 0}, sev = {critical: 0, high: 1, medium: 2, low: 3, info: 4};
  const pend = [...d.pending].sort((x, y) => (rank[(x.critic || {}).verdict] || 0) - (rank[(y.critic || {}).verdict] || 0) || sev[x.severity] - sev[y.severity]);
  const ready = pend.filter((f) => (f.critic || {}).verdict !== "disagree"), hold = pend.filter((f) => (f.critic || {}).verdict === "disagree");
  if (!pend.length) view.append(panel("Nothing to decide", el("p", "dim", "When the crew suggests a change it appears here with its steps and an undo plan.")));
  if (ready.length) view.append(panel(`Ready for your decision (${ready.length})`, ...ready.map((f) => proposalCard(f, who))));
  if (hold.length) view.append(panel(`On hold: the Critic says wait (${hold.length})`, el("p", "dim", "These can still be approved, but something else should be fixed first. The reason is on each card."), ...hold.map((f) => proposalCard(f, who))));
  const t = el("table", "tbl"); const hd = el("tr"); ["When", "Suggested by", "What", "Status", ""].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  d.history.forEach((h) => {
    const tr = el("tr"), last = el("td"), lk = el("a", null, (h.action.steps || [])[0] || h.kind); lk.href = "#/finding/" + h.finding_id; const pl = el("td"); pl.append(lk);
    if (h.status === "approved") last.append(btn("I did it", async () => { const n = prompt("Note (optional)") ?? ""; await send(`/api/approvals/${h.id}/complete`, {note: n}); location.reload(); }));
    if (h.status === "done") last.append(btn("I undid it", async () => { if (confirm("Undo plan:\n" + (h.action.rollback || []).join("\n") + "\n\nMark as undone?")) { await send(`/api/approvals/${h.id}/rolled-back`, {}); location.reload(); } }, "mini"));
    tr.append(el("td", null, clock(h.created)), el("td", null, (who[h.agent] || {}).callsign || h.agent), pl, el("td", h.status === "rejected" ? "bad" : h.status === "done" ? "ok" : "", h.status + (h.note ? ": " + h.note : "")), last); t.append(tr);
  });
  const hist = el("details", "howto"); hist.append(el("summary", null, `Past decisions (${d.history.length})`), d.history.length ? t : el("p", "dim", "No decisions yet."));
  view.append(hist);
}
export async function briefing(view, ctx, id) {
  const list = await api("/api/briefings?limit=30");
  const b = id ? await api("/api/briefing/" + id) : await api("/api/briefing/latest");
  const bar = el("div", "row");
  const gen = btn("Generate a new briefing now", async () => { await send("/api/briefing/generate", {}); await new Promise((r) => setTimeout(r, 6000)); location.hash = "#/briefing"; location.reload(); });
  const pick = el("select"); list.forEach((x) => { const o = el("option", null, `${x.date} ${new Date(x.ts * 1000).toLocaleTimeString()} (${x.trigger})`); o.value = x.id; pick.append(o); });
  if (b.id) pick.value = b.id; pick.addEventListener("change", () => { location.hash = "#/briefing/" + pick.value; });
  bar.append(gen, list.length ? pick : el("span", "dim", "No briefings yet"));
  view.append(panel("Daily briefing", el("p", "dim", "The Commander groups duplicate findings, ranks what is left, and writes one briefing a day (after 07:00 local). Every item links to its evidence."), bar));
  if (!b.doc) { view.append(panel(null, el("p", null, "No briefing has been written yet. Press the button above."))); return; }
  const d = b.doc;
  view.append(panel(`${d.date} · generated ${clock(d.generated)}`, el("p", "headline", d.headline)));
  const t = el("div", "tiles"); const c = d.counts;
  t.append(tile("Needs attention", `${c.topics_attention} topic(s)`, c.topics_attention === 0), tile("Watch list", `${c.topics_watch} topic(s)`), tile("Open findings", Object.entries(c.by_severity).map(([k, v]) => v + " " + k).join(", ") || "none"),
    tile("New in 24 h", String(c.new_24h)), tile("Cleared on their own (24 h)", String(c.resolved_24h)), tile("You rejected (7 d)", String(c.rejected_7d)));
  view.append(t);
  const top = el("div");
  d.top.forEach((g) => {
    const card = el("div", "fnd"); const h = el("div", "t"); h.append(sevPill(g.severity), ...(g.verdict === "disagree" ? [Object.assign(el("span", "sev high", "disputed"))] : g.verdict === "caution" ? [el("span", "sev medium", "caution")] : []), g.title, el("span", "grp", g.agents.join(", ")));
    card.append(h, el("div", "s", g.summary));
    if (g.related.length) { const r = el("div", "meta"); r.append(`${g.related.length} related: `); g.related.forEach((x, i) => { const a = el("a", null, x.title); a.href = "#/finding/" + x.id; r.append(a, i < g.related.length - 1 ? " · " : ""); }); card.append(r); }
    if (g.action) { const det = el("details"); det.append(el("summary", null, `Proposed action (${g.action.kind}, risk ${g.action.risk})`)); g.action.steps.forEach((st) => det.append(el("div", "ev", st))); card.append(det); }
    const row = el("div", "row"); g.finding_ids.slice(0, 4).forEach((fid, i) => { const a = el("a", "ghost-link", i === 0 ? "Open the finding and its evidence" : "Related #" + (i + 1)); a.href = "#/finding/" + fid; row.append(a); }); card.append(row);
    top.append(card);
  });
  view.append(panel("Needs attention", ...(d.top.length ? [top] : [el("p", "ok", "Nothing above low severity is open.")])));
  if (d.standing && d.standing.length) { const w = el("div"); d.standing.forEach((x) => { const a = el("a", null, x.title); a.href = "#/finding/" + x.finding_ids[0]; const p = el("div", "ev"); p.append(sevPill(x.severity), a, el("span", "dim", "  " + x.agents.join(", "))); w.append(p); }); view.append(panel("Standing conditions: open and unchanged for two weeks (still real, but not news)", w)); }
  if (d.blind_spots.length) { const bl = el("div"); d.blind_spots.forEach((x) => bl.append(el("p", "bad", `${who(x.agent).callsign}: ${x.issue}`))); view.append(panel("Blind spots: things the team cannot currently see", bl)); }
  if (d.watch.length) { const w = el("div"); d.watch.forEach((x) => { const a = el("a", null, x.title); a.href = "#/finding/" + x.finding_ids[0]; const p = el("div", "ev"); p.append(sevPill(x.severity), a, el("span", "dim", "  " + x.agents.join(", "))); w.append(p); }); view.append(panel("Watch list (low priority)", w)); }
  if (d.since_last.resolved.length) { const r = el("div"); d.since_last.resolved.forEach((x) => r.append(el("div", "ev", x))); view.append(panel("Cleared on their own in the last 24 h", r)); }
  if (d.decisions.rejected.length) { const r = el("div"); d.decisions.rejected.forEach((x) => r.append(el("div", "ev", `${x.title}  (${ago(x.when)})`))); view.append(panel("You rejected these recently (they will not be raised again for 30 days)", r)); }
  const at = el("table", "tbl"); const hd = el("tr"); ["Agent", "State", "Last run", "Open"].forEach((x) => hd.append(el("th", null, x))); at.append(hd);
  d.agents.forEach((x) => { const tr = el("tr"); const lk = el("a", null, x.name); lk.href = "#/agent/" + x.name; const td = el("td"); td.append(lk); tr.append(td, el("td", null, x.state), el("td", null, ago(x.last_run)), el("td", null, String(x.open))); at.append(tr); });
  view.append(panel("The team", at));
}

export async function findingPage(view, ctx, id) {
  const f = await api("/api/findings/" + encodeURIComponent(id));
  const reload = () => { location.reload(); };
  view.append(panel("Finding", findingCard(f, reload)));
  const tl = await api(`/api/memory/timeline?agent=${encodeURIComponent(f.agent)}&fingerprint=${encodeURIComponent(f.fingerprint)}`);
  const t = el("table", "tbl"); const hd = el("tr"); ["First seen", "Last seen", "Times", "Severity", "Status"].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  tl.forEach((x) => { const tr = el("tr"); tr.append(el("td", null, clock(x.ts)), el("td", null, clock(x.last_seen)), el("td", null, String(x.count)), el("td", null, x.severity), el("td", null, x.status + (x.decided_at ? " (" + ago(x.decided_at) + ")" : ""))); t.append(tr); });
  view.append(panel("History of this finding (the Archivist's memory)", t));
}

export async function morning(view) {
  const b = await api("/api/briefing/latest?kind=personal");
  const gen = btn("Write this morning's brief now", async () => { await send("/api/briefing/generate?kind=personal", {}); await new Promise((r) => setTimeout(r, 15000)); location.reload(); });
  view.append(panel("Morning brief", el("p", "dim", "Weather, calendar, news headlines and the lab in one place, written at 06:15. Facts come from their sources; the greeting is checked so it cannot add numbers."), gen));
  if (!b.doc) { view.append(panel(null, el("p", null, "No brief yet. Press the button above."))); return; }
  const d = b.doc;
  view.append(panel(`${d.date} · written ${clock(d.generated)}`, el("p", "headline", d.headline)));
  if (d.weather) { const w = d.weather, t = el("div", "tiles"); t.append(tile("Now", `${w.now} °F, ${w.sky}`), tile("Today", `${w.low} to ${w.high} °F`), tile("Rain chance", w.rain + " %", w.rain < 50), tile("Wind", w.wind + " mph")); view.append(panel("Weather in " + d.place, t)); }
  const cal = el("div"); if (d.calendar.configured) { if (!d.calendar.events.length) cal.append(el("p", "dim", "Nothing in the next 36 hours.")); d.calendar.events.forEach((e) => cal.append(el("div", "ev", `${e.when}  ${e.title}`))); } else cal.append(el("p", "dim", "Not connected yet."));
  view.append(panel("Calendar", cal));
  d.news.forEach((n) => { const l = el("div"); n.items.forEach((i) => { const a = el("a", null, i.title); a.href = i.link; a.target = "_blank"; a.rel = "noopener noreferrer"; const r = el("div", "ev"); r.append(a); l.append(r); }); view.append(panel(n.source, l)); });
  view.append(panel("Lab", el("p", null, d.lab.headline || "No lab briefing yet.")));
  if (d.gaps.length) { const g = el("div"); d.gaps.forEach((x) => g.append(el("p", "dim", x))); view.append(panel("What this brief could not include", g)); }
}

const CHECK = {supported: ["ok", "checks out"], partly: ["warn", "partly right, corrected"], unsupported: ["stop", "not supported by the sources, rewritten"]};
function deskCard(n) {
  const d = n.doc, card = el("article", "prop");
  const h = el("h3", null, d.question); card.append(h);
  const live = {searching: "Searching the web…", reading: "Reading the top pages…", writing: "Writing the answer…", checking: "An advisor is checking the answer…"}[d.status];
  if (live) { card.append(el("p", "rstate", live)); return card; }
  if (d.status === "failed") { card.append(el("p", "bad", "Couldn't finish: " + (d.error || "unknown problem"))); return card; }
  const by = el("p", "prop-by"); const m = crewSync()[d.by] || crewSync().research_librarian || {callsign: "Shelf"};
  if (m.look) by.append(pp(m.look, 22)); by.append(el("span", "call", m.callsign), ` wrote this · ${ago(d.finished || n.ts)} · ${d.sources.filter((x) => x.read).length} pages read`);
  card.append(by);
  card.append(el("p", "prop-sum", d.answer || "No answer passed the citation check, so only the sources are shown below."));
  if (d.check && d.check.verdict) {
    const a = crewSync()[d.check.advisor] || {callsign: d.check.advisor};
    const [cls, word] = CHECK[d.check.verdict] || ["", d.check.verdict];
    const v = el("div", "verdict " + cls); v.append(el("b", null, `${a.callsign}'s fact-check: ${word}.`), " " + (d.check.notes || "")); card.append(v);
  } else if (d.answer) card.append(el("p", "meta", "Not fact-checked (no outside advisor was available)."));
  const src = el("details", "fold"); src.append(el("summary", null, `Sources (${d.sources.length})`));
  d.sources.forEach((x, i) => { const row = el("div", "ev"); const a = el("a", null, `[${i + 1}] ${x.title}`); a.href = x.url; a.target = "_blank"; a.rel = "noopener noreferrer"; row.append(a, el("div", "dim", (x.read ? "" : "(snippet only) ") + x.extract.slice(0, 400) + (x.extract.length > 400 ? "…" : ""))); src.append(row); });
  card.append(src);
  if (d.local_answer && d.answer !== d.local_answer) { const l = el("details", "fold"); l.append(el("summary", null, "Shelf's first draft (before the fact-check)"), el("p", "dim", d.local_answer)); card.append(l); }
  return card;
}

export async function research(view, ctx) {
  const ask = panel(null); const hh = el("h2", null, "Ask the crew"); hh.append(el("span", "hint", "they search the web, read the pages, answer with sources, and an advisor checks it")); ask.append(hh);
  const form = el("form", "compose"), q = el("input"), go = el("button", "primary", "Research it"); q.placeholder = "e.g. What is the safest way to upgrade Proxmox 9.1 to 9.2?"; q.maxLength = 300; q.setAttribute("aria-label", "Research question"); go.type = "submit";
  form.append(q, go); ask.append(form, el("p", "compose-hint", "Only your question leaves the lab (through your own SearXNG). Pages are read from the public web only; links into your network are refused."));
  const deskList = el("div");
  const loadDesk = async () => { const items = await api("/api/research/desk?limit=15"); keyedList(deskList, items, (n) => n.id, (n) => n.doc.status + (n.doc.finished || ""), deskCard, el("p", "empty", "No questions yet. Ask one above.")); };
  form.addEventListener("submit", async (e) => { e.preventDefault(); if (q.value.trim().length < 8) return; go.disabled = true; try { await send("/api/research/ask", {question: q.value.trim()}); q.value = ""; await loadDesk(); } catch (err) { alert(err.message); } go.disabled = false; });
  ask.append(deskList); view.append(ask);
  await loadDesk();
  const off = ctx && ctx.onEvent ? ctx.onEvent((ev) => { if (ev.type === "research.done" || ev.type === "research.asked") loadDesk(); }) : null;
  const poll = setInterval(loadDesk, 8000);
  const topics = await api("/api/research/topics"), notes = (await api("/api/research?limit=40")).filter((n) => n.topic !== "desk");
  const list = el("div"), rows = [];
  const addRow = (t) => { const r = el("div", "row"); const n = el("input"), q = el("input"), e = el("input"); n.value = t.name; q.value = t.query; e.value = t.every_h; e.type = "number"; e.min = 6; e.style.width = "90px"; n.placeholder = "name"; q.placeholder = "search query"; q.style.flex = "1"; r.append(n, q, el("span", "dim", "every (h)"), e, btn("Remove", async () => { r.remove(); rows.splice(rows.indexOf(r), 1); }, "mini")); r._get = () => ({id: t.id, name: n.value, query: q.value, every_h: Number(e.value), last: t.last || 0}); rows.push(r); list.append(r); };
  topics.forEach(addRow);
  const msg = el("p", "dim");
  const save = async () => { try { await send("/api/research/topics", {topics: rows.map((r) => r._get())}, "PUT"); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } };
  view.append(panel("Topics the Librarian follows", el("p", "dim", "It searches the private SearXNG for each topic and writes a short digest in which every sentence cites a numbered source. If the digest cannot be verified, only the sources are kept."), list,
    el("div", "row"), msg));
  const bar = view.querySelector(".panel:last-child .row:last-of-type");
  bar.append(btn("Add a topic", async () => addRow({id: "", name: "", query: "", every_h: 168})), btn("Save topics", save), btn("Research all topics now", async () => { await save(); await send("/api/agents/research_librarian/run", {}); await new Promise((r) => setTimeout(r, 20000)); location.reload(); }));
  const seen = new Set();
  notes.forEach((n) => {
    if (seen.has(n.topic)) return; seen.add(n.topic);
    const t = topics.find((x) => x.id === n.topic), box = el("div");
    box.append(el("p", n.doc.digest ? "" : "dim", n.doc.digest || "No verified digest for this run: only the sources are shown."));
    n.doc.sources.forEach((s, i) => { const a = el("a", null, `[${i + 1}] ${s.title}`); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer"; const d = el("div", "ev"); d.append(a, el("div", "dim", s.snippet)); box.append(d); });
    if (n.doc.wiki) box.append(el("p", n.doc.wiki.ok ? "ok" : "bad", "Wiki.js: " + (n.doc.wiki.ok ? "filed" : n.doc.wiki.message)));
    else box.append(el("p", "dim", "Not filed in Wiki.js (needs a Wiki.js API token in the Command environment)."));
    view.append(panel(`${t ? t.name : n.topic} · ${n.doc.date}`, box));
  });
  if (!notes.length) view.append(panel("Notes", el("p", "dim", "Nothing researched yet. Press 'Research all topics now'.")));
  return () => { clearInterval(poll); if (off) off(); };
}

export async function backlog(view) {
  const all = await api("/api/backlog"), now = Date.now() / 1000;
  const run = btn("Re-read the handoff documents now", async () => { await send("/api/agents/backlog_keeper/run", {}); await new Promise((r) => setTimeout(r, 6000)); location.reload(); });
  const open = all.filter((t) => t.status === "open"), done = all.filter((t) => t.status === "done").slice(-15).reverse();
  view.append(panel("Backlog", el("p", "dim", "Built from the pending / next / open-question lists in the handoff documents. A task closes when a later version of the document no longer lists it. Open for more than a week = stuck."), run));
  const groups = {}; open.forEach((t) => { (groups[t.doc] = groups[t.doc] || []).push(t); });
  Object.entries(groups).forEach(([doc, ts]) => {
    const box = el("div");
    ts.forEach((t) => { const days = Math.floor((now - t.first_seen) / 86400), r = el("div", "ev" + (days > 7 ? " stuckrow" : "")); r.append(el("b", null, t.section), el("div", null, t.text), el("div", "meta", `open ${days} day${days === 1 ? "" : "s"}${days > 7 ? " · STUCK" : ""}`), btn("Not a task", async () => { await send(`/api/backlog/${t.id}/dismiss`, {}); location.reload(); }, "mini")); box.append(r); });
    view.append(panel(`${doc.replace(".md", "")} (${ts.length} open)`, box));
  });
  if (!open.length) view.append(panel("Open tasks", el("p", "dim", "No open tasks found. Either everything is done, or the handoff documents have not been copied in yet.")));
  if (done.length) { const d = el("div"); done.forEach((t) => d.append(el("div", "ev", `${t.text}  (closed ${ago(t.closed_at)})`))); view.append(panel("Recently closed", d)); }
}

export async function media(view) {
  const d = await api("/api/media");
  const ta = el("textarea"); ta.rows = 8; ta.value = d.watchlist.join("\n"); ta.placeholder = "One show per line, e.g.\nSeverance\nThe Expanse"; const msg = el("p", "dim");
  const save = async () => { try { await send("/api/media/watchlist", {watchlist: ta.value.split("\n")}, "PUT"); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } };
  const row = el("div", "row"); row.append(btn("Save watchlist", save), btn("Find picks now", async () => { await save(); await send("/api/agents/media_curator/run", {}); await new Promise((r) => setTimeout(r, 25000)); location.reload(); }));
  view.append(panel("Your watchlist", el("p", "dim", "Type the shows you watch or want to watch. This is the only taste data the Curator uses: it does not read the TV's viewing history. Data comes from TVmaze."), ta, row, msg));
  const l = d.latest; if (!l) { view.append(panel("Picks", el("p", "dim", "No picks yet. Save a watchlist and press 'Find picks now'."))); return; }
  if (l.watchlist.length) { const t = el("table", "tbl"); const hd = el("tr"); ["Show", "Status", "Next episode"].forEach((x) => hd.append(el("th", null, x))); t.append(hd); l.watchlist.forEach((w) => { const tr = el("tr"); tr.append(el("td", null, w.title), el("td", null, w.status || ""), el("td", null, w.next || "none announced")); t.append(tr); }); view.append(panel("Your shows", t)); }
  const box = el("div"); l.picks.forEach((p) => { const a = el("a", null, p.show); a.href = p.url; a.target = "_blank"; a.rel = "noopener noreferrer"; const r = el("div", "ev"); r.append(a, el("div", "dim", `${p.why} · ${p.genres.join(", ")} · rated ${p.rating} · ${p.airdate}`)); box.append(r); });
  view.append(panel(l.personalised ? "Picks for you this week" : "Well rated this week (add a watchlist to personalise)", ...(l.picks.length ? [box] : [el("p", "dim", "Nothing matched this week.")])));
}

export async function careers(view) {
  const d = await api("/api/careers"), p = d.profile || {resume: "", titles: [], keywords: [], locations: [], remote_ok: true};
  view.append(panel("Read this first", el("p", null, "The Scout finds and scores roles, drafts tailored wording and keeps the tracker. It never applies for you: it holds no job-site logins and has no way to submit anything. Applying, and every status after 'applied', is yours. Tailored lines only re-word what is already in your resume; check them before you use them.")));
  const ta = el("textarea"); ta.rows = 10; ta.value = p.resume; ta.placeholder = "Paste your resume as plain text (stored only on this server)";
  const f = (v, ph) => { const i = el("input"); i.value = (v || []).join(", "); i.placeholder = ph; i.style.width = "100%"; return i; };
  const titles = f(p.titles, "Target job titles, comma separated (e.g. SOC analyst, security engineer, network engineer)"), kws = f(p.keywords, "Extra keywords (e.g. proxmox, suricata, ccna)"), locs = f(p.locations, "Locations you accept (e.g. San Luis Obispo, Santa Barbara)");
  const rem = el("input"); rem.type = "checkbox"; rem.checked = p.remote_ok; const rl = el("label", "inline"); rl.append(rem, " remote is fine");
  const msg = el("p", "dim"), split = (i) => i.value.split(",").map((x) => x.trim()).filter(Boolean);
  const save = async () => { try { await send("/api/careers/profile", {resume: ta.value, titles: split(titles), keywords: split(kws), locations: split(locs), remote_ok: rem.checked}, "PUT"); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } };
  const row = el("div", "row"); row.append(btn("Save profile", save), btn("Search now", async () => { await save(); await send("/api/agents/job_scout/run", {}); await new Promise((r) => setTimeout(r, 30000)); location.reload(); }),
    btn("Delete my profile and all tracked jobs", async () => { if (confirm("Delete the resume, the targets and every tracked job from this server?")) { await send("/api/careers/profile", {}, "DELETE"); location.reload(); } }, "ghost"));
  view.append(panel("Your profile", ta, titles, kws, locs, rl, row, msg));
  const by = {}; d.jobs.forEach((j) => { by[j.status] = (by[j.status] || 0) + 1; });
  const t = el("div", "tiles"); ["found", "drafted", "applied", "interview", "rejected"].forEach((k) => t.append(tile(k, String(by[k] || 0))));
  view.append(panel("Tracker", t));
  const list = el("div");
  d.jobs.forEach((j) => {
    const c = el("div", "fnd"), h = el("div", "t"), a = el("a", null, `${j.title} · ${j.company}`); a.href = j.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    h.append(el("span", "sev " + (j.score >= 70 ? "low" : "info"), j.score + "%"), a, el("span", "grp", j.source));
    c.append(h, el("div", "s", j.snippet || ""), el("div", "meta", `${j.location || ""} · posted ${j.posted || "?"} · matches: ${j.matched.join(", ") || "none"} · gaps: ${j.gaps.join(", ") || "none"}`));
    if (j.drafts) { const det = el("details"); det.append(el("summary", null, "Tailored draft (check before use)")); j.drafts.bullets.forEach((b) => { const x = el("div", "ev"); x.append(el("div", "dim", "Your line: " + b.original), el("div", null, b.changed ? "Tailored: " + b.tailored : "Kept as is.")); det.append(x); }); const L = el("div", "ev"); L.append(el("b", null, "Cover letter"), el("pre", null, j.drafts.letter || "Not drafted: the model's letter mentioned something not in your resume or the posting, so it was thrown away.")); det.append(L, el("p", "dim", j.drafts.note)); c.append(det); }
    const r = el("div", "row"), sel = el("select"); ["found", "drafted", "applied", "interview", "rejected", "dismissed"].forEach((s) => { const o = el("option", null, s); o.value = s; sel.append(o); }); sel.value = j.status;
    const note = el("input"); note.placeholder = "note"; note.value = j.notes || "";
    r.append(el("span", "dim", "Status:"), sel, note, btn("Save", async () => { await send(`/api/careers/jobs/${j.id}/status`, {status: sel.value, note: note.value, followup_days: sel.value === "applied" ? 7 : 0}); location.reload(); }, "mini"));
    if (!j.drafts) r.append(btn("Draft wording for this job", async () => { await send(`/api/careers/jobs/${j.id}/draft`, {}); await new Promise((x) => setTimeout(x, 25000)); location.reload(); }, "mini"));
    c.append(r); list.append(c);
  });
  view.append(panel(`Roles found (${d.jobs.length})`, ...(d.jobs.length ? [list] : [el("p", "dim", "Nothing yet. Save your profile and press 'Search now'.")])));
}


export async function council(view, ctx) {
  const d = await api("/api/incidents"), h = await api("/api/health");
  { const hw = el("details", "howto"); hw.append(el("summary", null, "How the council works"), el("p", "dim", "This is a multi-agent system: many autonomous agents share one environment (the findings store, the blackboard and the event stream). Collaboration: agents post weak observations on the blackboard and read each other's findings. The Correlator joins what several agents say about the same machine into one incident. Orchestration: the Commander routes work: it asks the right agents to look again and to scan that one machine. Debate: the Analyst (Correlator) and the Defender (Critic) argue in rounds and the Judge (Commander) decides; the rules set floors the judge cannot argue away. Every line of the debate is stored so you can read how the verdict came about.")); view.append(hw); }
  const open = d.incidents.filter((f) => f.status === "new" || f.status === "seen"), closed = d.incidents.filter((f) => !(f.status === "new" || f.status === "seen")).slice(0, 8);
  const list = el("div");
  const reload = () => location.reload();
  open.forEach((f) => { list.append(findingCard(f, reload)); const m = d.members[f.id] || []; if (m.length) { const mm = el("div", "meta"); mm.append("Members: "); m.forEach((x, i) => { const a = el("a", null, `${who(x.agent).callsign}: ${x.title.slice(0, 60)}`); a.href = "#/finding/" + x.id; mm.append(a, i < m.length - 1 ? " · " : ""); }); list.append(mm); } });
  view.append(panel(`Open incidents (${open.length})`, ...(open.length ? [list] : [el("p", "ok", "No machine has several agents reporting on it right now.")])));
  const sig = el("table", "tbl"), hd = el("tr"); ["Agent", "About", "Kind", "Strength", "Seen", "Note"].forEach((x) => hd.append(el("th", null, x))); sig.append(hd);
  d.signals.forEach((s) => { const tr = el("tr"); tr.append(el("td", null, s.agent), el("td", null, s.entity), el("td", null, s.kind), el("td", null, String(s.strength)), el("td", null, ago(s.ts)), el("td", "dim", s.text)); sig.append(tr); });
  view.append(panel("The blackboard: weak observations from the last hour (below the level of a finding)", ...(d.signals.length ? [sig] : [el("p", "dim", "Nothing small is being noticed right now.")])));
  if (closed.length) { const c = el("div"); closed.forEach((f) => { const a = el("a", null, f.title); a.href = "#/finding/" + f.id; const x = el("div", "ev"); x.append(sevPill(f.severity), a, el("span", "dim", `  ${f.status} · ${ago(f.last_seen)}${f.thread && f.thread.debate ? " · verdict " + f.thread.debate.verdict : ""}`)); c.append(x); }); view.append(panel("Recent past incidents", c)); }
}

async function patchConfig(patch) {
  const c = await api("/api/config"), {version, updated_at, note, ...body} = c.config;
  const merge = (a, b) => { const o = {...a}; for (const [k, v] of Object.entries(b)) o[k] = v && typeof v === "object" && !Array.isArray(v) && o[k] && typeof o[k] === "object" ? merge(o[k], v) : v; return o; };
  return send("/api/config", {config: merge(body, patch), note: "settings page"}, "PUT");
}

function notifyPanel(nt, cfg) {
  const p = nt.policy, box = el("div"), tiles = el("div", "tiles");
  tiles.append(tile("Discord", nt.discord ? "connected" : "not set up (DISCORD_WEBHOOK_URL)", nt.discord), tile("Phone push (ntfy)", nt.ntfy ? "connected" : "optional, not set up", true), tile("Last delivered", nt.last_ok ? ago(nt.last_ok) : "never", nt.fails < 3), tile("Failures in a row", String(nt.fails), nt.fails === 0), tile("Tracking", nt.tracking + " finding(s)"));
  const sel = (opts, val) => { const s = el("select"); opts.forEach(([v, l]) => { const o = el("option", null, l); o.value = v; s.append(o); }); s.value = val; return s; };
  const cb = (v, label) => { const i = el("input"); i.type = "checkbox"; i.checked = v; const l = el("label", "inline"); l.append(i, " " + label); return [i, l]; };
  const [en, enL] = cb(p.enabled, "notifications on"), [br, brL] = cb(p.daily_briefing, "one morning message a day"), [rs, rsL] = cb(p.resolved, "tell me when things clear");
  const sev = sel([["medium", "medium and above"], ["high", "high and above"], ["critical", "critical only"]], p.min_severity), men = sel([["@here", "@here (buzzes your phone)"], ["@everyone", "@everyone"], ["", "never mention anyone"]], p.mention);
  const esc = el("input"); esc.value = p.escalate_min.join(", "); esc.style.width = "180px";
  const msg = el("p", "dim");
  const row1 = el("div", "row"); row1.append(enL, brL, rsL);
  const row2 = el("div", "row"); row2.append(el("span", "dim", "Tell me right away about"), sev, el("span", "dim", "Remind me after (minutes)"), esc, el("span", "dim", "Reminders mention"), men);
  const row3 = el("div", "row");
  row3.append(btn("Save", async () => { try { await patchConfig({notify: {enabled: en.checked, daily_briefing: br.checked, resolved: rs.checked, min_severity: sev.value, mention: men.value, escalate_min: esc.value.split(",").map((x) => Number(x.trim())).filter((x) => x > 0)}}); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } }),
    btn("Send a test message", async () => { try { await send("/api/notify/test", {}); msg.textContent = "Sent. Check Discord."; msg.className = "ok"; } catch (e) { msg.textContent = "Could not send: " + e.message; msg.className = "bad"; } }, "ghost"));
  box.append(tiles, el("p", "dim", "A new finding at or above the level you pick is sent to Discord with a link. If nobody acknowledges it (opening it and pressing Seen, Reject or Done counts), you get reminders after the minutes listed, then daily. Messages carry the title and a short summary only: outside addresses are masked and evidence never leaves this server. Findings under a maintenance window are never announced."), row1, row2, row3, msg);
  if (nt.last_error) box.append(el("p", "bad", "Last error: " + nt.last_error));
  if (nt.recent.length) { const r = el("div", "dim"); r.append("Recently sent: " + nt.recent.slice(-5).reverse().map((x) => `${x.kind} (${ago(x.ts)})`).join(", ")); box.append(r); }
  return panel("Notifications and escalation", box);
}

function maintenancePanel(m, agentNames) {
  const box = el("div"), msg = el("p", "dim");
  const scope = el("input"); scope.placeholder = "what: lxc/131, 10.20.40.131, vlan 50, backups, or *"; scope.style.flex = "1";
  const mins = el("select"); [[30, "30 minutes"], [60, "1 hour"], [120, "2 hours"], [240, "4 hours"], [480, "8 hours"], [1440, "1 day"]].forEach(([v, l]) => { const o = el("option", null, l); o.value = v; mins.append(o); }); mins.value = 120;
  const ag = el("input"); ag.placeholder = "only these agents (blank = all)"; ag.style.width = "200px";
  const note = el("input"); note.placeholder = "what are you doing? (shown in the audit log)"; note.style.flex = "1";
  const row = el("div", "row"); row.append(scope, mins, ag, note, btn("Start a maintenance window", async () => {
    try { await send("/api/maintenance", {scope: scope.value.split(",").map((x) => x.trim()).filter(Boolean), minutes: Number(mins.value), agents: ag.value.split(",").map((x) => x.trim()).filter(Boolean), note: note.value}); location.reload(); } catch (e) { msg.textContent = e.message; msg.className = "bad"; } }));
  box.append(el("p", "dim", "Tell the team you are working on something. While a window is open, findings and observations about what you named are held back (and counted here), so routine changes do not fill the dashboard or ping your phone. The window ends by itself; findings about it return afterwards if the problem is still there."), row, msg);
  const act = m.active;
  if (act.length) { act.forEach((w) => { const r = el("div", "ev"); r.append(el("b", null, w.scope.join(", ")), ` · until ${clock(w.end)}${w.start > Date.now() / 1000 ? " (starts " + clock(w.start) + ")" : ""} · held back ${w.hits} · ${w.note || "no note"}${w.agents.length ? " · agents: " + w.agents.join(", ") : ""}  `, btn("End now", async () => { await send(`/api/maintenance/${w.id}/cancel`, {}); location.reload(); }, "mini")); box.append(r); }); }
  else box.append(el("p", "ok", "No maintenance window is open."));
  return panel("Maintenance windows (change awareness)", box);
}

function tuningPanel(agents, cfg) {
  const box = el("div"), msg = el("p", "dim");
  box.append(el("p", "dim", "The thresholds each agent uses. Raising one makes the agent quieter. If you keep rejecting the same kind of finding, the Archivist will suggest a change here too (on the Findings page). Every change is a new config version you can roll back."));
  const t = el("table", "tbl"), hd = el("tr"); ["Agent", "Setting", "Value", "Default", "Allowed", ""].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  agents.filter((a) => a.params).forEach((a) => Object.entries(a.params).forEach(([k, sp]) => {
    const tr = el("tr"), i = el("input"); i.type = "number"; i.step = "any"; i.value = sp.value; i.style.width = "90px"; const c = el("td"); c.append(i);
    const act = el("td"); act.append(btn("Save", async () => { try { await send(`/api/agents/${a.name}/config`, {params: {[k]: Number(i.value)}}); msg.textContent = `${a.title}: ${k} saved.`; msg.className = "ok"; } catch (e) { msg.textContent = e.message; msg.className = "bad"; } }, "mini"), btn("Default", async () => { await send(`/api/agents/${a.name}/config`, {params: {[k]: null}}); location.reload(); }, "mini"));
    tr.append(el("td", null, a.title), el("td", "dim", sp.label), c, el("td", "dim", String(sp.default)), el("td", "dim", `${sp.min} to ${sp.max}`), act); t.append(tr);
  }));
  const cm = el("input"); cm.value = cfg.gateway.council_model || ""; cm.placeholder = "e.g. phi4:latest (empty = the default model)"; cm.style.width = "300px";
  const row = el("div", "row"); row.append(el("span", "dim", "Council model for debates:"), cm, btn("Save", async () => { try { await patchConfig({gateway: {council_model: cm.value.trim()}}); msg.textContent = "Council model saved."; msg.className = "ok"; } catch (e) { msg.textContent = e.message; msg.className = "bad"; } }, "mini"),
    el("span", "dim", "A bigger model is only loaded in the quiet hours; daytime debates use the default model and are repeated with this one at night."));
  box.append(t, row, msg);
  return panel("Agent tuning and the council's model", box);
}


const STATUS_TEXT = {planning: "planning", working: "working", awaiting_ok: "waiting for your OK", done: "done", stopped: "stopped", interrupted: "interrupted", failed: "failed"};
const STATUS_CLS = {done: "low", working: "medium", planning: "medium", awaiting_ok: "high", stopped: "info", interrupted: "high", failed: "critical"};
const TEMPLATES = [
  ["Why is a disk filling?", "Why is the backups storage filling so fast, and what should I do about it?"],
  ["Is this machine behaving oddly?", "What is 10.20.50.20 doing on the network, and is it normal for it?"],
  ["What needs patching?", "What needs patching on the Proxmox host, and is it safe to reboot now?"],
  ["Plan the firewall logging", "Plan how to get OPNsense and Suricata logs into Loki."],
  ["Check last night's backup", "Did last night's backup finish cleanly, and which guests take the most space?"],
];

async function people() {
  const r = await api("/api/roster"), m = {};
  r.forEach((a) => { const c = crewSync()[a.name]; m[a.name] = c ? {...a, title: `${c.callsign} (${a.title.replace(/^The /, "")})`} : a; });
  m.owner = {title: "You", look: {shirt: "#f6a04a"}};
  m.claude = {title: "Sage (Claude, outside advisor)", look: crewSync().claude?.look || {shirt: "#d97757"}};
  m.openai = {title: "Atlas (OpenAI, outside advisor)", look: crewSync().openai?.look || {shirt: "#10a37f"}};
  m.gemini = {title: "Nova (Gemini, outside advisor)", look: crewSync().gemini?.look || {shirt: "#4285f4"}};
  return m;
}

const avatar = (who) => { const s = el("span", "av"); s.style.background = (who && who.look && who.look.shirt) || "#777"; return s; };

function planBlock(tasks, who, onSkip) {
  const t = el("table", "tbl"), hd = el("tr"); ["#", "Who", "Does", "Why", "Asked by", "State", ""].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  tasks.forEach((k) => {
    const tr = el("tr"), st = el("td", k.status === "failed" ? "bad" : k.status === "done" ? "ok" : k.status === "waiting_ok" ? "bad" : "dim", ({waiting_ok: "needs your OK", pending: "queued", running: "working…"})[k.status] || k.status), act = el("td");
    if (k.status === "waiting_ok" || k.status === "pending") act.append(btn("Skip", async () => onSkip(k.n), "mini"));
    tr.append(el("td", null, String(k.n)), el("td", null, (who[k.agent] || {title: k.agent}).title), el("td", null, k.skill + (Object.keys(k.args || {}).length ? " " + Object.values(k.args).map((v) => JSON.stringify(v)).join(" ") : "")), el("td", "dim", k.why), el("td", "dim", k.requested_by === "commander" ? "Commander" + (k.source === "model" ? " (model)" : "") : (who[k.requested_by] || {title: k.requested_by}).title), st, act);
    t.append(tr);
  });
  return t;
}

function messageEl(m, who) {
  const a = who[m.author] || {title: m.author, look: {shirt: "#777"}};
  const d = el("div", "tmsg " + m.kind), head = el("div", "tmhead");
  head.append(avatar(a), el("b", null, a.title), el("span", "dim", " · " + ({goal: "goal", plan: "plan", ack: "", result: "findings", request: "asks a colleague", take: "reading", summary: "summary", critique: "second opinion", review: "outside review", note: "note", owner: ""})[m.kind] + " · " + new Date(m.ts * 1000).toLocaleTimeString()));
  d.append(head, el("div", "tmtext", m.text));
  if (m.kind === "result" && m.data && m.data.facts && m.data.facts.length) { const det = el("details"); det.open = m.data.facts.length <= 4; det.append(el("summary", null, `${m.data.facts.length} fact(s)` + (m.data.followups && m.data.followups.length ? " · asks " + m.data.followups.join(", ") : "")));
    const ul = el("ul"); m.data.facts.forEach((f) => ul.append(el("li", null, f))); det.append(ul); d.append(det); }
  if (m.kind === "plan" && m.data && m.data.tasks) { const ul = el("ul"); m.data.tasks.forEach((t) => ul.append(el("li", null, `${(who[t.agent] || {title: t.agent}).title}: ${t.skill} - ${t.why}${t.needs_ok ? " (needs your OK)" : ""}`))); d.append(ul); }
  return d;
}

export async function team(view, ctx, id) {
  if (id) return teamProject(view, ctx, id);
  const [list, skills, who] = await Promise.all([api("/api/projects"), api("/api/team/skills"), people()]);
  { const hw = el("details", "howto"); hw.append(el("summary", null, "How projects work"), el("p", "dim", "Give the team a goal and the agents plan and work on it together: the Commander splits it into tasks, each agent reports its facts in a shared thread, agents ask each other for help, the Critic reviews the proposed steps, and you can ask follow-up questions. The team only reads and proposes: nothing here changes your lab.")); view.append(hw); }
  const goal = el("textarea"); goal.rows = 3; goal.placeholder = "What should the team look into? e.g. Why is the backups storage filling so fast?"; goal.style.width = "100%";
  const chips = el("div", "row"); TEMPLATES.forEach(([l, t]) => chips.append(btn(l, async () => { goal.value = t; }, "ghost")));
  const ap = el("input"); ap.type = "checkbox"; const apl = el("label", "inline"); apl.append(ap, " let me approve the plan before anything runs");
  const det = el("details"); det.append(el("summary", null, "Choose who is on the team (everyone who can help by default)"));
  const boxes = []; const grid = el("div", "row");
  skills.filter((a) => !["commander", "critic", "correlator", "watchdog", "demo"].includes(a.agent)).forEach((a) => { const i = el("input"); i.type = "checkbox"; i.checked = true; i.value = a.agent; const l = el("label", "inline"); l.append(i, " " + a.title + " (" + a.skills.map((s) => s.name).join(", ") + ")"); boxes.push(i); grid.append(l); });
  det.append(grid);
  const msg = el("p", "dim");
  const go = btn("Start the project", async () => { try { const picked = boxes.filter((b) => b.checked).map((b) => b.value); const r = await send("/api/projects", {goal: goal.value, approve_plan: ap.checked, agents: picked.length === boxes.length ? [] : picked}); location.hash = "#/team/" + r.id; } catch (e) { msg.textContent = e.message; msg.className = "bad"; } });
  view.append(panel("Start a project", goal, chips, det, el("div", "row"), msg));
  const bar = view.querySelector(".panel:last-child .row:last-of-type"); bar.append(apl, go);
  const t = el("table", "tbl"), hd = el("tr"); ["Project", "State", "From", "Updated", "Summary"].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
  list.forEach((p) => { const tr = el("tr"), a = el("a", null, p.title); a.href = "#/team/" + p.id; const c = el("td"); c.append(a); const s = el("td"); s.append(el("span", "sev " + (STATUS_CLS[p.status] || "info"), STATUS_TEXT[p.status] || p.status)); tr.append(c, s, el("td", "dim", p.origin === "auto" ? "the Commander" : "you"), el("td", "dim", ago(p.updated)), el("td", "dim", p.summary)); t.append(tr); });
  view.append(panel(`Projects (${list.length})`, ...(list.length ? [t] : [el("p", "dim", "No projects yet. Pick an example above or write your own goal.")])));
}

async function teamProject(view, ctx, id) {
  const who = await people();
  const top = el("div"), planBox = el("div"), thread = el("div", "thread"), result = el("div"), ask = el("div", "row");
  view.append(top, panel("Plan and progress", planBox), panel("The team's thread", thread, ask), result);
  const input = el("input"); input.placeholder = "Ask the team something, or tell it what to look at next…"; input.style.flex = "1";
  const sendAsk = async () => { if (!input.value.trim()) return; const v = input.value; input.value = ""; try { await send(`/api/projects/${id}/message`, {text: v}); } catch (e) { alert(e.message); } };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") sendAsk(); });
  ask.append(input, btn("Send", sendAsk));
  let last = 0, timer = null;
  const render = async (full) => {
    const d = await api(`/api/projects/${id}?after=${full ? 0 : last}`), p = d.project;
    const h = el("div"); const status = el("span", "sev " + (STATUS_CLS[p.status] || "info"), STATUS_TEXT[p.status] || p.status);
    const bk = el("a", "ghost-link", "← all projects"); bk.href = "#/team";
    const row = el("div", "row");
    if (p.status === "awaiting_ok") row.append(btn("Approve the waiting steps", async () => { await send(`/api/projects/${id}/approve`, {}); }));
    if (["working", "planning", "awaiting_ok"].includes(p.status)) row.append(btn("Stop", async () => { if (confirm("Stop this project?")) await send(`/api/projects/${id}/stop`, {}); }, "ghost"));
    h.append(bk, el("h2", null, p.title), el("p", "dim", `${p.origin === "auto" ? "Opened by the Commander" : "Started by you"} · ${ago(p.created)} · `), el("p", null, p.goal), status, row);
    top.replaceChildren(panel(null, h));
    planBox.replaceChildren(d.tasks.length ? planBlock(d.tasks, who, async (n) => { await send(`/api/projects/${id}/tasks/${n}/skip`, {}); }) : el("p", "dim", "The Commander is working out a plan…"));
    if (full) thread.replaceChildren();
    d.messages.forEach((m) => { thread.append(messageEl(m, who)); last = Math.max(last, m.id); });
    const r = p.result;
    if (r) {
      const b = el("div");
      if (r.disputed && r.dispute) { const a = crewSync()[r.dispute.advisor] || {callsign: r.dispute.advisor}; const v = el("div", "verdict stop"); v.append(el("b", null, `${a.callsign} disputes the crew's summary. Read this first: `), r.dispute.view || r.dispute.concerns.join(" ")); b.append(v); }
      b.append(el("p", "headline", (r.disputed ? "The crew's summary (disputed): " : "") + r.summary), el("p", "dim", `${r.source === "claude" ? "Written by an outside advisor from the team's key facts (numbers checked against them)" : r.source === "model" ? "Written by the Commander's local model from the facts above (numbers checked)" : "Written by rules from the agents' findings"} · version ${r.version}`));
      if (r.outside) b.append(el("p", r.outside.verdict === "agree" ? "ok" : "bad", `Outside review (${r.outside.backend === "cli" ? "your subscription" : "API"}, ${r.outside.model}): ${r.outside.verdict}${r.outside.concerns.length ? " - " + r.outside.concerns.join(" ") : ""}`));
      if ((r.key_findings || []).length) { const kf = el("div", "ev"); kf.append(el("b", null, "What the facts say")); r.key_findings.forEach((x) => kf.append(el("div", null, "• " + x))); b.append(kf); }
      if (r.critic) { const c = criticBlock({...r.critic, concerns: r.critic.concerns || [], blockers: r.critic.blockers || [], harmless_explanations: r.critic.harmless_explanations || [], verify_first: r.critic.verify_first || []}); b.append(c); }
      (r.recommendations || []).forEach((x, i) => {
        const c = el("div", "fnd"), t = el("div", "t"); t.append(sevPill(x.risk === "high" ? "high" : x.risk === "low" ? "low" : "medium"), x.title, el("span", "grp", "proposed by " + (who[x.agent] || {title: x.agent}).title));
        c.append(t); const s = el("div", "ev"); s.append(el("b", null, "Steps")); x.steps.forEach((z, k) => s.append(el("div", null, `${k + 1}. ${z}`))); c.append(s);
        const rb = el("div", "ev rb"); rb.append(el("b", null, "Rollback")); x.rollback.forEach((z, k) => rb.append(el("div", null, `${k + 1}. ${z}`))); c.append(rb);
        if (x.execute) c.append(el("div", "meta", "The team can carry this out itself after your approval on the Approvals page (the exact change is shown there first)."));
        const rw = el("div", "row"); if (x.proposed) { const a = el("a", "ghost-link", "On the Approvals page: open the finding"); a.href = "#/finding/" + x.proposed; rw.append(a); } else rw.append(btn("Send to the Approvals page", async () => { await send(`/api/projects/${id}/recommendations/${i}/propose`, {}); }));
        c.append(rw); b.append(c);
      });
      if (!(r.recommendations || []).length) b.append(el("p", "dim", "The team proposes no manual steps for this."));
      result.replaceChildren(panel("Result", b));
    } else result.replaceChildren();
  };
  await render(true);
  const refresh = () => { clearTimeout(timer); timer = setTimeout(() => render(true).catch(() => {}), 500); };
  const off = ctx.onEvent((ev) => { if (ev.project === id && /^project\./.test(ev.type)) refresh(); });
  return () => { off(); clearTimeout(timer); };
}


const ACTION_STATUS = {approved: "approved", running: "running…", done: "done and verified", failed: "FAILED", verify_failed: "check failed", rolling_back: "rolling back…", rolled_back: "rolled back", rollback_failed: "ROLLBACK FAILED", blocked: "blocked"};
const TERMINAL = ["done", "failed", "verify_failed", "rolled_back", "rollback_failed", "blocked"];

function frontierPanel(fr) {
  const p = fr.policy, box = el("div"), tiles = el("div", "tiles");
  tiles.append(tile("Outside model", !p.enabled ? "off" : fr.blocked_because ? "paused: " + fr.blocked_because : "ready", p.enabled && !fr.blocked_because),
    tile("Way in", fr.backend === "api" ? "API key (pay per token)" : fr.backend === "cli" ? "your Claude subscription (CLI)" : "none set up", !!fr.backend),
    tile("This month", fr.backend === "cli" ? `${fr.month.cli_calls} call(s), no per-token bill` : `$${fr.month.api_cost_usd} of $${fr.month.budget_usd} (${fr.month.api_calls} calls)`, true),
    tile("Calls today", `${fr.today_calls} of ${fr.daily_cap}`, fr.today_calls < fr.daily_cap), tile("Privacy", fr.privacy === "masked" ? "masked facts only" : "anonymised"));
  const cb = (v, label) => { const i = el("input"); i.type = "checkbox"; i.checked = v; const l = el("label", "inline"); l.append(i, " " + label); return [i, l]; };
  const sel = (opts, val) => { const s = el("select"); opts.forEach(([v, l]) => { const o = el("option", null, l); o.value = v; s.append(o); }); s.value = val; return s; };
  const num = (v, w) => { const i = el("input"); i.type = "number"; i.step = "any"; i.value = v; i.style.width = (w || 90) + "px"; return i; };
  const [en, enL] = cb(p.enabled, "let the team ask Claude for help");
  const be = sel([["auto", "automatic (API key if set, else subscription)"], ["api", "API key"], ["cli", "my subscription (CLI)"]], p.backend), model = el("input"), cli = sel([["sonnet", "Sonnet"], ["opus", "Opus"], ["haiku", "Haiku"]], p.cli_model), eff = sel([["low", "low (cheapest)"], ["medium", "medium"], ["high", "high"]], p.effort), pr = sel([["masked", "masked facts (outside addresses, MACs, secrets removed)"], ["anonymised", "anonymised (internal names and addresses become labels too)"]], p.privacy);
  model.value = p.model; model.style.width = "170px";
  const bud = num(p.monthly_budget_usd), cap = num(p.daily_call_cap, 70);
  const uses = {}; const urow = el("div", "row"); [["summary", "write and check project summaries"], ["plan", "help plan projects"], ["review", "review proposed steps"], ["verify", "check the result of an action"]].forEach(([k, l]) => { const [i, lab] = cb(p.uses[k], l); uses[k] = i; urow.append(lab); });
  const msg = el("p", "dim");
  const r1 = el("div", "row"); r1.append(enL, el("span", "dim", "way in"), be, el("span", "dim", "API model"), model, el("span", "dim", "CLI model"), cli);
  const r2 = el("div", "row"); r2.append(el("span", "dim", "thinking effort"), eff, el("span", "dim", "privacy"), pr);
  const r3 = el("div", "row"); r3.append(el("span", "dim", "API monthly budget ($)"), bud, el("span", "dim", "most calls per day"), cap);
  const r4 = el("div", "row"); r4.append(btn("Save", async () => { try { await patchConfig({frontier: {enabled: en.checked, backend: be.value, model: model.value.trim(), cli_model: cli.value, effort: eff.value, privacy: pr.value, monthly_budget_usd: Number(bud.value), daily_call_cap: Number(cap.value), uses: Object.fromEntries(Object.entries(uses).map(([k, i]) => [k, i.checked]))}}); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } }),
    btn("Test it", async () => { try { const r = await send("/api/frontier/test", {}); msg.textContent = `It answered through the ${r.backend === "cli" ? "subscription CLI" : "API"} (${r.model}).`; msg.className = "ok"; } catch (e) { msg.textContent = "Not working: " + e.message; msg.className = "bad"; } }, "ghost"));
  box.append(tiles, el("p", "dim", "Claude plans, reviews and checks the local team's work. It only ever returns text: it gets no credential, no shell and cannot act. What it receives is a short fact digest with outside addresses, MAC addresses and anything that looks like a secret removed; everything sent and received is listed below for 14 days. If it fails, or the budget or the daily cap is reached, the team carries on with its local models."),
    el("p", "dim", "Two ways in. (1) API key: put ANTHROPIC_API_KEY=... in /etc/prxy-command/env in the container and restart; it is billed per token on a separate Console account (a Claude subscription does not include API credit), capped by the budget above. (2) Your subscription: run tools/setup-claude-cli.sh on the host once, then sign in once inside the container (the steps are in that script); no per-token bill, but calls count against your plan's usage limits, so the daily cap matters. If both exist, 'automatic' uses the API key."), r1, r2, r3, urow, r4, msg);
  if (fr.last_error) box.append(el("p", "bad", "Last problem: " + fr.last_error));
  if (fr.log.length) { const d = el("details"); d.append(el("summary", null, `What was sent and received (${fr.log.length} most recent calls)`)); fr.log.forEach((x) => { const e = el("div", "ev"); e.append(el("b", null, `${clock(x.ts)} · ${x.purpose} · ${x.backend} ${x.model || ""} · ${x.tin}+${x.tout} tokens${x.cost ? " · ~$" + x.cost.toFixed(4) : ""} · ${x.ok ? "ok" : "FAILED: " + x.error}`)); const s = el("details"); s.append(el("summary", null, "sent / received"), el("pre", null, "SENT:\n" + x.sent + "\n\nRECEIVED:\n" + x.reply)); e.append(s); d.append(e); }); box.append(d); }
  return panel("Outside model (Claude): planning, review and checking", box);
}

function actionsPanel(st, list) {
  const box = el("div"), tiles = el("div", "tiles");
  tiles.append(tile("Executor", st.enabled ? "on" : "off", st.enabled), tile("Action token", st.token_configured ? "created" : "not created yet", st.token_configured), tile("Allowed", st.allowed.length ? st.allowed.join(", ") : "nothing"), tile("Today", `${st.today} of ${st.max_per_day}`, st.today < st.max_per_day));
  const cb = (v, label) => { const i = el("input"); i.type = "checkbox"; i.checked = v; const l = el("label", "inline"); l.append(i, " " + label); return [i, l]; };
  const [en, enL] = cb(st.enabled, "let the team carry out approved actions"), [ar, arL] = cb(st.auto_rollback, "undo an action automatically when the check afterwards fails");
  const picks = {}; const grid = el("div");
  st.catalogue.forEach((a) => { const [i, l] = cb(st.allowed.includes(a.name), `${a.title} (risk ${a.risk}; ${a.reversible})`); picks[a.name] = i; const r = el("div"); r.append(l, el("div", "dim", "needs: " + a.privileges.join("; "))); grid.append(r); });
  const mx = el("input"); mx.type = "number"; mx.min = 0; mx.max = 20; mx.value = st.max_per_day; mx.style.width = "70px";
  const msg = el("p", "dim"), row = el("div", "row"); row.append(enL, arL, el("span", "dim", "most per day"), mx);
  const save = btn("Save", async () => { try { await patchConfig({actions: {enabled: en.checked, auto_rollback: ar.checked, max_per_day: Number(mx.value), allowed: Object.entries(picks).filter(([, i]) => i.checked).map(([k]) => k)}}); msg.textContent = "Saved."; msg.className = "ok"; } catch (e) { msg.textContent = "Not saved: " + e.message; msg.className = "bad"; } });
  box.append(tiles, el("p", "dim", "Off by default. When on, an approved proposal that carries one of the named actions below gets a 'run it for me' button on the Approvals page, after showing the exact change and the checks. There is no shell and no free-form command: only these fixed, reversible actions, one at a time, each verified afterwards (and rolled back if the check fails). It uses its own Proxmox token with only the privileges below, which you create yourself on the host with tools/create-action-token.sh --vmids 110,131 (add --allow-backup-job only if you accept Sys.Modify). The read-only token the agents use cannot write."), row, grid, save, msg);
  if (list.length) { const t = el("table", "tbl"), hd = el("tr"); ["When", "What", "State", "Note", ""].forEach((x) => hd.append(el("th", null, x))); t.append(hd);
    list.slice(0, 12).forEach((a) => { const tr = el("tr"), last = el("td"); if (a.status === "done") last.append(btn("Roll back", async () => { if (confirm("Undo this change?")) { await send(`/api/actions/${a.id}/rollback`, {ack: true}); location.reload(); } }, "mini")); tr.append(el("td", null, clock(a.created)), el("td", null, `${a.action} ${JSON.stringify(a.params)}`), el("td", ["failed", "rollback_failed", "verify_failed"].includes(a.status) ? "bad" : "", ACTION_STATUS[a.status] || a.status), el("td", "dim", a.note || ""), last); t.append(tr); });
    box.append(el("p", "dim", "What the team has done:"), t); }
  return panel("Actions the team may carry out (approval first, verified, reversible)", box);
}

async function executeBlock(f, onDone) {
  const wrap = el("div", "ev incident"); wrap.append(el("b", null, "The team can do this for you"));
  let pv; try { pv = await api(`/api/findings/${f.id}/execute-preview`); } catch (e) { wrap.append(el("div", "bad", "Could not prepare the preview: " + e.message)); return wrap; }
  wrap.append(el("div", "dim", `${pv.what} · risk ${pv.risk} · ${pv.reversible}`));
  pv.changes.forEach((c) => wrap.append(el("div", null, "• " + c)));
  if (pv.calls.length) { const pre = el("pre"); pre.textContent = pv.calls.map((c) => `${c.method} ${c.path} ${JSON.stringify(c.body)}`).join("\n"); const d = el("details"); d.append(el("summary", null, "the exact Proxmox calls"), pre); wrap.append(d); }
  pv.prechecks.forEach((c) => wrap.append(el("div", c.ok ? "ok" : "bad", `${c.ok ? "✓" : "✗"} ${c.name}${c.detail ? " (" + c.detail + ")" : ""}`)));
  wrap.append(el("div", "dim", "Needs: " + pv.privileges.join("; ")));
  const ack = el("input"); ack.type = "checkbox"; const lab = el("label", "inline"); lab.append(ack, " I read the steps and the rollback plan"); const note = el("input"); note.placeholder = "note (optional)";
  const out = el("p", "dim"), row = el("div", "row");
  row.append(lab, note, btn(pv.ok ? "Approve and run it for me" : "Try anyway (it will be blocked)", async () => {
    if (!ack.checked) { alert("Tick the box to confirm you read the rollback plan."); return; }
    try {
      const r = await fetch(`/api/findings/${f.id}/execute`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ack: true, note: note.value})}), d = await r.json();
      if (!r.ok && r.status !== 409) throw new Error(d.error || r.status);
      if (r.status === 409) { out.textContent = "Blocked: " + (d.action.note || ""); out.className = "bad"; return; }
      out.textContent = "Running…"; out.className = "dim";
      for (let i = 0; i < 60; i++) { await new Promise((x) => setTimeout(x, 1500)); const a = await api("/api/actions/" + d.id); if (TERMINAL.includes(a.status)) { out.textContent = `${ACTION_STATUS[a.status]}: ${a.note || ""}`; out.className = a.status === "done" ? "ok" : "bad"; setTimeout(onDone, 2500); return; } }
    } catch (e) { out.textContent = e.message; out.className = "bad"; }
  }));
  wrap.append(row, out);
  return wrap;
}
