"""The Correlator (the council's analyst): joins what different agents say about the SAME machine inside the same hour.
One agent seeing a host fan out, another seeing its traffic jump and a third seeing DNS volume rise is a much stronger story than three separate
findings. It reads the open findings and the weak signals on the shared blackboard (observations too small to become findings), groups them by
machine (a guest id and its IP address are the same machine), and raises ONE compound finding that links its members:
  * two or more different agents, at least one reporting live behaviour  -> an incident, at least 'medium'; each extra agent raises it a level (cap: high);
  * a configuration change recorded on that machine in the last 3 hours is stated on the incident and caps it at 'medium' (never hides it);
  * the Commander then investigates (asks the other agents to look again) and the council debates it; the verdict is applied here.
Pure rules, no model: the same inputs always give the same incident. Maintenance windows hold incidents back like any other finding."""
import hashlib, time
from collections import defaultdict
from ..council import apply_verdict
from ..entities import entities_of, alias_map, vlan_of, VLANS
from ..runner import Agent, SEV_ORDER, SEV_STRENGTH, bump

ACTIVITY = {"sentinel", "iot_watcher", "alert_triage", "backup_warden"}          # report what is happening now
CONTEXT = {"exposure_auditor", "config_drift", "patch_scout", "capacity_planner"}  # report a standing condition of a machine
NETWORK_WIDE = {"dns_watcher"}                                                  # their signals describe the whole network (entity net:all)
MEMBERS = ACTIVITY | CONTEXT
CHANGE_WINDOW_S = 3 * 3600


def noisy_or(ps):
    out = 1.0
    for p in ps:
        out *= 1 - max(0.0, min(0.99, p))
    return round(1 - out, 3)


def compute(host, findings, signals, net_signals, changes, min_agents, now):
    """Pure function. Returns None when there is no incident, else a dict describing it."""
    by_agent = {}
    for f in findings:
        by_agent.setdefault(f["agent"], []).append(f)
    live = {a for a in by_agent if a in ACTIVITY}
    for s in signals:
        if s["agent"] in ACTIVITY:
            live.add(s["agent"])
    sig_agents = {s["agent"] for s in signals if s["agent"] not in by_agent}
    base = set(by_agent) | sig_agents
    if not live or len(base) < min_agents:
        return None                                  # network-wide DNS trouble is a booster for a machine two agents already agree on, never a partner on its own
    wide = {s["agent"] for s in net_signals} if live & {"sentinel", "iot_watcher"} else set()
    agents = base | wide
    strengths = [max(SEV_STRENGTH[f["severity"]] * max(0.3, f.get("confidence", 0.5)) for f in fs) for fs in by_agent.values()]
    strengths += [max(s["strength"] for s in signals if s["agent"] == a) for a in sig_agents]
    strengths += [max(s["strength"] for s in net_signals if s["agent"] == a) * 0.5 for a in wide]
    score = noisy_or(strengths)
    if not findings and score < 0.45:
        return None
    max_member = max((f["severity"] for f in findings), key=SEV_ORDER.index, default="info")
    n_live = len(live) + len(wide)
    sev = max_member
    if n_live >= 2:
        sev = bump(max(sev, "low", key=SEV_ORDER.index), 1)
    if n_live >= 3:
        sev = bump(sev, 1)
    if SEV_ORDER.index(sev) > SEV_ORDER.index("high") and max_member != "critical":
        sev = "high"
    change = [c for c in changes if c["entity"] == f"host:{host}" and now - c["ts"] <= CHANGE_WINDOW_S]
    if change and SEV_ORDER.index(sev) > SEV_ORDER.index("medium") and SEV_ORDER.index(max_member) < SEV_ORDER.index("high"):
        sev = "medium"
    sig = hashlib.sha1(("|".join(sorted(agents)) + f"|{max_member}|{bool(change)}").encode()).hexdigest()[:10]
    return {"host": host, "agents": sorted(agents), "n_agents": len(agents), "live": sorted(live | wide), "severity": sev, "max_member": max_member, "score": score,
            "change": change[0]["text"] if change else None, "sig": sig, "members": [f for fs in by_agent.values() for f in fs], "signals": signals, "net": net_signals if wide else []}


