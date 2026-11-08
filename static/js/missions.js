// Missions: the team proposes (goal + MOP + rollback), you approve once, the team carries it out. Server text is only written with textContent.
import {el, api, send, btn, panel, ago, keyedList} from "./api.js";
import {crewSync, pp} from "./crew.js";

const STATUS = {
  planning: ["info", "Sage is planning"], ready: ["warn", "Waiting for your approval"], needs_revision: ["stop", "Needs changes before it can run"],
  plan_failed: ["stop", "Could not be planned"], approved: ["info", "Approved, starting"], running: ["info", "Running now"], done: ["ok", "Done"],
  rolled_back: ["warn", "Stopped and undone"], rollback_failed: ["stop", "Stopped, an undo failed"], failed: ["stop", "Failed"], refused: ["stop", "Refused by the runner"],
  rejected: ["", "Rejected"], lost: ["stop", "Lost track"],
  waiting_for_you: ["warn", "Waiting for your part"], checking_your_part: ["info", "Checking your part"],
};
const V = {agree: ["ok", "no objection"], caution: ["warn", "go carefully"], disagree: ["stop", "wait"]};
const code = (t) => { const c = el("pre", "code mop-code"); c.textContent = t; return c; };

// "Your part": what only the owner can do by hand, shown before approval so you know what you sign up for. -> {node, beforeBoxes}
function yourPart(m, reload, msg) {
  const os = (m.plan || {}).owner_steps || [];
  if (!os.length) return {node: null, boxes: []};
  const own = m.owner || {}, done = {}; (own.items || []).forEach((x) => { done[x.i] = x.done_at; });
  const box = el("section", "yourpart"), boxes = [];
  const h = el("h4", null, "Your part"); h.append(el("span", "hint", m.status === "ready" ? " what you do by hand: read it before you approve" : " what only you can do"));
  box.append(h);
  const ol = el("ol", "yp-list");
  os.forEach((o) => {
    const li = el("li", "yp-item" + (done[o.i] ? " yp-done" : "")), top = el("div", "yp-top");
    top.append(el("span", "yp-when", o.when === "before" ? "Before the team starts" : "After the team's steps"), el("b", null, o.what));
    li.append(top);
    if (o.why) li.append(el("p", "dim", o.why));
    if (o.check) { const d = el("details", "fold"); d.append(el("summary", null, `How the team checks it (read-only, ${o.target === "host" ? "on the host" : "in container " + o.target.split(":")[1]})`), code(o.check)); li.append(d); }
    else li.append(el("p", "dim", "The team can't check this one by itself: your tick is the record."));
    if (m.status === "ready" && o.when === "before") {
      const cb = el("input"); cb.type = "checkbox"; cb.id = `yp-${m.id}-${o.i}`; const l = el("label", "inline"); l.htmlFor = cb.id; l.append(cb, " I did this");
      li.append(l); boxes.push(cb);
    }
    if (m.status === "waiting_for_you" && o.when === "after") {
      if (done[o.i]) li.append(el("span", "ok-word", "done"));
      else li.append(btn("I did it", async () => { try { await send(`/api/missions/${m.id}/owner/${o.i}/done`, {}); reload(); } catch (e) { msg.textContent = e.message; } }, "primary"));
    }
    if (done[o.i] && m.status !== "waiting_for_you") li.append(el("span", "ok-word", "done"));
    ol.append(li);
  });
  box.append(ol);
  const last = (own.checks || []).slice(-1)[0];
  if (last) {
    const v = el("div", "verdict " + (last.ok ? "ok" : "warn"));
    v.append(el("b", null, last.ok ? "The team checked your part: it's there. " : "The team checked your part: not there yet. "));
    (last.steps || []).filter((x) => x.state !== "ok").forEach((x) => v.append(code(x.out || "(no output)")));
    box.append(v);
  }
  if (m.status === "waiting_for_you") box.append(btn("Close it without my part", async () => { const why = prompt("Why close it without your part? (optional)") ?? null; if (why === null) return; await send(`/api/missions/${m.id}/owner/close`, {why}); reload(); }, "ghost"));
  return {node: box, boxes};
}

