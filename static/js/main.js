import {$, el, api} from "./api.js";
import * as P from "./pages.js";
import * as C from "./crew.js";
import * as M from "./missions.js";
import {portrait} from "./office.js";

const events = [], listeners = new Set();
const ctx = {events, onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }, setActions, setSub};

// Navigation: grouped by what you came to do. [hash key, label, page, subtitle]
const NAV = [
  [null, [["", "Home", C.home, "What happened, what needs you, and the crew's chat"],
          ["office", "Office", P.office, "The crew at their desks, live"],
          ["crew", "Crew", C.crewPage, "Who reports to whom"],
          ["chat", "Break room", C.chatPage, "The crew talking, and stand-up notes"]]],
  ["Work", [["missions", "Missions", M.missionsPage, "The team plans the job, you approve once, the team carries it out"],
            ["autopilot", "Autopilot", M.autopilotPage, "What the team may do without a click, and what it did"],
            ["findings", "Findings", P.findings, "Everything the crew has noticed"],
            ["approvals", "Approvals", P.approvals, "Steps waiting for your decision"],
            ["team", "Projects", P.team, "Investigations the crew works on together"],
            ["council", "Council", P.council, "Incidents, debates and verdicts"]]],
  ["For you", [["morning", "Morning", P.morning, "Your personal morning brief"],
               ["briefing", "Lab briefing", P.briefing, "The Commander's daily report"],
               ["research", "Research", P.research, "Topics the Librarian follows"],
               ["backlog", "Backlog", P.backlog, "Tasks from your handoff notes"],
               ["media", "Media", P.media, "What to watch"],
               ["careers", "Careers", P.careers, "Jobs and applications"]]],
  ["System", [["agents", "Agents", P.agents, "Every agent, its schedule and health"],
              ["overview", "Status", P.overview, "Sources, model queue and roadmap"],
              ["audit", "Audit log", P.audit, "Every action, hash-chained"],
              ["settings", "Settings", P.settings, "Models, limits, notifications, chat"]]],
];
const ROUTES = NAV.flatMap(([, items]) => items);
const COUNTS = {findings: 0, approvals: 0, missions: 0};
let cleanup = null, token = 0;

function setActions(...nodes) { $("actions").replaceChildren(...nodes); }
function setSub(text) { $("subtitle").textContent = text || ""; }

function drawNav(key) {
  const nav = $("nav"), kids = [];
  for (const [group, items] of NAV) {
    if (group) kids.push(el("h4", null, group));
    for (const [k, label] of items) {
      const a = el("a"); a.href = "#/" + k; a.append(el("span", null, label));
      const n = COUNTS[k];
      if (n) a.append(el("span", "count" + (k === "approvals" || k === "missions" ? " hot" : ""), String(n)));
      if (k === key || (key === "agent" && k === "agents") || (key === "finding" && k === "findings")) a.setAttribute("aria-current", "page");
      kids.push(a);
    }
  }
  nav.replaceChildren(...kids);
}

async function route() {
  const my = ++token; if (cleanup) { cleanup(); cleanup = null; }
  closeMenu();
  const parts = location.hash.replace(/^#\/?/, "").split("/"), key = parts[0] || "";
  drawNav(key);
  const hit = ROUTES.find((x) => x[0] === key);
  const title = key === "agent" ? "Agent" : key === "finding" ? "Finding" : hit ? hit[1] : "Home";
  $("title").textContent = title; setSub(hit ? hit[3] : ""); setActions();
  document.title = title + " · Prxy-AI Command";
  const view = $("view"); view.replaceChildren();
  try {
    let r;
    if (key === "agent") { r = await P.agentPage(view, ctx, decodeURIComponent(parts[1] || "")); await C.personaHeader(view, decodeURIComponent(parts[1] || "")); }
    else if (key === "finding") r = await P.findingPage(view, ctx, decodeURIComponent(parts[1] || ""));
    else if (key === "briefing") r = await P.briefing(view, ctx, parts[1]);
    else if (key === "team") r = await P.team(view, ctx, parts[1] ? decodeURIComponent(parts[1]) : "");
    else if (key === "chat") r = await C.chatPage(view, ctx, parts[1] ? decodeURIComponent(parts[1]) : "");
    else r = await (hit || ROUTES[0])[2](view, ctx);
    if (my !== token) { if (typeof r === "function") r(); return; }
    cleanup = typeof r === "function" ? r : null;
  } catch (e) { view.append(el("p", "bad", "This page didn't load: " + e.message + ". Check the connection and reload.")); }
  view.focus({preventScroll: true});
}

function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { $("wsdot").classList.add("on"); $("wsdot").title = "Live: updates arrive as they happen"; };
  ws.onclose = () => { $("wsdot").classList.remove("on"); $("wsdot").title = "Reconnecting…"; setTimeout(connect, 3000); };
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "hello") { events.splice(0, events.length, ...ev.recent); return; }
    events.push(ev); if (events.length > 200) events.shift();
    listeners.forEach((fn) => fn(ev));
    if (/^finding\.(created|status)$/.test(ev.type)) counts();
  };
}

async function badge() {
  try { const h = await api("/api/health"); $("badge").textContent = "Online, version " + h.version; $("badge").className = "badge ok"; }
  catch { $("badge").textContent = "Can't reach the server"; $("badge").className = "badge bad"; }
}

async function counts() {
  try {
    const [ap, f, ms] = await Promise.all([api("/api/approvals"), api("/api/findings?status=new&limit=500"), api("/api/missions")]);
    COUNTS.approvals = (ap.pending || []).length + ms.filter((m) => m.status === "ready").length;
    COUNTS.missions = ms.filter((m) => m.status === "ready" || m.status === "running").length;
    COUNTS.findings = f.filter((x) => x.severity === "high" || x.severity === "critical").length;
    drawNav(location.hash.replace(/^#\/?/, "").split("/")[0] || "");
  } catch { /* the nav still works without counts */ }
}

function closeMenu() { $("side").classList.remove("open"); $("menu").setAttribute("aria-expanded", "false"); }
$("menu").addEventListener("click", (e) => { e.stopPropagation(); const o = $("side").classList.toggle("open"); $("menu").setAttribute("aria-expanded", String(o)); });
document.addEventListener("click", (e) => { if ($("side").classList.contains("open") && !$("side").contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
$("view").tabIndex = -1;
portrait($("brandmark"), {skin: "#e8b98a", hair: "#2a2a2a", style: "short", shirt: "#1c2b4a", acc: "tie"});

window.addEventListener("hashchange", route);
connect(); badge(); counts(); setInterval(badge, 30000); setInterval(counts, 60000); C.crewInfo().finally(route);
