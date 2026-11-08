"""Commander (the council's Coordinator): reads every open finding, groups duplicates, ranks them, writes the daily briefing, and hands out follow-ups.
Ranking and grouping are plain code so the briefing is reproducible; the model only writes a two-sentence headline from the facts, and a headline
that contains a number not present in the facts is thrown away (template headline instead)."""
import asyncio, json, re, time
from datetime import datetime
from zoneinfo import ZoneInfo
from ..council import deliberate, apply_verdict
from ..redact import scrub
from ..runner import Agent

SEV_W = {"critical": 100, "high": 60, "medium": 30, "low": 10, "info": 2}
SEV_ORDER = ["critical", "high", "medium", "low", "info"]
COUNCIL = {"commander", "archivist", "critic", "correlator", "watchdog"}
SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def topic_of(f, incident_of=None):
    """Group key: findings about the same thing (even from different agents) share a topic. Members of a Correlator incident share the incident's topic."""
    if incident_of and f["id"] in incident_of:
        return incident_of[f["id"]]
    fp, title = f["fingerprint"], f["title"].lower()
    if fp.startswith("incident:"):
        return fp
    if "local-lvm" in fp or "thin-pool" in fp or "thin pool" in title:
        return "thin-pool"
    m = re.search(r"(lxc/\d+|qemu/\d+)", fp)
    if m:
        return f"guest:{m.group(1)}"
    m = re.match(r"backup:(?:failed|skipped|shrunk|tiny|none|stale):(\d+)", fp)
    if m:
        return f"guest:lxc/{m.group(1)}"
    if fp.startswith(("backup:no-recent-run", "backup:last-run", "backup:no-offdisk")):
        return "backups"
    m = re.match(r"(?:capacity:(?:guest|storage|host)|storage|host-fs):(.+)", fp)
    if m:
        name = m.group(1).rstrip("/").split("/")[-1] or "root"          # storage/node/backups, /mnt/pve/backups and 'backups' are the same thing
        return f"storage:{name}"
    m = re.match(r"(?:target-down|restart-loop|container-missing):([^:]+)", fp)
    if m:
        return f"component:{m.group(1)}"
    m = re.match(r"(?:scan|inbound|susp-out|big-out):(\d+\.\d+\.\d+\.\d+)", fp)
    if m:
        return f"net:{m.group(1)}"
    if fp.startswith("unifi:"):
        return "unifi"
    if fp.startswith("patch:"):
        return "patching"
    m = re.match(r"drift:([^|]+)", fp)
    if m:
        return f"config:{m.group(1)}"
    for pre in ("source:", "feed:"):
        if fp.startswith(pre):
            return fp
    if fp.startswith(("host-", "temp:")):
        return "host-health"
    if fp.startswith("ups"):
        return "ups"
    return fp.split(":")[0] + ":" + (fp.split(":")[1] if ":" in fp else "")


def score(f, now):
    s = SEV_W.get(f["severity"], 1) * max(0.3, f.get("confidence", 0.5))
    if f["status"] == "seen":
        s *= 0.6                                    # acknowledged: still open, but the owner has looked
    if f.get("chronic"):
        s *= 0.5                                    # unchanged for two weeks: a standing condition, not news
    if now - f["ts"] < 86400:
        s += 8                                      # fresh
    s += min(f.get("count", 1), 24) * 0.25          # keeps coming back
    return s


def group(findings, now):
    incident_of = {m: f["fingerprint"] for f in findings if f["agent"] == "correlator" and f["status"] in ("new", "seen") for m in (f.get("links") or [])}
    groups = {}
    for f in findings:
        groups.setdefault(topic_of(f, incident_of), []).append(f)
    out = []
    for topic, fs in groups.items():
        fs.sort(key=lambda f: -score(f, now))
        top = fs[0]
        out.append({"topic": topic, "severity": min((f["severity"] for f in fs), key=SEV_ORDER.index),
                    "score": round(score(top, now) + 3 * (len(fs) - 1), 1), "title": top["title"], "summary": top["summary"], "agents": sorted({f["agent"] for f in fs}),
                    "finding_ids": [f["id"] for f in fs], "chronic": all(f.get("chronic") for f in fs),
                    "incident": next(({"verdict": ((f.get("thread") or {}).get("debate") or {}).get("verdict")} for f in fs if f["agent"] == "correlator"), None),
                    "verdict": next((v for v in ("disagree", "caution", "agree") if any((f.get("critic") or {}).get("verdict") == v for f in fs)), None), "related": [{"id": f["id"], "agent": f["agent"], "title": f["title"], "severity": f["severity"]} for f in fs[1:]],
                    "action": next(({"kind": f["proposed_action"].get("kind"), "risk": f["proposed_action"].get("risk"), "steps": f["proposed_action"].get("steps", [])[:4]} for f in fs if f["proposed_action"]), None)})
    return sorted(out, key=lambda g: -g["score"])