function stepRow(s, live) {
  const li = el("li", "mop-step" + (live ? " st-" + live.state : ""));
  const head = el("div", "mop-head");
  head.append(el("b", null, `${s.n}. ${s.title}`), el("span", "mop-target", s.target === "host" ? "on the Proxmox host" : `in container ${s.target.split(":")[1]}`));
  if (live) head.append(el("span", "mop-state", {ok: "done", running: "running…", failed: "failed", check_failed: "check failed"}[live.state] || live.state));
  li.append(head);
  const g = el("div", "mop-grid");
  g.append(el("span", "k", "Run"), code(s.run), el("span", "k", "Check"), code(s.check + (s.expect && s.expect !== "exit0" ? `   (must show: ${s.expect.replace(/^(contains|absent):/, (m, a) => a === "absent" ? "NOT " : "")})` : "")),
           el("span", "k", "Undo"), s.rollback ? code(s.rollback) : el("span", "dim", "nothing to undo (it changes nothing)"));
  li.append(g);
  if (live && (live.out || live.check_out)) { const d = el("details", "fold"); d.append(el("summary", null, `Output (${live.seconds ?? "?"} s, exit ${live.rc})`), code((live.out || "") + (live.check_out ? "\n--- check ---\n" + live.check_out : ""))); li.append(d); }
  return li;
}

