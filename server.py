"""Prxy-AI_Command: background agents, council and Office page. S0 skeleton: login, TLS, health, roadmap.
Plan and session roadmap: /root/PRXY_AI_COMMAND_HANDOFF.md (on the Proxmox host)."""
import asyncio, base64, hmac, json, os, re, ssl, time
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
from aiohttp import web

from command.config import VersionedConfig
from command.events import EventBus
from command.gateway import Gateway, GatewayError, INTERACTIVE
from command.runner import Runner
from command.notify import Notifier
from command.team import Team
from command.chat import Crew, BREAK
from command import crew as crew_mod
from command.frontier import Frontier
from command.advisors import Advisors, PROVIDERS
from command.research_desk import ResearchDesk
from command.missions import Missions
from command.nextsteps import NextSteps
from command.autopilot import Autopilot, RULE_KEYS
from command.actions import Actions, ActionError
from command import maintenance as maint, trace as _trace
from command.agents.watchdog import health_snapshot
from command.config import PARAM_SPECS
from command.roster import roster, ROSTER
from command.store import Store, SEVERITIES, STATUSES
from command import agents as agent_modules

ROOT = Path(__file__).resolve().parent
VERSION = "1.6.0"
USER = os.getenv("CMD_USER", "prxy")
PASSWORD = os.getenv("CMD_PASSWORD", "")
HOST = os.getenv("CMD_HOST", "127.0.0.1")
PORT = int(os.getenv("CMD_PORT", "8320"))
DATA = Path(os.getenv("CMD_DATA", ROOT / "data"))
OLLAMA = os.getenv("OLLAMA_URL", "http://10.20.40.131:11434")
STARTED = time.time()

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self' wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


FEED_ALLOW = set(filter(None, os.getenv("FEED_ALLOW", "10.20.40.137,10.20.50.117,10.20.40.224").split(",")))


@web.middleware
async def guard(request, handler):
    if request.path in ("/feed/lab.json", "/feed/media.json", "/feed/health.json") and request.method == "GET" and request.remote in FEED_ALLOW:
        resp = await handler(request)              # counts-only status for the TV feed: no login, but only these source addresses
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if not PASSWORD:                      # refuse to serve without a password configured
        return web.Response(status=503, text="CMD_PASSWORD is not set")
    given = request.headers.get("Authorization", "")
    want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    if not hmac.compare_digest(given.encode(), want.encode()):
        await asyncio.sleep(1)            # slow down guessing
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Prxy-AI_Command"'}, text="Login required")
    origin = request.headers.get("Origin")
    if origin and (request.method not in ("GET", "HEAD", "OPTIONS") or request.path == "/ws"):
        if urlparse(origin).netloc != request.host:          # cross-site request with our cached Basic credentials: refuse
            return web.json_response({"error": "cross-origin request refused"}, status=403)
    if request.method in ("POST", "PUT") and request.can_read_body and not request.content_type.startswith("application/json"):
        return web.json_response({"error": "JSON body required"}, status=415)
    if request.method == "GET" and request.path in ("/", "/ws") and "crew" in request.app:
        try:
            request.app["crew"].touch_owner()           # the boss opened the dashboard: counts as a visit (API calls from scripts do not)
        except Exception as e:
            print(f"presence: {e}", flush=True)
    resp = await handler(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Cache-Control", "no-cache")        # a stale script against new HTML breaks the page
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Frame-Options", "DENY")
    return resp



def J(data, status=200):
    return web.json_response(data, status=status)


async def body_json(request):
    try:
        d = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json")
    return d if isinstance(d, dict) else {}


async def health(request):
    a = request.app
    data_ok = False
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        probe = DATA / ".probe"; probe.write_text("ok"); probe.unlink(); data_ok = True
    except OSError:
        pass
    ollama = {"ok": False}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{OLLAMA}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                names = [m["name"] for m in (await r.json()).get("models", [])]
        ollama = {"ok": True, "models": len(names)}
    except Exception:
        pass
    snap = health_snapshot(a["runner"], a["notifier"], data_ok)
    board = a["store"].kv_get("watchdog:sources", {})
    return J({"name": "Prxy-AI_Command", "version": VERSION, "uptime_s": round(time.time() - STARTED), "data_dir_writable": data_ok,
              "ollama": ollama, "agents_registered": len(a["runner"].agents), "config_version": a["cfg"].version(),
              "audit": a["store"].verify_audit(), "watch": snap, "sources": board})


async def feed_health(request):
    """Counts only, no login, only for the addresses in FEED_ALLOW: the timer outside this container reads it to know the system is alive."""
    a = request.app
    return J({**health_snapshot(a["runner"], a["notifier"], True), "version": VERSION})


async def roadmap(request):
    return J(json.loads((ROOT / "roadmap.json").read_text(encoding="utf-8")))


async def agents_list(request):
    return J(request.app["runner"].describe())


async def roster_get(request):
    return J(roster(request.app["runner"]))


# ---- the crew: org chart, break room, stand-ups, recap
async def org_get(request):
    adv = request.app["advisors"]
    return J(crew_mod.org(roster(request.app["runner"]), [p for p in PROVIDERS if adv.why_not(p) is None]))


def _chat_view(m):
    return {**m, "callsign": crew_mod.callsign(m["author"]) if m["author"] != "owner" else "You",
            "to_callsign": crew_mod.callsign(m["to_agent"]) if m["to_agent"] and m["to_agent"] != "owner" else ("you" if m["to_agent"] == "owner" else ""),
            "squad": crew_mod.squad_of(m["author"]), "tier": crew_mod.tier_of(m["author"])}


async def chat_get(request):
    q = request.query
    try:
        after, limit = int(q.get("after", 0)), max(1, min(300, int(q.get("limit", 80))))
        before = int(q["before"]) if q.get("before") else None
    except ValueError:
        return J({"error": "after, before and limit must be numbers"}, 400)
    thread = q.get("thread", BREAK)
    if not re.fullmatch(r"[a-z]+(:[0-9-]{10})?", thread):
        return J({"error": "unknown thread"}, 400)
    return J({"thread": thread, "messages": [_chat_view(m) for m in request.app["store"].chat_list(thread, after, limit, before)]})


async def chat_threads(request):
    return J(request.app["store"].chat_threads())


async def chat_post(request):
    d = await body_json(request)
    try:
        rt = d.get("reply_to")
        mid = await request.app["crew"].owner_says(d.get("text", ""), int(rt) if isinstance(rt, int) or (isinstance(rt, str) and rt.isdigit()) else None)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True, "id": mid})


