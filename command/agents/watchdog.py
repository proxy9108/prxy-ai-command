"""The Watchdog: who watches the watchers. Every other agent can only report on what it sees; this one checks that they are all still seeing.
  * late        an enabled agent has not produced a run for far longer than its schedule says (the scheduler stalled, or the run hangs)
  * failing     an agent's last runs ended in error or timeout
  * silent      an agent 'succeeds' but its last runs examined nothing at all (a dead feed that raises no error)
  * LLM         the model gateway answers most calls with errors, or not at all
  * notifier    notifications are wanted but cannot be delivered, so a critical finding would go unheard
  * self        the data disk is nearly full, or the audit chain is broken
It also keeps a small source board (Prometheus, Loki, Ollama, Proxmox: up or not) for the Overview. It cannot catch its OWN scheduler dying: that is what
the separate timer outside this container is for (it reads /feed/health.json and shouts on Discord when this system stops answering)."""
import asyncio, os, shutil, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiohttp
from ..runner import Agent

CRITICAL = {"sentinel", "alert_triage", "backup_warden"}          # being blind here matters most
SKIP = {"demo", "watchdog"}


def last_slot(at, tz, now):
    """Most recent scheduled wall-clock time (HH:MM list) that is not in the future, as a timestamp."""
    n = datetime.fromtimestamp(now, ZoneInfo(tz))
    best = None
    for hm in at:
        h, m = map(int, hm.split(":"))
        t = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > n:
            t -= timedelta(days=1)
        best = t if best is None or t > best else best
    return best.timestamp() if best else None


def lateness(agent, interval, state, now, started, tz):
    """-> (is_late, seconds_overdue). Interval agents may be late by 2.5 intervals; daily agents by 2 hours after their slot."""
    if interval:
        ref = state["last_run"] or started
        over = now - ref - (2.5 * interval + 120)
        return over > 0, max(0, over)
    if agent.at:
        slot = last_slot(agent.at, tz, now)
        if slot is None or slot < started:                       # it was not even supposed to run yet since this server started
            return False, 0
        if (state["last_run"] or 0) >= slot - 60:
            return False, 0
        over = now - slot - 7200
        return over > 0, max(0, over)
    return False, 0


def late_agents(runner, now=None):
    now = now or time.time()
    tz = runner.cfg.get()["tz"]
    out = {}
    for a in runner.agents.values():
        if a.name in SKIP or not runner.enabled(a.name):
            continue
        late, over = lateness(a, runner.interval(a), runner.state[a.name], now, runner.started, tz)
        if late:
            out[a.name] = over
    return out


def health_snapshot(runner, notifier=None, data_ok=True, now=None):
    """Counts only (the unauthenticated /feed/health.json is built from this)."""
    now = now or time.time()
    late = late_agents(runner, now)
    failing = [n for n, st in runner.state.items() if st["last_status"] in ("error", "timeout") and runner.enabled(n) and n not in SKIP]
    loop_age = round(now - runner.heartbeat, 1)
    gw = runner.store.llm_health(now - 3600)
    gw_ok = not (gw["calls"] >= 3 and gw["errors"] / gw["calls"] >= 0.5)
    cached = getattr(runner, "_audit_ok", (0, True))
    if now - cached[0] > 60:                                      # walking the whole hash chain on every poll would be wasteful
        cached = (now, runner.store.verify_audit()["ok"]); runner._audit_ok = cached
    ok = loop_age < 90 and not (set(late) & CRITICAL) and data_ok and cached[1]
    return {"ok": bool(ok), "version_ok": True, "now": round(now), "loop_age_s": loop_age, "late_agents": len(late), "late_critical": len(set(late) & CRITICAL), "failing_agents": len(failing),
            "gateway_ok": gw_ok, "notifier_ok": notifier.healthy() if notifier else True, "data_ok": data_ok}