export function missionCard(m, reload) {
  const p = m.plan || {}, card = el("article", "prop mission");
  const [cls, word] = STATUS[m.status] || ["", m.status];
  const h = el("h3"); h.append(el("span", "ticket", m.ticket || ("PRX-" + String(m.id).padStart(4, "0"))), el("span", "sev " + ({ok: "low", warn: "medium", stop: "high"}[cls] || "info"), word), m.title);
  if ((m.attempt || 1) > 1) h.append(el("span", "meta", ` attempt ${m.attempt}`));
  if (m.auto_rule) { const b = el("a", "autobadge", "Autopilot"); b.href = "#/autopilot"; b.title = "Carried out without a click under a standing permission you set"; h.append(b); }
  card.append(h);
  const who = crewSync()[(p.planner || {}).advisor] || {callsign: "Sage"};
  const co = ((p.leads || {}).co || []).map((x) => (crewSync()[x] || {callsign: x}).callsign);
  const by = el("p", "prop-by"); if (who.look) by.append(pp(who.look, 22)); by.append(el("span", "call", who.callsign), ` leads this mission${co.length ? " with " + co.join(" and ") : ""} · ${ago(m.ts)} · version ${m.version || 1}` + (p.risk ? ` · risk ${p.risk}` : "") + (p.downtime && p.downtime !== "none" ? ` · downtime: ${p.downtime}` : "")); card.append(by);
  const tr = (m.trail || []);
  if (tr.length) {
    const d = el("details", "fold trail"); d.open = m.status === "planning";
    d.append(el("summary", null, m.status === "planning" ? "How the plan is being made (live)" : `How the plan was made (${tr.length} steps of thinking)`));
    const ol = el("ol", "trail-list");
    tr.forEach((x) => { const a = crewSync()[x.who] || {callsign: x.who === "commander" ? "Chief" : x.who}; const li = el("li", "tr-" + x.kind); if (a.look) li.append(pp(a.look, 20)); li.append(el("b", "call", a.callsign + (x.attempt > 1 ? ` (attempt ${x.attempt})` : "") + ": "), x.text); ol.append(li); });
    d.append(ol); card.append(d);
  }
  if (m.status === "planning") { card.append(el("p", "rstate", "Planning in progress: the trail above updates as each member of the team works.")); return card; }
  const goal = el("div", "mission-goal"); goal.append(el("b", null, "Goal: "), p.goal || m.goal); if (p.why) goal.append(el("p", "dim", p.why)); card.append(goal);
  const rv = m.review || {};
  if (p.revised_for && m.status !== "planning") { const d = el("details", "fold"); d.append(el("summary", null, `${who.callsign} revised the plan after review`), el("p", "dim", p.revised_for)); card.append(d); }
  if ((m.history || []).length) {
    const d = el("details", "fold"); d.append(el("summary", null, `Earlier attempts (${m.history.length})`));
    m.history.forEach((x) => { const row = el("div", "ev"); row.append(el("b", null, `Attempt ${x.attempt}: ${x.status === "done" ? "done" : "stopped"}`), el("div", "dim", `${x.error || ""}${x.failed_step ? ` (step ${x.failed_step})` : ""}; ${x.steps_ok} step(s) had passed and were undone.`)); if (x.output) row.append(code(x.output)); d.append(row); });
    card.append(d);
  }
  if ((rv.problems || []).length) { const v = el("div", "verdict stop"); v.append(el("b", null, "Must be fixed before it can run: "), rv.problems.join(" · ")); card.append(v); }
  if ((rv.panel || []).length) {
    const row = el("div", "advrow"); rv.panel.forEach((x) => { const a = crewSync()[x.advisor] || {callsign: x.advisor}; const c = el("span", "advchip " + (V[x.verdict] || [""])[0]); if (a.look) c.append(pp(a.look, 18)); c.append(`${a.callsign}: ${(V[x.verdict] || ["", x.verdict])[1]}`); row.append(c); }); card.append(row);
    const cc = rv.panel.flatMap((x) => (x.concerns || []).map((c) => `${(crewSync()[x.advisor] || {callsign: x.advisor}).callsign}: ${c}`));
    if (cc.length) { const d = el("details", "fold"); d.append(el("summary", null, `What the reviewers said (${cc.length})`)); const ul = el("ul"); cc.forEach((c) => ul.append(el("li", null, c))); d.append(ul); card.append(d); }
  }
  if ((rv.concerns || []).length) card.append(el("p", "meta", rv.concerns.join(" · ")));
  const dr = p.dry_run;
  if (dr && (dr.steps || []).length) {
    const d = el("details", "fold"); d.append(el("summary", null, `Dry run: ${dr.steps.length} read-only step(s) were run for real before planning the rest (${dr.steps.filter((x) => x.ok).length} ok)`));
    dr.steps.forEach((x) => { const row = el("div", "ev"); row.append(el("b", null, `${x.n}. ${x.title}: ${x.ok ? "ok" : "failed"}`), code(x.out || "(no output)")); d.append(row); });
    card.append(d);
  }
  const msg = el("span", "dim");
  const yp = yourPart(m, reload, msg); if (yp.node) card.append(yp.node);
  const live = {}; ((m.run || {}).steps || []).forEach((s) => { live[s.n] = s; });
  if (p.steps) {
    const mop = el("details", "fold"); mop.open = m.status === "ready" || m.status === "running";
    mop.append(el("summary", null, `The plan: ${p.steps.length} steps (restore points are made first: container snapshots and a copy of /etc)`));
    const ol = el("ol", "mop"); p.steps.forEach((s) => ol.append(stepRow(s, live[s.n]))); mop.append(ol); card.append(mop);
  }
  const run = m.run || {};
  if ((run.rollback || []).length) { const d = el("div", "verdict warn"); d.append(el("b", null, "Undone: "), run.rollback.map((r) => `step ${r.n}: ${r.state}`).join(", ")); card.append(d); }
  const res = m.result || {};
  if (res.error) card.append(el("p", "bad", res.error));
  if (res.learned && (res.learned.facts || []).length) {
    const box = el("div", "verdict ok"); box.append(el("b", null, "What we learned: "), res.learned.conclusion || "");
    const ul = el("ul"); res.learned.facts.forEach((f) => ul.append(el("li", null, f))); box.append(ul);
    if (res.learned.next_id) box.append(el("p", null, `Next step planned as PRX-${String(res.learned.next_id).padStart(4, "0")}${res.learned.next_why ? ": " + res.learned.next_why : ""}.`));
    card.append(box);
  }
  if (res.stick) { const v = el("div", "verdict " + (res.stick.ok ? "ok" : "stop")); v.append(el("b", null, res.stick.ok ? "Checked again later: still fixed. " : "Checked again later: it came back. "), res.stick.ok ? "" : "A new mission was opened for it."); card.append(v); }
  if (res.verify) { const a = crewSync()[res.verify.advisor] || {callsign: res.verify.advisor}; const d = el("div", "verdict " + (res.verify.verdict === "ok" ? "ok" : "warn")); d.append(el("b", null, `${a.callsign} checked the result: `), res.verify.note || res.verify.verdict); card.append(d); }
  const bar = el("div", "prop-actions");
  if (m.status === "ready") {
    const ack = el("input"); ack.type = "checkbox"; ack.id = "mack-" + m.id; const l = el("label", "inline"); l.htmlFor = ack.id; l.append(ack, " I've read every step and its undo");
    bar.append(l, btn("Approve: the team does it", async () => { if (!ack.checked) { alert("Tick the box first."); return; } if (yp.boxes.some((b) => !b.checked)) { alert("First do your part that comes before the team starts, and tick it."); return; } try { await send(`/api/missions/${m.id}/approve`, {ack: true, before_done: yp.boxes.length > 0}); reload(); } catch (e) { msg.textContent = e.message; } }, "primary"));
  }
  if (["rolled_back", "failed", "refused", "rollback_failed", "lost"].includes(m.status)) {
    const fb = el("input"); fb.placeholder = "Anything Sage should know for the new plan? (optional)"; fb.setAttribute("aria-label", "Notes for the replan");
    bar.append(fb, btn("Replan it", async () => { try { await send(`/api/missions/${m.id}/revise`, {feedback: fb.value.trim() || "Replan from the failure output of the last attempt."}); reload(); } catch (e) { msg.textContent = e.message; } }, "primary"));
  }
  if (["ready", "needs_revision", "plan_failed"].includes(m.status)) {
    const fb = el("input"); fb.placeholder = "What should change?"; fb.setAttribute("aria-label", "What should change");
    bar.append(fb, btn("Send back to Sage", async () => { try { await send(`/api/missions/${m.id}/revise`, {feedback: fb.value}); reload(); } catch (e) { msg.textContent = e.message; } }, "ghost"),
               btn("Reject", async () => { if (confirm("Reject this mission?")) { await send(`/api/missions/${m.id}/reject`, {why: fb.value}); reload(); } }, "ghost"));
  }
  if (m.status === "running") bar.append(btn("Stop and undo", async () => { if (confirm("Stop after the current step and undo what was done?")) { await send(`/api/missions/${m.id}/abort`, {}); reload(); } }, "ghost"));
  bar.append(msg); if (bar.children.length > 1) card.append(bar);
  return card;
}