async def chat_accept(request):
    try:
        n = request.app["crew"].accept(int(request.match_info["id"]))
    except (ValueError, KeyError) as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True, "changed": n})


def _mission_id(request):
    return int(request.match_info["id"])


async def missions_list(request):
    return J(request.app["store"].mission_list(40))


async def mission_get(request):
    m = request.app["store"].mission_get(_mission_id(request))
    return J(m) if m else J({"error": "not found"}, 404)


async def mission_create(request):
    d = await body_json(request)
    try:
        mid = request.app["missions"].create(d.get("goal", ""), "owner", str(d.get("ref") or "")[:80])
    except ValueError as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True, "id": mid}, 202)


async def mission_approve(request):
    d = await body_json(request)
    try:
        h = await request.app["missions"].approve(_mission_id(request), bool(d.get("ack")), bool(d.get("before_done")))
    except ValueError as e:
        return J({"error": str(e)}, 409)
    return J({"ok": True, "hash": h})


async def mission_revise(request):
    d = await body_json(request)
    try:
        request.app["missions"].revise(_mission_id(request), d.get("feedback", ""))
    except ValueError as e:
        return J({"error": str(e)}, 409)
    return J({"ok": True})


async def mission_reject(request):
    d = await body_json(request)
    try:
        request.app["missions"].reject(_mission_id(request), d.get("why", ""))
    except ValueError as e:
        return J({"error": str(e)}, 409)
    return J({"ok": True})


async def mission_abort(request):
    return J({"ok": await request.app["missions"].abort(_mission_id(request))})


async def mission_owner_done(request):
    await body_json(request)
    try:
        st = await request.app["missions"].owner_done(_mission_id(request), int(request.match_info["i"]))
    except ValueError as e:
        return J({"error": str(e)}, 409)
    return J({"ok": True, "status": st})


async def mission_owner_close(request):
    d = await body_json(request)
    try:
        request.app["missions"].owner_close(_mission_id(request), d.get("why", ""))
    except ValueError as e:
        return J({"error": str(e)}, 409)
    return J({"ok": True})


async def next_get(request):
    nx = request.app["next"]
    cache = request.app["store"].kv_get("next:why") or {}
    return J({"items": nx.items(), "generated": time.time(), "why_by": cache.get("by"), "why_ts": cache.get("ts")})


async def todo_add(request):
    d = await body_json(request)
    try:
        t = request.app["missions"].todo_add(d.get("what", ""), d.get("why", ""), "owner")
    except ValueError as e:
        return J({"error": str(e)}, 400)
    request.app["store"].audit("owner", "todo.add", "", str(d.get("what", ""))[:150])
    return J({"ok": True, "todo": t})


async def todo_close(request):
    await body_json(request)
    try:
        t = request.app["missions"].todo_close(int(request.match_info["id"]), request.match_info["how"])
    except ValueError as e:
        return J({"error": str(e)}, 404)
    return J({"ok": True, "todo": t})


async def autopilot_get(request):
    return J(request.app["missions"].autopilot.status())


async def autopilot_put(request):
    """Change the standing permissions: {enabled?, max_per_day?, dry_run?, stick_check_h?, cleanup_after_days?, rules?: {key: bool}}."""
    from command.config import DEFAULTS
    d = await body_json(request)
    cfg = request.app["cfg"]
    raw = {k: v for k, v in cfg.raw().items() if k in DEFAULTS}
    ap = {**cfg.get()["autopilot"]}
    for k in ("enabled", "max_per_day", "dry_run", "stick_check_h", "cleanup_after_days"):
        if k in d:
            ap[k] = d[k]
    if isinstance(d.get("rules"), dict):
        ap["rules"] = {**ap["rules"], **{k: v for k, v in d["rules"].items() if k in RULE_KEYS}}
    raw["autopilot"] = ap
    try:
        doc = cfg.save(raw, "owner", "autopilot: " + json.dumps(d)[:150])
    except ValueError as e:
        return J({"error": str(e)}, 400)
    request.app["bus"].publish("config.saved", version=doc["version"])
    return J(request.app["missions"].autopilot.status())


async def runner_status(request):
    return J(await request.app["missions"].runner_health())


async def recap_get(request):
    st, c = request.app["store"], request.app["crew"]
    rc = st.kv_get("recap:current")
    last = c.owner_last()
    return J({"recap": rc if rc and not rc.get("dismissed") else None, "last_seen": last, "standup": st.kv_get("chat:standup:last")})


async def recap_dismiss(request):
    st = request.app["store"]
    rc = st.kv_get("recap:current")
    if rc:
        rc["dismissed"] = True
        st.kv_set("recap:current", rc)
    return J({"ok": True})


async def standup_run(request):
    thread = await request.app["crew"].standup(trigger="owner")
    request.app["store"].audit("owner", "chat.standup_now", thread, "")
    return J({"ok": True, "thread": thread})


async def agent_config(request):
    """Enable/disable an agent or change its interval/model/budget (validated, versioned, audited)."""
    name = request.match_info["name"]
    runner = request.app["runner"]
    if name not in runner.agents:
        return J({"error": "unknown agent"}, 404)
    d = await body_json(request)
    cfg = request.app["cfg"]
    raw = {k: v for k, v in cfg.raw().items() if k in ("retention_hours", "suppress_rejected_days", "tz", "gateway", "notify", "frontier", "actions", "team", "agents")}
    cur = dict(raw.get("agents", {}).get(name, {}))
    for k in ("enabled", "interval_s", "model", "token_budget"):
        if k in d:
            if d[k] is None: cur.pop(k, None)
            else: cur[k] = d[k]
    if "params" in d:                                   # {param: value | null}; a null puts the default back
        if not isinstance(d["params"], dict):
            return J({"error": "params must be an object"}, 400)
        ps = dict(cur.get("params", {}))
        for pk, pv in d["params"].items():
            if pv is None: ps.pop(pk, None)
            else: ps[pk] = pv
        cur["params"] = ps
    raw.setdefault("agents", {})[name] = cur
    try:
        doc = cfg.save(raw, "owner", f"agent {name}: {json.dumps(d)[:120]}")
    except ValueError as e:
        return J({"error": str(e)}, 400)
    request.app["bus"].publish("config.saved", version=doc["version"])
    return J({"ok": True, "version": doc["version"]})