class Correlator(Agent):
    name = "correlator"
    group = "council"
    title = "The Correlator"
    autonomy = "R"
    interval_s = 300
    auto_resolve = True
    PARAMS = {
        "window_min": dict(default=60, min=10, max=360, label="how close in time (minutes) observations must be to belong together", quieter="down", step=10, rules=["incident:"]),
        "min_agents": dict(default=2, min=2, max=4, label="how many different agents must report on a machine before it becomes an incident", quieter="up", step=1, rules=["incident:"]),
    }

    async def run(self, ctx):
        rt, store, now = ctx.runner, ctx.runner.store, time.time()
        window, min_agents = ctx.param("window_min") * 60, int(ctx.param("min_agents"))
        aliases = alias_map(store)
        open_f = [f for f in store.open_findings(500) if f["agent"] in MEMBERS]
        sigs = store.signals_since(now - window)
        per = defaultdict(lambda: {"f": [], "s": []})
        for f in open_f:
            iv = rt.interval(rt.agents[f["agent"]]) if f["agent"] in rt.agents else None
            if f["agent"] in ACTIVITY and now - f["last_seen"] > max(window, 2.5 * (iv or 0)):
                continue                                         # a live-behaviour finding that was not seen again recently is history, not a clue
            for h in entities_of(f["fingerprint"], f["title"], f["summary"], aliases=aliases)["hosts"]:
                per[h]["f"].append(f)
        for s in sigs:
            if s["entity"].startswith("host:") and s["kind"] != "change":
                per[s["entity"][5:]]["s"].append(s)
        net = [s for s in sigs if s["entity"] == "net:all" and s["agent"] in NETWORK_WIDE]
        changes = [s for s in store.signals_since(now - CHANGE_WINDOW_S) if s["kind"] == "change"]
        ctx.checked(len(per))
        n = 0
        for host, d in per.items():
            inc = compute(host, d["f"], d["s"], net, changes, min_agents, now)
            if not inc:
                continue
            fp = f"incident:host:{host}"
            cur = next((x for x in store.list_findings(self.name, None, None, 300) if x["fingerprint"] == fp and x["status"] in ("new", "seen")), None)
            th = dict(cur["thread"] or {}) if cur else {}
            sev = inc["severity"]
            if th.get("debate") and th.get("debate_sig") == inc["sig"]:
                sev = apply_verdict(sev, th["debate"]["verdict"])                  # the council's verdict applies while the picture is unchanged
            vlan = VLANS.get(vlan_of(host) or "", "")
            titles = {a: (rt.agents[a].title if a in rt.agents else a) for a in inc["agents"]}
            members = "; ".join(f"{titles.get(f['agent'], f['agent'])}: {f['title'][:80]} ({f['severity']})" for f in inc["members"][:6])
            seen_only = "; ".join(f"{titles.get(s['agent'], s['agent'])} noticed: {s['text'][:80]}" for s in inc["signals"][:4])
            wide = "; ".join(f"{titles.get(s['agent'], s['agent'])}: {s['text'][:80]} (network-wide)" for s in inc["net"][:2])
            summary = (f"{inc['n_agents']} different agents report on {host}{' (' + vlan + ' network)' if vlan else ''} within the last {int(window / 60)} minutes: "
                       + "; ".join(x for x in (members, seen_only, wide) if x) + ". "
                       + (f"A configuration change was recorded on this machine recently ({inc['change']}), which may explain part of it. " if inc["change"] else "")
                       + (f"Council verdict: {th['debate']['verdict']}. " if th.get("debate") and th.get("debate_sig") == inc["sig"] else "The council has not discussed this yet. "))
            ev = [{"type": "finding", "ref": f"{f['agent']} · {f['fingerprint'][:60]}", "excerpt": f"{f['severity']}: {f['title']}"[:300]} for f in inc["members"][:8]]
            ev += [{"type": "signal", "ref": f"{s['agent']} · {s['kind']}", "excerpt": s["text"][:300]} for s in (inc["signals"] + inc["net"])[:6]]
            f, state = ctx.finding(fp, sev, f"{inc['n_agents']} agents report on {host}: " + ", ".join(titles[a] for a in inc["agents"]), summary, ev, confidence=min(0.95, 0.4 + 0.15 * inc["n_agents"]))
            if not f:
                continue
            n += 1
            th.update(entity=host, sig=inc["sig"], agents=inc["agents"], computed=inc["severity"], change=bool(inc["change"]), max_member=inc["max_member"], score=inc["score"], live=inc["live"])
            store.set_thread(f["id"], th)
            store.set_links(f["id"], [m["id"] for m in inc["members"]])
            if state == "created":
                rt.bus.publish("council.incident", agent=self.name, id=f["id"], host=host, agents=inc["agents"], severity=sev)
                store.audit("agent:correlator", "incident", f["id"], f"{host}: {', '.join(inc['agents'])}")
                if SEV_ORDER.index(sev) >= SEV_ORDER.index("medium") and "commander" in rt.agents:
                    rt.trigger("commander", "incident")
        ctx.note(f"{len(per)} machine(s) with observations, {n} incident(s)")