def template_headline(groups, n_watch):
    attention = [g for g in groups if g["severity"] in ("critical", "high", "medium")]
    if not attention:
        return f"All quiet: nothing open above low severity. {n_watch} low-priority item(s) are on the watch list."
    hi = sum(1 for g in attention if g["severity"] in ("critical", "high"))
    me = len(attention) - hi
    return f"{len(attention)} topic(s) need attention ({hi} high, {me} medium). Top item: {attention[0]['title']}."


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def build(findings, resolved, rejected, agents, now, tz):
    """Pure function: raw material -> briefing document (headline filled in later by the agent)."""
    gs = group([f for f in findings if f["status"] in ("new", "seen")], now)
    standing = [g for g in gs if g["chronic"] and g["severity"] in ("medium", "low")]          # unchanged for two weeks: listed, but not shouted about every morning
    gs = [g for g in gs if g not in standing]
    attention = [g for g in gs if g["severity"] in ("critical", "high", "medium")]
    watch = [g for g in gs if g["severity"] in ("low", "info")]
    by_sev = {}
    for f in findings:
        if f["status"] in ("new", "seen"):
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    blind = []
    for a in agents:
        if a["name"] in ("demo",):
            continue
        if not a["enabled"]:
            continue                                   # disabled on purpose
        if a["last_status"] and a["last_status"] != "ok":
            blind.append({"agent": a["name"], "issue": f"last run failed: {a['last_summary'][:120]}"})
        elif a["last_run"] and a["interval_s"] and now - a["last_run"] > 3 * a["interval_s"] + 120:
            blind.append({"agent": a["name"], "issue": f"has not run for {int((now - a['last_run']) / 3600)} h (expected every {int(a['interval_s'] / 60)} min)"})
    d = datetime.fromtimestamp(now, ZoneInfo(tz))
    return {"date": d.strftime("%Y-%m-%d"), "generated": now, "headline": template_headline(gs, len(watch)),
            "counts": {"open": sum(by_sev.values()), "by_severity": by_sev, "topics_attention": len(attention), "topics_watch": len(watch),
                       "new_24h": sum(1 for f in findings if now - f["ts"] < 86400), "resolved_24h": len(resolved), "rejected_7d": len(rejected)},
            "top": attention[:8], "standing": [{k: g[k] for k in ("topic", "severity", "title", "agents", "finding_ids")} for g in standing[:12]], "watch": [{k: g[k] for k in ("topic", "severity", "title", "agents", "finding_ids")} for g in watch[:12]],
            "since_last": {"resolved": [scrub(f["title"]) for f in resolved[:10]]},
            "blind_spots": blind, "agents": [{"name": a["name"], "state": a["state"] if a["enabled"] else "disabled", "last_run": a["last_run"], "open": sum(a["open_findings"].values())} for a in agents if a["name"] != "demo"],
            "decisions": {"rejected": [{"title": scrub(f["title"]), "when": f.get("decided_at")} for f in rejected[:8]]}}