async def agent_run(request):
    name = request.match_info["name"]
    try:
        started = request.app["runner"].trigger(name, "manual")
    except KeyError:
        return J({"error": "unknown agent"}, 404)
    request.app["store"].audit("owner", "agent.run_now", name, "started" if started else "already running")
    return J({"started": started})


async def maintenance_list(request):
    st = request.app["store"]
    return J({"active": [w for w in maint.active(st)], "all": st.maint_list(False)})


async def maintenance_add(request):
    d = await body_json(request)
    st = request.app["store"]
    try:
        start, end, scope, agents, pattern, note = maint.validate(d)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    unknown = [a for a in agents if a not in request.app["runner"].agents]
    if unknown:
        return J({"error": f"unknown agent(s): {unknown}"}, 400)
    mid = st.maint_add(start, end, scope, agents, pattern, note)
    st.audit("owner", "maintenance.add", f"window#{mid}", f"{scope} for {round((end - start) / 60)} min; {note[:80]}")
    request.app["bus"].publish("maintenance.changed", id=mid)
    return J({"id": mid, "start": start, "end": end})


async def maintenance_cancel(request):
    st, mid = request.app["store"], int(request.match_info["id"])
    ok = st.maint_cancel(mid)
    st.audit("owner", "maintenance.cancel", f"window#{mid}", "cancelled" if ok else "not active")
    request.app["bus"].publish("maintenance.changed", id=mid)
    return J({"ok": bool(ok)})


async def notify_status(request):
    return J(request.app["notifier"].status())


async def notify_test(request):
    r = await request.app["notifier"].send_test()
    request.app["store"].audit("owner", "notify.test", "", "ok" if r["ok"] else str(r["error"])[:100])
    return J(r, 200 if r["ok"] else 502)


async def incidents_list(request):
    st = request.app["store"]
    rows = [f for f in st.list_findings("correlator", None, None, 60)]
    members = {}
    for f in rows[:20]:
        members[f["id"]] = [m for m in (st.get(i) for i in f["links"]) if m]
    return J({"incidents": rows, "members": {k: [{"id": m["id"], "agent": m["agent"], "severity": m["severity"], "title": m["title"], "status": m["status"]} for m in v] for k, v in members.items()},
              "signals": st.signals_since(time.time() - 3600)[:60]})


def _run_trace(st, rid):
    if not rid:
        return None
    r = st.get_run(rid)
    return {**r, "queries": st.trace_for_run(rid), "llm": st.usage_for_run(rid)} if r else None


async def trace_finding(request):
    st = request.app["store"]
    fid = request.match_info["id"]
    f = st.get(fid)
    if not f:
        return J({"error": "not found"}, 404)
    cited = [dict(r) for r in st.q("SELECT id, date, ts FROM briefings WHERE doc LIKE ? ORDER BY ts DESC LIMIT 5", (f"%{fid}%",))]
    audit = [dict(r) for r in st.q("SELECT ts, actor, action, detail FROM audit WHERE target=? OR detail LIKE ? ORDER BY id DESC LIMIT 30", (fid, f"%{fid}%"))]
    members = [{"id": m["id"], "agent": m["agent"], "title": m["title"], "run_id": m["run_id"]} for m in (st.get(i) for i in f["links"]) if m]
    return J({"finding": {k: f[k] for k in ("id", "agent", "fingerprint", "title", "severity", "status", "ts", "last_seen", "changed_at", "count", "confidence")},
              "raised_by": _run_trace(st, f.get("run_id")), "latest_run": _run_trace(st, f.get("last_run_id")) if f.get("last_run_id") != f.get("run_id") else None,
              "critic": {k: f["critic"][k] for k in ("verdict", "reviewed", "by")} if f.get("critic") else None, "briefings": cited, "audit": audit, "members": members,
              "thread": f["thread"], "approvals": st.approvals_for_finding(fid), "note": "Query details are kept for the retention window (24 h); run records and model calls for 30 days."})


async def trace_run(request):
    r = _run_trace(request.app["store"], int(request.match_info["id"]))
    return J(r) if r else J({"error": "not found"}, 404)


async def tune_accept(request):
    """Apply a threshold change the Archivist suggested. The owner's click is the approval; it is a normal versioned config change."""
    st, cfg = request.app["store"], request.app["cfg"]
    f = st.get(request.match_info["id"])
    t = ((f or {}).get("thread") or {}).get("tune")
    if not f or f["agent"] != "archivist" or not t:
        return J({"error": "this finding carries no tuning suggestion"}, 404)
    if f["status"] not in ("new", "seen"):
        return J({"error": f"finding is {f['status']}"}, 400)
    sp = PARAM_SPECS.get(t["agent"], {}).get(t["param"])
    cur = cfg.get()["agents"].get(t["agent"], {}).get("params", {}).get(t["param"], sp["default"] if sp else None)
    if not sp or cur != t["from"]:
        return J({"error": "the setting has changed since this was suggested; wait for a fresh suggestion"}, 409)
    raw = {k: v for k, v in cfg.raw().items() if k in ("retention_hours", "suppress_rejected_days", "tz", "gateway", "notify", "frontier", "actions", "team", "agents")}
    ag = dict(raw.get("agents", {}).get(t["agent"], {}))
    ag["params"] = {**ag.get("params", {}), t["param"]: t["to"]}
    raw.setdefault("agents", {})[t["agent"]] = ag
    try:
        doc = cfg.save(raw, "owner", f"tune {t['agent']}.{t['param']}: {t['from']} -> {t['to']}")
    except ValueError as e:
        return J({"error": str(e)}, 400)
    st.kv_set(f"tune_at:{t['agent']}:{t['param']}", time.time())
    st.set_status(f["id"], "done")
    st.audit("owner", "tune.accept", f["id"], f"{t['agent']}.{t['param']}: {t['from']} -> {t['to']} (config v{doc['version']})")
    request.app["bus"].publish("config.saved", version=doc["version"])
    return J({"ok": True, "version": doc["version"], "rollback": "Settings > Config history"})