export async function missionsPage(view, ctx) {
  const intro = el("div");
  const runner = await api("/api/missions/runner");
  if (!runner.installed) { const v = el("div", "verdict warn"); v.append(el("b", null, "The team can plan missions, but can't carry them out yet. "), "Install the runner on the Proxmox host once: ", code("/root/prxy-command/tools/install-runner.sh --targets 110,111,...")); intro.append(v); }
  else if (!runner.reachable) intro.append(el("p", "bad", "The runner is installed but not answering: check `systemctl status prxy-runner` on the host."));
  else if (runner.disabled) intro.append(el("p", "bad", "The runner's kill switch is on (/etc/prxy-runner.disabled): approved missions will not run."));
  const ask = panel(null); const h = el("h2", null, "Give the team a mission"); h.append(el("span", "hint", "they plan every step with a check and an undo; you approve; they carry it out")); ask.append(h);
  const form = el("form", "compose"), q = el("input"), go = el("button", "primary", "Plan it"); q.placeholder = "e.g. Fix logrotate in every container so logs can't fill the disk"; q.maxLength = 600; q.setAttribute("aria-label", "Mission"); go.type = "submit";
  form.append(q, go); ask.append(form);
  const list = el("div"), pastBox = el("div"), pastWrap = el("details", "howto"), pastSum = el("summary");
  pastWrap.append(pastSum, pastBox);
  const load = async () => {
    const ms = await api("/api/missions");
    const order = ["waiting_for_you", "checking_your_part", "running", "approved", "ready", "needs_revision", "planning", "plan_failed"];
    const act = ms.filter((m) => order.includes(m.status)).sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status)), past = ms.filter((m) => !order.includes(m.status));
    keyedList(list, act, (m) => m.id, (m) => m.updated, (m) => missionCard(m, load), el("p", "empty", "No missions waiting. Give the team one above, type one in the break room (fix ..., set up ...), or press Plan a mission on a finding."));
    pastSum.textContent = `Past missions (${past.length})`; pastWrap.hidden = !past.length;
    keyedList(pastBox, past, (m) => m.id, (m) => m.updated, (m) => missionCard(m, load));
  };
  form.addEventListener("submit", async (e) => { e.preventDefault(); if (q.value.trim().length < 10) return; go.disabled = true; try { await send("/api/missions", {goal: q.value.trim()}); q.value = ""; await load(); } catch (err) { alert(err.message); } go.disabled = false; });
  view.append(intro, ask, list, pastWrap);
  await load();
  const off = ctx.onEvent((ev) => { if (/^mission\./.test(ev.type)) load(); });
  const t = setInterval(load, 5000);
  return () => { off(); clearInterval(t); };
}