class Commander(Agent):
    name = "commander"
    group = "council"
    title = "Commander"
    autonomy = "R"
    interval_s = 900

    async def run(self, ctx):
        rt, store, cfg = ctx.runner, ctx.runner.store, ctx.cfg
        now = time.time()
        tz = cfg["tz"]
        ctx.working("reading", "all open findings")
        # ---- follow-ups: wake agents that went quiet, and ask peers in the same group to cross-check fresh high-severity findings
        assigned = 0
        for a in rt.agents.values():
            st = rt.state[a.name]
            iv = rt.interval(a)
            if a.name in COUNCIL or a.name == "demo" or not rt.enabled(a.name) or not iv or not st["last_run"]:
                continue
            if st["state"] != "working" and now - st["last_run"] > 3 * iv + 60 and (now - (ctx.kv_get(f"nudge:{a.name}", 0))) > 3600:
                ctx.kv_set(f"nudge:{a.name}", now)
                if rt.trigger(a.name, "commander"):
                    assigned += 1
                    rt.bus.publish("council.assign", agent=self.name, to=a.name, reason="went quiet")
                    store.audit("agent:commander", "assign", a.name, "run requested: it had not run for 3x its interval")
        followed = set(ctx.kv_get("followed", []))
        for f in store.list_findings(None, "new", None, 200):
            if f["severity"] in ("high", "critical") and now - f["ts"] < 1200 and f["id"] not in followed and f["agent"] in rt.agents:
                followed.add(f["id"])
                for a in rt.agents.values():
                    if a.group == rt.agents[f["agent"]].group and a.name != f["agent"] and a.name not in COUNCIL and rt.enabled(a.name) and now - (rt.state[a.name]["last_run"] or 0) > 300:
                        if rt.trigger(a.name, "commander"):
                            assigned += 1
                            rt.bus.publish("council.assign", agent=self.name, to=a.name, reason=f"cross-check: {scrub(f['title'])[:60]}")
                            store.audit("agent:commander", "assign", a.name, f"cross-check of finding {f['id']}")
        ctx.kv_set("followed", sorted(followed)[-200:])
        # ---- incidents the Correlator built: ask the other agents to look again, then convene the council
        handled = await self.handle_incidents(ctx)
        if ctx.trigger == "incident":
            ctx.note(f"{handled} incident step(s) handled")
            return
        # ---- briefing: once a day after 07:00 local, or whenever the owner asks
        local = datetime.now(ZoneInfo(tz))
        due = ctx.trigger == "manual" or (local.hour >= 7 and not store.briefing_today(local.strftime("%Y-%m-%d")))
        if not due:
            ctx.note(f"no briefing due; {assigned} follow-up(s) assigned" + (f"; {handled} incident step(s)" if handled else ""))
            return
        ctx.working("scanning", "ranking and grouping")
        live = {a.name for a in rt.agents.values() if rt.enabled(a.name) and a.name != "demo"}
        findings = [f for f in store.list_findings(None, None, None, 500) if f["agent"] in live or f["status"] in ("rejected", "resolved")]
        resolved = [f for f in findings if f["status"] == "resolved" and f.get("decided_at") and now - f["decided_at"] < 86400]
        rejected = [f for f in findings if f["status"] == "rejected" and f.get("decided_at") and now - f["decided_at"] < 7 * 86400]
        doc = build(findings, resolved, rejected, rt.describe(), now, tz)
        ctx.working("thinking", "writing the headline")
        facts = f"{doc['counts']['topics_attention']} topics need attention; open by severity {doc['counts']['by_severity']}; " + "; ".join(f"{g['severity']}: {scrub(g['title'])}" for g in doc["top"][:4]) + f"; {len(doc['blind_spots'])} blind spots"
        try:
            r = await ctx.llm([{"role": "system", "content": "You write exactly two short plain sentences summarising an infrastructure status. Use only the facts given, add no numbers or names that are not in them."},
                               {"role": "user", "content": facts}], options={"num_predict": 90})
            txt = r["text"].strip().replace("\n", " ")
            if numbers(txt) <= numbers(facts) and 20 < len(txt) < 400:
                doc["headline"] = scrub(txt)
        except Exception:
            pass                                    # the template headline stays
        doc = json.loads(scrub(json.dumps(doc)))        # the briefing outlives the 24 h evidence window, so it carries no outside addresses at all
        bid = store.save_briefing(doc["date"], doc, "manual" if ctx.trigger == "manual" else "schedule")
        store.audit("agent:commander", "briefing", f"#{bid}", doc["headline"][:200])
        rt.bus.publish("council.briefing", agent=self.name, id=bid, topics=doc["counts"]["topics_attention"], trigger="manual" if ctx.trigger == "manual" else "schedule")
        ctx.note(f"briefing #{bid}: {doc['headline']}")

    # ---- orchestration: the Commander routes work between agents, then judges
    ROUTES = {   # what to ask whom when an incident forms around a machine
        "network": [("sentinel", {}), ("iot_watcher", {}), ("dns_watcher", {})],
        "probe": [("exposure_auditor", "hosts")],
    }

    def plan(self, rt, th, now):
        """-> [{'agent','ask','args'}]: agents worth asking again. Skips the ones that just ran, are busy, or are switched off."""
        out, host = [], th.get("entity")
        for name, args in self.ROUTES["network"] + [(n, {"hosts": [host]}) for n, _ in self.ROUTES["probe"]]:
            a = rt.agents.get(name)
            if not a or not rt.enabled(name) or (name in rt.tasks and not rt.tasks[name].done()):
                continue
            if now - (rt.state[name]["last_run"] or 0) < 120:
                continue
            ask = "scan this one machine's open ports now" if name == "exposure_auditor" else "look again at the network now and refresh your observations"
            out.append({"agent": name, "ask": ask, "args": args})
        return out

    async def handle_incidents(self, ctx):
        rt, store, cfg = ctx.runner, ctx.runner.store, ctx.cfg
        now, steps = time.time(), 0
        incidents = [f for f in store.open_findings(200) if f["agent"] == "correlator" and (SEV_RANK[f["severity"]] >= SEV_RANK["medium"] or (f["thread"] or {}).get("debate"))]
        for inc in incidents[:3]:
            th = dict(inc["thread"] or {})
            if not th.get("entity"):
                continue
            # 1. ask the other agents (once per incident)
            if not th.get("dispatch"):
                ctx.working("thinking", "asking the other agents")
                asked = []
                for p in self.plan(rt, th, now):
                    started = rt.trigger(p["agent"], "commander", p["args"])
                    rt.bus.publish("council.assign", agent=self.name, to=p["agent"], reason=f"incident on {th['entity']}: {p['ask']}"[:80])
                    store.audit("agent:commander", "assign", p["agent"], f"incident {inc['id']}: {p['ask']}")
                    asked.append({**p, "started": started})
                wait = [rt.tasks[a["agent"]] for a in asked if a["started"] and a["agent"] in rt.tasks]
                if wait:
                    await asyncio.wait(wait, timeout=120)
                th["dispatch"] = [{"agent": a["agent"], "ask": a["ask"], "at": now, "result": (rt.state[a["agent"]]["last_summary"] or rt.state[a["agent"]]["last_status"] or "")[:160] if a["started"] else "was already busy"} for a in asked]
                th["dispatched_at"] = now
                store.set_thread(inc["id"], th)
                steps += 1
            # 2. convene the council when there is something new to discuss
            deb = th.get("debate")
            stale = not deb or th.get("debate_sig") != th.get("sig")
            deep_due = deb and th.get("deep_pending") and ctx.runner.gateway.quiet_now() and cfg["gateway"].get("council_model")
            if (stale and now - th.get("debate_at", 0) > 1800) or deep_due:
                steps += await self.convene(ctx, inc, th, deep=bool(deep_due))
        return steps

    async def convene(self, ctx, inc, th, deep=False):
        rt, store, cfg = ctx.runner, ctx.runner.store, ctx.cfg
        gw = rt.gateway
        council_model = cfg["gateway"].get("council_model") or None
        use = council_model if council_model and (deep or gw.quiet_now() or council_model in await gw.loaded_models()) else None
        pretty = {a: (rt.agents[a].title if a in rt.agents else a) for a in th.get("agents", [])}
        members = [f for f in store.open_findings(500) if f["id"] in set(inc.get("links") or [])]
        lines = [f"- {pretty.get(f['agent'], f['agent'])} reports ({f['severity']}): {scrub(f['title'])}. {scrub(f['summary'])[:160]}" for f in members[:6]]
        facts = (f"Machine: {th['entity']}. {len(th['agents'])} different agents report on it within the last hour.\n" + "\n".join(lines)
                 + (f"\nA configuration change was recorded on this machine recently." if th.get("change") else "\nNo configuration change was recorded on this machine recently.")
                 + "".join(f"\nWhen asked to look again, {pretty.get(d['agent'], d['agent'])} answered: {scrub(d['result'])}" for d in th.get("dispatch", []) if d.get("result")))
        incident = {"agents": len(th["agents"]), "max_member": th.get("max_member", "low"), "change": th.get("change"), "severity": inc["severity"]}

        async def llm(role, messages):
            r = await gw.chat(role, messages, model=use, options={"num_predict": 150}, allow_swap=bool(use and gw.quiet_now()), timeout=120)
            return r["text"]

        def emit(role, stance, text):
            rt.bus.publish("council.debate", agent=role, incident=inc["id"], stance=stance, text=scrub(text)[:120])

        ctx.working("thinking", "convening the council")
        rt.bus.publish("council.convened", agent=self.name, incident=inc["id"])
        deb = await deliberate(llm, incident, facts, model=use, emit=emit)
        deb["mode"] = "deep" if use else "quick"
        th.update(debate=deb, debate_sig=th.get("sig"), debate_at=time.time(), deep_pending=bool(council_model and not use))
        store.set_thread(inc["id"], th)
        sev = apply_verdict(th.get("computed", inc["severity"]), deb["verdict"])
        if sev != inc["severity"]:
            store.x("UPDATE findings SET severity=?, changed_at=? WHERE id=?", (sev, time.time(), inc["id"]))
        store.audit("agent:commander", "council.verdict", inc["id"], f"{th['entity']}: {deb['verdict']} ({deb['mode']}, {'consensus' if deb['consensus'] else 'after rebuttal'})")
        rt.bus.publish("council.verdict", agent=self.name, incident=inc["id"], verdict=deb["verdict"], host=th["entity"])
        return 1