def _project_view(st, p, after=0):
    tasks = [{k: t[k] for k in ("n", "agent", "skill", "args", "why", "source", "requested_by", "depth", "status", "needs_ok", "started", "finished", "run_id")} | {"summary": (t["result"] or {}).get("summary", "")} for t in st.tasks(p["id"])]
    return {"project": p, "tasks": tasks, "messages": st.msgs(p["id"], after)}


async def team_skills(request):
    team = request.app["team"]; rt = request.app["runner"]
    return J([{"agent": a, "title": team.title_of(a), "group": rt.agents[a].group, "skills": [{"name": n, **sp} for n, sp in sk.items()]} for a, sk in team.catalogue().items()])


async def projects_list(request):
    return J([{k: p[k] for k in ("id", "title", "status", "origin", "created", "updated")} | {"summary": ((p["result"] or {}).get("summary") or "")[:200], "agents": (p["result"] or {}).get("agents", [])} for p in request.app["store"].project_list(60)])


async def project_get(request):
    st = request.app["store"]
    p = st.project_get(request.match_info["id"])
    if not p:
        return J({"error": "not found"}, 404)
    try:
        after = int(request.query.get("after", 0))
    except ValueError:
        after = 0
    return J(_project_view(st, p, after))


async def project_create(request):
    d = await body_json(request)
    agents = d.get("agents") or []
    if not isinstance(agents, list) or any(not isinstance(a, str) for a in agents):
        return J({"error": "agents must be a list of agent names (empty = the whole team)"}, 400)
    try:
        pid = request.app["team"].create(str(d.get("goal", "")), str(d.get("title") or "") or None, "owner", "", agents, bool(d.get("approve_plan")))
    except ValueError as e:
        return J({"error": str(e)}, 400)
    return J({"id": pid})


async def project_message(request):
    d = await body_json(request)
    try:
        await request.app["team"].ask(request.match_info["id"], str(d.get("text", "")))
    except KeyError:
        return J({"error": "not found"}, 404)
    except ValueError as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True})


async def project_approve(request):
    pid = request.match_info["id"]
    if not request.app["store"].project_get(pid):
        return J({"error": "not found"}, 404)
    return J({"approved": request.app["team"].approve(pid)})


async def project_stop(request):
    pid = request.match_info["id"]
    if not request.app["store"].project_get(pid):
        return J({"error": "not found"}, 404)
    request.app["team"].stop(pid)
    return J({"ok": True})


async def project_skip(request):
    ok = request.app["team"].skip(request.match_info["id"], int(request.match_info["n"]))
    return J({"ok": ok}, 200 if ok else 400)


async def project_propose(request):
    try:
        fid = request.app["team"].propose(request.match_info["id"], int(request.match_info["i"]))
    except KeyError:
        return J({"error": "no such recommendation"}, 404)
    return J({"finding": fid})


async def frontier_status(request):
    fr, st = request.app["frontier"], request.app["store"]
    log = [{**r, "sent": r["sent"][:1500], "reply": r["reply"][:1500]} for r in st.frontier_recent(20)]
    return J({**fr.status(), "log": log})


async def advisors_get(request):
    return J(request.app["advisors"].status())


async def advisor_test(request):
    from command.frontier import FrontierError
    name = request.match_info["name"]
    if name not in PROVIDERS:
        return J({"error": "unknown advisor"}, 404)
    try:
        r = await request.app["advisors"].test(name)
    except FrontierError as e:
        return J({"ok": False, "error": str(e)}, 502)
    request.app["store"].audit("owner", "advisor.test", name, r["model"])
    return J(r)


async def advisors_coach_now(request):
    r = await request.app["advisors"].coach()
    request.app["store"].audit("owner", "advisor.coach_now", "", "started" if r else "nothing (no advisor ready)")
    return J(r or {"ok": False, "error": "no advisor is ready for coaching: switch one on and check its key and budget"}, 200 if r else 409)


async def frontier_test(request):
    fr = request.app["frontier"]
    from command.frontier import FrontierError
    try:
        r = await fr.ask("test", "Reply with exactly the word OK.", "ping", 20)
    except FrontierError as e:
        return J({"ok": False, "error": str(e)}, 502)
    request.app["store"].audit("owner", "frontier.test", r["backend"], r["model"])
    return J({"ok": True, "backend": r["backend"], "model": r["model"], "reply": r["text"][:40]})


async def actions_status(request):
    return J(request.app["actions"].status())


async def actions_list(request):
    return J(request.app["store"].action_list(40))


async def action_get(request):
    a = request.app["store"].action_get(int(request.match_info["id"]))
    return J(a) if a else J({"error": "not found"}, 404)


def _execute_spec(request, fid):
    f = request.app["store"].get(fid)
    return f, ((f or {}).get("proposed_action") or {}).get("execute")


async def finding_execute_preview(request):
    f, ex = _execute_spec(request, request.match_info["id"])
    if not ex:
        return J({"error": "this finding carries no action the team can carry out"}, 404)
    return J(await request.app["actions"].preview(ex["action"], ex["params"]))


async def finding_execute(request):
    st, bus = request.app["store"], request.app["bus"]
    fid = request.match_info["id"]
    d = await body_json(request)
    f, ex = _execute_spec(request, fid)
    if not ex:
        return J({"error": "this finding carries no action the team can carry out"}, 404)
    if f["status"] not in ("new", "seen"):
        return J({"error": f"finding is {f['status']}"}, 400)
    if d.get("ack") is not True:
        return J({"error": "confirm that you read the steps and the rollback plan (ack: true)"}, 400)
    aid, state = await request.app["actions"].start(fid, ex["action"], ex["params"], note=str(d.get("note", "")))
    if state == "blocked":
        return J({"id": aid, "status": "blocked", "action": st.action_get(aid)}, 409)
    st.approval_create(fid, f["agent"], f["proposed_action"], "approved", "carried out by the team: " + str(d.get("note", "")))
    st.set_status(fid, "approved")
    bus.publish("finding.status", agent=f["agent"], id=fid, status="approved")
    return J({"id": aid, "status": "running"}, 202)