// Autopilot: what the team may do without a click (standing permissions), and everything it did that way.
export async function autopilotPage(view, ctx) {
  const top = el("div"), rulesBox = el("div", "ap-rules"), logBox = el("div");
  view.append(top, panel(null, el("h2", null, "What the team may do on its own"), el("p", "dim", "Each kind is checked by plain code against exact command patterns, step by step. If a single step does not fit, the mission waits for you as usual. Reviewers objecting, a risky plan, or anything you must do first also means it waits."), rulesBox),
              panel(null, el("h2", null, "Done on its own"), logBox));
  const save = async (body) => { try { render(await send("/api/autopilot", body, "PUT")); } catch (e) { alert(e.message); } };
  const render = (d) => {
    const hero = el("section", "ap-hero " + (d.enabled ? "on" : "off"));
    const state = el("div"); state.append(el("b", "ap-state", d.enabled ? "Autopilot is ON" : "Autopilot is PAUSED"),
      el("p", "dim", d.enabled ? `${d.today} of ${d.max_per_day} autopilot missions used today. Pausing stops new ones; a running mission finishes (or use the runner's kill switch).` : "Nothing runs without your click."));
    hero.append(state, btn(d.enabled ? "Pause autopilot" : "Turn autopilot on", () => save({enabled: !d.enabled}), d.enabled ? "ghost" : "primary"));
    const opts = el("div", "ap-opts");
    const tg = (label, key, val, hint) => { const l = el("label", "ap-opt"); const c = el("input"); c.type = "checkbox"; c.checked = val; c.onchange = () => save({[key]: c.checked}); l.append(c, el("span", null, label), el("span", "dim", hint)); return l; };
    const nm = (label, key, val, lo, hi, hint) => { const l = el("label", "ap-opt"); const i = el("input"); i.type = "number"; i.min = lo; i.max = hi; i.value = val; i.style.width = "70px"; i.onchange = () => save({[key]: Number(i.value)}); l.append(el("span", null, label), i, el("span", "dim", hint)); return l; };
    opts.append(tg("Dry run before asking you", "dry_run", d.dry_run, "read-only first steps run for real, Sage fixes the plan to the output"),
                nm("Check again after (hours)", "stick_check_h", d.stick_check_h, 0, 168, "did the fix hold? 0 = off"),
                nm("Delete own restore points after (days)", "cleanup_after_days", d.cleanup_after_days, 0, 90, "0 = keep forever"),
                nm("Autopilot missions per day", "max_per_day", d.max_per_day, 0, 100, ""));
    top.replaceChildren(hero, opts);
    rulesBox.replaceChildren(...d.rules.map((r) => {
      const row = el("div", "ap-rule " + (r.on ? "on" : ""));
      const sw = el("input"); sw.type = "checkbox"; sw.checked = r.on; sw.id = "apr-" + r.key; sw.setAttribute("aria-label", r.label); sw.onchange = () => save({rules: {[r.key]: sw.checked}});
      const lab = el("label"); lab.htmlFor = sw.id; lab.append(el("b", null, r.label), el("span", "ap-kind", r.changes ? "changes things" : "only reads"));
      const may = el("p"); may.append(el("span", "k", "May: "), r.may); const never = el("p", "dim"); never.append(el("span", "k", "Never: "), r.never);
      row.append(sw, lab, may, never); return row;
    }));
    if (!d.log.length) logBox.replaceChildren(el("p", "empty", "Nothing yet. When a mission fits one of the kinds above, it runs by itself and shows up here."));
    else {
      const ul = el("ul", "ap-log");
      d.log.forEach((x) => { const li = el("li"); const a = el("a", null, `${x.ticket} ${x.title}`); a.href = "#/missions";
        li.append(el("span", "ap-when", x.approved_at ? new Date(x.approved_at * 1000).toLocaleString() : ""), a, el("span", "tag", x.rule_label), el("span", "ap-st " + x.status, (STATUS[x.status] || ["", x.status])[1]));
        if (x.stick) li.append(el("span", x.stick.ok ? "ok-word" : "bad", x.stick.ok ? "still fixed" : "came back"));
        ul.append(li); });
      logBox.replaceChildren(ul);
    }
    if ((d.stick_pending || []).length) logBox.append(el("p", "meta", "Waiting for a second look: " + d.stick_pending.map((x) => `${x.ticket} (${new Date(x.due * 1000).toLocaleString()})`).join(", ")));
  };
  render(await api("/api/autopilot"));
  const off = ctx.onEvent((ev) => { if (/^mission\./.test(ev.type)) api("/api/autopilot").then(render).catch(() => {}); });
  return () => off();
}