class Watchdog(Agent):
    name = "watchdog"
    group = "housekeeping"
    title = "Watchdog"
    autonomy = "R"
    interval_s = 300
    auto_resolve = True

    async def probe(self, name, url, ctx):
        t0 = time.time()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    ok = r.status < 500
        except Exception:
            ok = False
        return {"ok": ok, "ms": round((time.time() - t0) * 1000), "ts": time.time()}

    async def run(self, ctx):
        rt, store, now = ctx.runner, ctx.runner.store, time.time()
        cfg = ctx.cfg
        problems = 0
        n = 0
        # ---- late agents
        late = late_agents(rt, now)
        for a in rt.agents.values():
            if a.name in SKIP or not rt.enabled(a.name):
                continue
            n += 1
        for name, over in late.items():
            st = rt.state[name]
            sev = "high" if name in CRITICAL else "medium"
            since = f"{int((now - st['last_run']) / 60)} minutes ago" if st["last_run"] else "since this server started"
            ctx.finding(f"watchdog:late:{name}", sev, f"{rt.agents[name].title} has stopped running on schedule",
                        f"Its last run was {since}, much later than its schedule allows. The scheduler may be stuck, or its runs may be hanging. Until it runs again, what it watches is unwatched.",
                        [{"type": "metric", "ref": "runner state", "excerpt": f"{name}: last run {since}; overdue by {int(over / 60)} min"}], confidence=0.9)
            problems += 1
        # ---- failing and silent agents
        for a in rt.agents.values():
            if a.name in SKIP or not rt.enabled(a.name):
                continue
            runs = store.list_runs(a.name, 8)
            if len(runs) >= 3 and all(r["status"] in ("error", "timeout") for r in runs[:3]):
                ctx.finding(f"watchdog:failing:{a.name}", "high" if a.name in CRITICAL else "medium", f"{a.title} has failed its last {sum(1 for r in runs[:5] if r['status'] in ('error', 'timeout'))} runs",
                            f"Latest problem: {runs[0]['error'] or runs[0]['summary'] or 'no detail'}.", [{"type": "metric", "ref": "run history", "excerpt": "; ".join(f"{r['status']}: {(r['error'] or '')[:60]}" for r in runs[:3])}], confidence=0.9)
                problems += 1
            reported = [r for r in runs if r["checked"] is not None and r["status"] == "ok"]
            older = [r for r in store.list_runs(a.name, 40)[5:] if r["checked"]]
            if len(reported) >= 4 and all(r["checked"] == 0 for r in reported[:4]) and older:
                ctx.finding(f"watchdog:silent:{a.name}", "medium", f"{a.title} is running but examining nothing",
                            "Its last runs finished without errors yet looked at zero items, although earlier runs examined some. A feed has probably gone quiet without raising an error.",
                            [{"type": "metric", "ref": "run history", "excerpt": f"last 4 runs examined 0 items; earlier runs examined {older[0]['checked']}"}], confidence=0.8)
                problems += 1
        # ---- LLM gateway
        gw = store.llm_health(now - 3600)
        if gw["calls"] >= 3 and gw["errors"] / gw["calls"] >= 0.5:
            allbad = gw["errors"] == gw["calls"]
            ctx.finding("watchdog:llm", "high" if allbad else "medium", "The language model is failing most requests" if not allbad else "The language model is not answering",
                        "Most agents only use the model for wording, so findings keep coming, but the Commander's headline, the Critic's second paragraph and the council's debate fall back to plain rules until it recovers.",
                        [{"type": "metric", "ref": "llm usage, last hour", "excerpt": f"{gw['errors']} of {gw['calls']} calls failed"}], confidence=0.85)
            problems += 1
        # ---- notifier
        nt = getattr(rt, "notifier", None)
        if nt and not nt.healthy():
            st = nt._status()
            ctx.finding("watchdog:notifier", "medium", "Notifications cannot be delivered",
                        "Push notifications are switched on but the last attempts to send failed, so a critical finding would not reach you. Check the webhook address in the server environment and that Discord is reachable.",
                        [{"type": "metric", "ref": "notifier", "excerpt": f"{st['fails']} failures in a row; last error: {st['last_error']}"}], confidence=0.9)
            problems += 1
        # ---- this server itself
        dd = getattr(rt, "data_dir", None)
        if dd:
            u = shutil.disk_usage(dd)
            if u.free / u.total < 0.10:
                ctx.finding("watchdog:disk", "medium", "The data disk of this system is almost full", "The database and history live here; when it fills, findings and the audit log stop being written.",
                            [{"type": "metric", "ref": str(dd), "excerpt": f"{u.free / 2**30:.1f} GiB free of {u.total / 2**30:.1f} GiB"}], confidence=1.0)
                problems += 1
        if now - ctx.kv_get("verified", 0) > 3600:
            ctx.kv_set("verified", now)
            v = store.verify_audit()
            if not v["ok"]:
                ctx.finding("watchdog:audit", "high", "The audit log has been tampered with", f"The hash chain breaks at row {v.get('broken_at')}: a row was edited or removed.",
                            [{"type": "metric", "ref": "audit chain", "excerpt": f"broken at row {v.get('broken_at')}"}], confidence=1.0)
                problems += 1
        # ---- sources board (Loki is probed with a real query: /ready said 'ready' while every query hung on a full disk, 2026-11-06)
        urls = {"Prometheus": os.getenv("PROM_URL", "http://10.20.40.116:9090") + "/-/ready", "Loki": os.getenv("LOKI_URL", "http://10.20.40.133:3100") + "/loki/api/v1/labels", "Ollama": os.getenv("OLLAMA_URL", "http://10.20.40.131:11434") + "/api/tags"}
        res = await asyncio.gather(*(self.probe(k, u, ctx) for k, u in urls.items()))
        board = dict(zip(urls, res))
        ctx.kv_set("sources", board)
        ctx.checked(n + len(board))
        ctx.note(f"{n} agents checked, {problems} problem(s); sources: " + ", ".join(f"{k} {'up' if v['ok'] else 'DOWN'}" for k, v in board.items()))