async def action_rollback(request):
    d = await body_json(request)
    if d.get("ack") is not True:
        return J({"error": "confirm that you want this undone (ack: true)"}, 400)
    aid = int(request.match_info["id"])
    try:
        await request.app["actions"].rollback(aid, "the owner asked for it")
    except ActionError as e:
        return J({"error": str(e)}, 400)
    request.app["store"].audit("owner", "action.rollback", f"action#{aid}", "")
    return J(request.app["store"].action_get(aid))


async def findings_list(request):
    q = request.query
    if q.get("severity") and q["severity"] not in SEVERITIES:
        return J({"error": f"severity must be one of {SEVERITIES}"}, 400)
    return J(request.app["store"].list_findings(q.get("agent"), q.get("status"), q.get("severity"), q.get("limit", 100)))


async def finding_get(request):
    f = request.app["store"].get(request.match_info["id"])
    return J(f) if f else J({"error": "not found"}, 404)


async def finding_status(request):
    d = await body_json(request)
    fid, status = request.match_info["id"], d.get("status")
    if status not in ("seen", "rejected", "done"):        # approving actions comes with S8; here the owner can only triage
        return J({"error": "status must be seen, rejected or done"}, 400)
    f = request.app["store"].set_status(fid, status)
    if not f:
        return J({"error": "not found"}, 404)
    request.app["store"].audit("owner", f"finding.{status}", fid, f["title"][:120])
    request.app["bus"].publish("finding.status", agent=f["agent"], id=fid, status=status)
    return J(f)


async def runs_list(request):
    return J(request.app["store"].list_runs(request.query.get("agent"), request.query.get("limit", 50)))


async def audit_list(request):
    return J(request.app["store"].list_audit(request.query.get("limit", 100)))


async def audit_verify(request):
    return J(request.app["store"].verify_audit())


async def events_recent(request):
    return J(request.app["bus"].recent(int(request.query.get("n", 100))))


async def gateway_status(request):
    return J(request.app["gateway"].status())


async def gateway_test(request):
    d = await body_json(request)
    prompt = str(d.get("prompt", "")).strip()[:2000]
    if not prompt:
        return J({"error": "prompt is required"}, 400)
    try:
        r = await request.app["gateway"].chat("owner-test", [{"role": "user", "content": prompt}], model=d.get("model") or None,
                                              priority=INTERACTIVE, allow_swap=True, options={"num_predict": 120})
    except GatewayError as e:
        return J({"error": str(e)}, 502)
    request.app["store"].audit("owner", "gateway.test", r["model"], f"{r['tokens']} tokens")
    return J(r)


async def briefing_latest(request):
    b = request.app["store"].get_briefing(trigger="personal" if request.query.get("kind") == "personal" else None)
    return J(b or {"doc": None})


async def feed_lab(request):
    return J(request.app["store"].kv_get("briefing_editor:tv_lab", {"state": "unknown", "line": "Lab: no status yet."}))


async def briefing_get(request):
    b = request.app["store"].get_briefing(int(request.match_info["id"]))
    return J(b) if b else J({"error": "not found"}, 404)


async def briefing_list(request):
    return J(request.app["store"].list_briefings(request.query.get("limit", 30), request.query.get("kind") == "personal"))


async def briefing_generate(request):
    who = "day_briefing" if request.query.get("kind") == "personal" else "commander"
    started = request.app["runner"].trigger(who, "manual")
    request.app["store"].audit("owner", "briefing.generate", "commander", "started" if started else "already running")
    return J({"started": started})


async def memory_search(request):
    q = request.query.get("q", "").strip()
    return J(request.app["store"].search_findings(q, request.query.get("limit", 30)) if len(q) >= 2 else [])


async def memory_timeline(request):
    q = request.query
    return J(request.app["store"].timeline(q.get("agent", ""), q.get("fingerprint", "")))


async def approvals_get(request):
    """Proposed actions waiting for a decision, and the history of decisions. Nothing here executes anything: an approval records that
    the owner will carry the steps out (with the rollback plan stored first) and tracks whether they did."""
    st = request.app["store"]
    return J({"pending": st.pending_proposals(), "history": st.list_approvals(None, 100)})


async def approve(request):
    st, bus = request.app["store"], request.app["bus"]
    fid = request.match_info["id"]
    d = await body_json(request)
    f = st.get(fid)
    if not f or not f["proposed_action"]:
        return J({"error": "no proposed action on this finding"}, 404)
    if f["status"] not in ("new", "seen"):
        return J({"error": f"finding is {f['status']}"}, 400)
    if d.get("ack") is not True:
        return J({"error": "confirm that you read the steps and the rollback plan (ack: true)"}, 400)
    if [a for a in st.approvals_for_finding(fid) if a["status"] in ("approved", "done")]:
        return J({"error": "this action was already approved"}, 409)
    action = f["proposed_action"]
    aid = st.approval_create(fid, f["agent"], action, "approved", str(d.get("note", "")))
    st.set_status(fid, "approved")
    st.audit("owner", "action.approved", f"approval#{aid}", f"{f['title'][:100]}; steps={json.dumps(action.get('steps', []))[:300]}; rollback={json.dumps(action.get('rollback', []))[:300]}")
    bus.publish("approval.approved", agent=f["agent"], id=aid)
    return J({"approval": st.approval_get(aid), "message": "Recorded. The steps are yours to carry out; press 'I did it' afterwards so the finding can close."})


async def reject(request):
    st, bus = request.app["store"], request.app["bus"]
    fid = request.match_info["id"]
    d = await body_json(request)
    f = st.get(fid)
    if not f:
        return J({"error": "not found"}, 404)
    if f["proposed_action"]:
        st.approval_create(fid, f["agent"], f["proposed_action"], "rejected", str(d.get("note", "")))
    st.set_status(fid, "rejected")
    st.audit("owner", "action.rejected", fid, f["title"][:120])
    bus.publish("finding.status", agent=f["agent"], id=fid, status="rejected")
    return J({"ok": True})


async def approval_complete(request):
    st, bus = request.app["store"], request.app["bus"]
    aid = int(request.match_info["id"])
    d = await body_json(request)
    ap = st.approval_get(aid)
    if not ap or ap["status"] != "approved":
        return J({"error": "only an approved action that is still open can be marked done"}, 400)
    st.approval_update(aid, status="done", executed_at=time.time(), result={"ok": True, "manual": True, "note": str(d.get("note", ""))[:300]})
    st.set_status(ap["finding_id"], "done")
    st.audit("owner", "action.completed", f"approval#{aid}", str(d.get("note", ""))[:200])
    bus.publish("approval.done", agent=ap["agent"], id=aid)
    return J(st.approval_get(aid))


async def approval_rolled_back(request):
    st = request.app["store"]
    aid = int(request.match_info["id"])
    d = await body_json(request)
    ap = st.approval_get(aid)
    if not ap or ap["status"] != "done":
        return J({"error": "only a completed action can be marked as rolled back"}, 400)
    st.approval_update(aid, status="rolled_back", rollback_result={"ok": True, "manual": True, "note": str(d.get("note", ""))[:300]})
    st.audit("owner", "action.rolled_back", f"approval#{aid}", str(d.get("note", ""))[:200])
    return J({"ok": True, "plan": ap["action"].get("rollback", [])})


async def research_topics_get(request):
    from command.agents.research_librarian import DEFAULT_TOPICS
    return J(request.app["store"].kv_get("research_librarian:topics", DEFAULT_TOPICS))


async def research_topics_put(request):
    d = await body_json(request)
    topics = d.get("topics")
    if not isinstance(topics, list) or len(topics) > 20:
        return J({"error": "topics must be a list of at most 20"}, 400)
    clean_t = []
    for t in topics:
        if not isinstance(t, dict) or not str(t.get("name", "")).strip() or not str(t.get("query", "")).strip():
            return J({"error": "each topic needs a name and a query"}, 400)
        tid = re.sub(r"[^a-z0-9]+", "-", str(t.get("id") or t["name"]).lower()).strip("-")[:40] or "topic"
        try:
            every = int(t.get("every_h", 168))
        except (TypeError, ValueError):
            return J({"error": "every_h must be a number"}, 400)
        if not 6 <= every <= 24 * 60:
            return J({"error": "every_h must be between 6 and 1440"}, 400)
        clean_t.append({"id": tid, "name": str(t["name"]).strip()[:60], "query": str(t["query"]).strip()[:160], "every_h": every, "last": float(t.get("last", 0) or 0)})
    request.app["store"].kv_set("research_librarian:topics", clean_t)
    request.app["store"].audit("owner", "research.topics", "", json.dumps([t["name"] for t in clean_t])[:300])
    return J(clean_t)


async def research_ask(request):
    d = await body_json(request)
    try:
        rid = request.app["desk"].ask(d.get("question", ""))
    except ValueError as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True, "id": rid}, 202)


async def research_desk_list(request):
    return J(request.app["store"].research_list("desk", request.query.get("limit", 20)))


async def research_list(request):
    return J(request.app["store"].research_list(request.query.get("topic"), request.query.get("limit", 30)))


async def backlog_list(request):
    return J(request.app["store"].tasks_list(request.query.get("status") or None, 500))


async def backlog_dismiss(request):
    ok = request.app["store"].task_dismiss(request.match_info["id"])
    request.app["store"].audit("owner", "backlog.dismiss", request.match_info["id"], "")
    return J({"ok": bool(ok)})


async def feed_media(request):
    d = request.app["store"].kv_get("media_curator:latest", {"picks": [], "watchlist": []})
    return J({"generated": d.get("generated"), "picks": [{"title": p["show"], "why": p["why"], "airdate": p["airdate"]} for p in d.get("picks", [])[:8]]})   # titles only


async def media_get(request):
    st = request.app["store"]
    return J({"watchlist": st.kv_get("media_curator:watchlist", []), "latest": st.kv_get("media_curator:latest")})


async def media_put(request):
    d = await body_json(request)
    wl = d.get("watchlist")
    if not isinstance(wl, list) or len(wl) > 30 or any(not isinstance(x, str) or len(x) > 80 for x in wl):
        return J({"error": "watchlist must be a list of at most 30 titles"}, 400)
    wl = [x.strip() for x in wl if x.strip()]
    request.app["store"].kv_set("media_curator:watchlist", wl)
    request.app["store"].audit("owner", "media.watchlist", "", f"{len(wl)} titles")
    return J({"watchlist": wl})


def _list(v, n, m):
    return [str(x).strip()[:m] for x in (v or []) if str(x).strip()][:n] if isinstance(v, list) else None


async def careers_get(request):
    st = request.app["store"]
    return J({"profile": st.kv_get("job_scout:profile"), "jobs": [j for j in st.jobs_list(None, 150) if j["status"] != "dismissed"]})


async def careers_profile_put(request):
    d = await body_json(request)
    resume = str(d.get("resume", ""))
    titles, kws, locs = _list(d.get("titles"), 10, 80), _list(d.get("keywords"), 30, 40), _list(d.get("locations"), 10, 60)
    if len(resume) > 20000 or None in (titles, kws, locs):
        return J({"error": "resume up to 20000 characters; titles, keywords and locations must be lists"}, 400)
    prof = {"resume": resume, "titles": titles, "keywords": kws, "locations": locs, "remote_ok": bool(d.get("remote_ok", True))}
    request.app["store"].kv_set("job_scout:profile", prof)
    request.app["store"].audit("owner", "careers.profile", "", f"resume {len(resume)} chars, {len(titles)} titles")
    return J(prof)


async def careers_profile_delete(request):
    st = request.app["store"]
    st.kv_set("job_scout:profile", None)
    st.jobs_wipe()
    st.audit("owner", "careers.wipe", "", "profile and all tracked jobs deleted")
    return J({"ok": True})


async def careers_status(request):
    from command.agents.job_scout import STATUSES
    d = await body_json(request)
    st, jid = request.app["store"], request.match_info["id"]
    if d.get("status") not in STATUSES:
        return J({"error": f"status must be one of {STATUSES}"}, 400)
    if not st.job_get(jid):
        return J({"error": "not found"}, 404)
    kw = {"status": d["status"], "notes": str(d.get("note", ""))[:500]}
    if d.get("followup_days"):
        kw["followup"] = time.time() + float(d["followup_days"]) * 86400
    j = st.job_update(jid, **kw)
    st.audit("owner", "careers.status", jid, f"{j['company']}: {d['status']}")
    return J(j)


async def careers_draft(request):
    st, jid = request.app["store"], request.match_info["id"]
    if not st.job_get(jid):
        return J({"error": "not found"}, 404)
    st.job_update(jid, want_draft=1)
    return J({"started": request.app["runner"].trigger("job_scout", "draft-only")})


async def office_layout_get(request):
    return J(request.app["store"].kv_get("office:layout", {}))


async def office_layout_put(request):
    d = await body_json(request)
    lay = d.get("layout")
    names = {r[0] for r in ROSTER}
    if not isinstance(lay, dict) or len(lay) > 40 or any(k not in names or not (isinstance(v, list) and len(v) == 2 and all(isinstance(i, int) and not isinstance(i, bool) for i in v) and 1 <= v[0] <= 29 and 2 <= v[1] <= 20) for k, v in lay.items()):
        return J({"error": "layout must map agent names to [x, y] tile positions inside the office"}, 400)
    spots = list(lay.values())
    for i, a in enumerate(spots):
        for b in spots[i + 1:]:
            if abs(a[0] - b[0]) < 2 and a[1] == b[1]:
                return J({"error": "two desks overlap"}, 400)
    request.app["store"].kv_set("office:layout", lay)
    request.app["store"].audit("owner", "office.layout", "", f"{len(lay)} desk(s) moved")
    return J(lay)


async def config_get(request):
    from command.config import DEFAULTS
    return J({"config": request.app["cfg"].raw(), "effective": request.app["cfg"].get(), "defaults": DEFAULTS})


async def config_put(request):
    d = await body_json(request)
    try:
        doc = request.app["cfg"].save(d.get("config", {}), "owner", d.get("note", ""))
    except ValueError as e:
        return J({"error": str(e)}, 400)
    request.app["bus"].publish("config.saved", version=doc["version"])
    return J(doc)


async def config_history(request):
    return J(request.app["cfg"].history())


async def config_rollback(request):
    d = await body_json(request)
    try:
        doc = request.app["cfg"].rollback(int(d.get("version")))
    except (ValueError, TypeError, OSError) as e:
        return J({"error": f"cannot roll back: {e}"}, 400)
    request.app["bus"].publish("config.saved", version=doc["version"])
    return J(doc)


async def ws(request):
    sock = web.WebSocketResponse(heartbeat=25)
    await sock.prepare(request)
    bus, q = request.app["bus"], request.app["bus"].subscribe()
    try:
        await sock.send_json({"type": "hello", "agents": request.app["runner"].describe(), "recent": bus.recent(60)})
        while not sock.closed:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                continue
            await sock.send_json(ev)
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
    return sock


def build_app(data_dir=None, ollama_url=None, notifier_kwargs=None, frontier_kwargs=None, advisor_kwargs=None):
    data = Path(data_dir or DATA)
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "command.db")
    cfg = VersionedConfig(data / "config.json", store)
    bus = EventBus()
    gateway = Gateway(cfg, store, bus, ollama_url or OLLAMA)
    runner = Runner(cfg, store, bus, gateway)
    for cls in agent_modules.ALL:
        runner.register(cls())
    notifier = Notifier(cfg, store, bus, **(notifier_kwargs or {}))
    runner.notifier, runner.data_dir = notifier, data
    frontier = Frontier(cfg, store, bus, **(frontier_kwargs or {}))
    advisors = Advisors(cfg, store, bus, frontier, **(advisor_kwargs or {}))
    runner.frontier = advisors                     # every outside call goes through the advisors: job routing, panels, per-advisor budgets
    actions = Actions(runner, notifier, advisors)
    team = Team(runner, notifier, advisors)
    advisors.team = team
    desk = ResearchDesk(runner, advisors)
    crew = Crew(runner)
    crew.team, crew.desk = team, desk
    missions = Missions(runner, advisors, crew)
    crew.missions = missions
    missions.autopilot = Autopilot(missions)
    nextsteps = NextSteps(store, missions, advisors, bus)
    crew.next = nextsteps

    a = web.Application(middlewares=[guard])
    a["store"], a["cfg"], a["bus"], a["gateway"], a["runner"], a["notifier"], a["team"], a["frontier"], a["actions"] = store, cfg, bus, gateway, runner, notifier, team, frontier, actions
    a["crew"], a["advisors"], a["desk"], a["missions"], a["next"] = crew, advisors, desk, missions, nextsteps

    async def on_startup(_):
        store.audit("system", "start", VERSION, f"{len(runner.agents)} agents")
        gateway.start()
        runner.start()
        if notifier.configured():
            notifier.start()
        team.start()
        crew.start()
        advisors.start()
        missions.start()
        nextsteps.start()

    async def on_cleanup(_):
        for t in (runner.loop_task, gateway.task, *notifier.tasks, *team.tasks_running(), *actions.tasks.values(), *crew.running(), advisors.task, *desk.tasks.values(), *missions.tasks.values(), nextsteps.task):
            if t:
                t.cancel()
        store.audit("system", "stop", VERSION, "")

    a.on_startup.append(on_startup)
    a.on_cleanup.append(on_cleanup)
    a.add_routes(routes())
    return a


def routes():
    return [
    web.get("/api/health", health),
    web.get("/api/roadmap", roadmap),
    web.get("/api/agents", agents_list),
    web.get("/api/roster", roster_get),
    web.get("/api/org", org_get),
    web.get("/api/chat", chat_get),
    web.get("/api/chat/threads", chat_threads),
    web.post("/api/chat", chat_post),
    web.post("/api/chat/{id:\\d+}/accept", chat_accept),
    web.get("/api/missions", missions_list),
    web.get("/api/missions/runner", runner_status),
    web.post("/api/missions", mission_create),
    web.get("/api/missions/{id:\\d+}", mission_get),
    web.post("/api/missions/{id:\\d+}/approve", mission_approve),
    web.post("/api/missions/{id:\\d+}/revise", mission_revise),
    web.post("/api/missions/{id:\\d+}/reject", mission_reject),
    web.post("/api/missions/{id:\\d+}/abort", mission_abort),
    web.post("/api/missions/{id:\\d+}/owner/{i:\\d+}/done", mission_owner_done),
    web.post("/api/missions/{id:\\d+}/owner/close", mission_owner_close),
    web.get("/api/next", next_get),
    web.get("/api/autopilot", autopilot_get),
    web.put("/api/autopilot", autopilot_put),
    web.post("/api/next/todo", todo_add),
    web.post("/api/next/todo/{id:\\d+}/{how:done|dismiss}", todo_close),
    web.get("/api/recap", recap_get),
    web.post("/api/recap/dismiss", recap_dismiss),
    web.post("/api/standup", standup_run),
    web.get("/api/office/layout", office_layout_get),
    web.put("/api/office/layout", office_layout_put),
    web.post("/api/agents/{name}/run", agent_run),
    web.post("/api/agents/{name}/config", agent_config),
    web.get("/api/findings", findings_list),
    web.get("/api/findings/{id}", finding_get),
    web.post("/api/findings/{id}/status", finding_status),
    web.get("/api/runs", runs_list),
    web.get("/api/audit", audit_list),
    web.get("/api/audit/verify", audit_verify),
    web.get("/api/events", events_recent),
    web.get("/api/approvals", approvals_get),
    web.post("/api/findings/{id}/approve", approve),
    web.post("/api/findings/{id}/reject", reject),
    web.post("/api/approvals/{id}/complete", approval_complete),
    web.post("/api/approvals/{id}/rolled-back", approval_rolled_back),
    web.get("/api/briefing/latest", briefing_latest),
    web.get("/api/briefing/{id}", briefing_get),
    web.get("/api/briefings", briefing_list),
    web.get("/api/research/topics", research_topics_get),
    web.put("/api/research/topics", research_topics_put),
    web.get("/api/research", research_list),
    web.post("/api/research/ask", research_ask),
    web.get("/api/research/desk", research_desk_list),
    web.get("/api/careers", careers_get),
    web.put("/api/careers/profile", careers_profile_put),
    web.delete("/api/careers/profile", careers_profile_delete),
    web.post("/api/careers/jobs/{id}/status", careers_status),
    web.post("/api/careers/jobs/{id}/draft", careers_draft),
    web.get("/api/backlog", backlog_list),
    web.post("/api/backlog/{id}/dismiss", backlog_dismiss),
    web.get("/feed/lab.json", feed_lab),
    web.get("/feed/media.json", feed_media),
    web.get("/feed/health.json", feed_health),
    web.get("/api/maintenance", maintenance_list),
    web.post("/api/maintenance", maintenance_add),
    web.post("/api/maintenance/{id}/cancel", maintenance_cancel),
    web.get("/api/notify", notify_status),
    web.post("/api/notify/test", notify_test),
    web.get("/api/incidents", incidents_list),
    web.get("/api/frontier", frontier_status),
    web.get("/api/advisors", advisors_get),
    web.post("/api/advisors/{name}/test", advisor_test),
    web.post("/api/advisors/coach", advisors_coach_now),
    web.post("/api/frontier/test", frontier_test),
    web.get("/api/actions/status", actions_status),
    web.get("/api/actions", actions_list),
    web.get("/api/actions/{id}", action_get),
    web.post("/api/actions/{id}/rollback", action_rollback),
    web.get("/api/findings/{id}/execute-preview", finding_execute_preview),
    web.post("/api/findings/{id}/execute", finding_execute),
    web.get("/api/team/skills", team_skills),
    web.get("/api/projects", projects_list),
    web.post("/api/projects", project_create),
    web.get("/api/projects/{id}", project_get),
    web.post("/api/projects/{id}/message", project_message),
    web.post("/api/projects/{id}/approve", project_approve),
    web.post("/api/projects/{id}/stop", project_stop),
    web.post("/api/projects/{id}/tasks/{n}/skip", project_skip),
    web.post("/api/projects/{id}/recommendations/{i}/propose", project_propose),
    web.get("/api/trace/finding/{id}", trace_finding),
    web.get("/api/trace/run/{id}", trace_run),
    web.post("/api/tune/{id}/accept", tune_accept),
    web.get("/api/media", media_get),
    web.put("/api/media/watchlist", media_put),
    web.post("/api/briefing/generate", briefing_generate),
    web.get("/api/memory/search", memory_search),
    web.get("/api/memory/timeline", memory_timeline),
    web.get("/api/gateway", gateway_status),
    web.post("/api/gateway/test", gateway_test),
    web.get("/api/config", config_get),
    web.put("/api/config", config_put),
    web.get("/api/config/history", config_history),
    web.post("/api/config/rollback", config_rollback),
    web.get("/ws", ws),
    web.get("/favicon.ico", lambda r: web.Response(status=204)),
    web.get("/", lambda r: web.FileResponse(ROOT / "static" / "index.html")),
    web.static("/static", ROOT / "static"),
    ]


if __name__ == "__main__":
    app = build_app()
    ctx = None
    cert, key = os.getenv("CMD_TLS_CERT"), os.getenv("CMD_TLS_KEY")
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
    print(f"Prxy-AI_Command {VERSION} on {'https' if ctx else 'http'}://{HOST}:{PORT} (login on)", flush=True)
    web.run_app(app, host=HOST, port=PORT, ssl_context=ctx, print=None, shutdown_timeout=2)
