"""The Team: projects the agents plan and carry out TOGETHER.

You give the team a goal ("why is the backups disk filling?"). Then:
  1. PLAN    The Commander turns the goal into tasks for the agents that can help. The routing is plain rules (so the plan is reproducible); the model may
             suggest up to two more tasks from the menu of skills, which are validated before they are accepted. The plan is posted to the shared thread.
  2. WORK    Each agent runs its skill (read-only), posts its facts to the thread and, when its facts call for it, ASKS A COLLEAGUE FOR HELP: a result can carry
             follow-up requests ("this guest is most of the backup: Config Drift, what is mounted on it?"). Follow-ups are validated, de-duplicated and limited
             (two levels deep, a step budget). Agents also propose manual steps with a rollback plan.
  3. TALK    Each agent may add one short reading of its own facts (model, dropped if it contains a number that is not in the facts).
  4. REVIEW  The Commander writes the summary; the Critic reviews the proposed steps against what the other agents currently see (rules + a devil's-advocate
             paragraph) and says agree / caution / disagree.
  5. YOU     You can ask follow-up questions in the thread (the Commander plans more tasks), approve steps that actively probe the network, stop the project, and
             send any recommendation to the Approvals page. Nothing here executes a change: agents read and propose.
Everything is stored (60 days), outside addresses are masked in what is stored, and every task is a normal agent run with its own trace."""
import asyncio, json, re, time

from . import trace
from .entities import entities_of, alias_map, vlan_of
from .redact import scrub

SEV_RISK = {"low": 0, "medium": 1, "high": 2}
STOP = {"the", "and", "for", "why", "what", "how", "are", "our", "with", "from", "this", "that", "does", "can", "should", "about", "have", "has", "was", "any", "into", "need", "get", "make", "please"}
STORAGE_WORDS = ("backups", "local-lvm", "local")
MAX_DEPTH, PARALLEL, MAX_TAKES = 2, 3, 8
ADVISOR_NAME = {"claude": "Claude", "openai": "OpenAI", "gemini": "Gemini"}


def keywords(text, n=4):
    ws = [w for w in re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", text.lower()) if w not in STOP]
    return sorted(dict.fromkeys(ws), key=lambda w: -len(w))[:n]


def numbers(t):
    return set(re.findall(r"\d+(?:\.\d+)?", t or ""))


def norm_args(a):
    return json.dumps(a or {}, sort_keys=True)


def targets_of(goal, store):
    """Which machines, guests and storages does the goal talk about? -> (hosts, guests[(id, name)], storages)"""
    g = goal.lower()
    al = alias_map(store)
    hosts = sorted(entities_of(goal, aliases=al)["hosts"])
    base = (store.kv_get("config_drift:baseline") or {}).get("items", {})
    guests = []
    for gid, c in base.items():
        if not gid.startswith(("lxc/", "qemu/")):
            continue
        nm = str(c.get("hostname") or c.get("name") or "").lower()
        if re.search(r"(?<![\w/-])" + re.escape(gid) + r"\b", g) or (nm and len(nm) > 2 and re.search(r"(?<![\w-])" + re.escape(nm) + r"(?![\w-])", g)):
            guests.append((gid, nm))
    storages = [w for w in STORAGE_WORDS if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", g)]
    return hosts, guests, storages


def route(goal, store, catalogue, auto=False):
    """Rules: goal -> [{agent, skill, args, why, source:'rules'}], only skills that exist in `catalogue` ({agent: {skill: spec}}), de-duplicated.
    Active probes (network scans) and web searches are left out of automatic projects."""
    g = goal.lower()
    hosts, guests, storages = targets_of(goal, store)
    al = alias_map(store)
    from .entities import guest_to_ip
    ips = list(hosts) + [guest_to_ip(gid, al) for gid, _ in guests if guest_to_ip(gid, al) and guest_to_ip(gid, al) not in hosts]
    target = storages[0] if storages else (guests[0][1] or guests[0][0]) if guests else ""
    steps = []

    def add(agent, skill, args, why):
        if agent in catalogue and skill in catalogue[agent]:
            sp = catalogue[agent][skill]
            if auto and (sp.get("active") or sp.get("external")):
                return
            if not any(s["agent"] == agent and s["skill"] == skill and norm_args(s["args"]) == norm_args(args) for s in steps):
                steps.append({"agent": agent, "skill": skill, "args": args, "why": why, "source": "rules"})

    disk = re.search(r"disk|storage|space|full\b|fill|capacity|thin|lvm|volume|grow|gb\b", g) or storages
    backup = re.search(r"backup|restore|vzdump|retention|prune|snapshot", g)
    net = ips or re.search(r"network|traffic|scan|flows?|device|iot|suspicious|connect|exposed|ports?\b|behav", g)
    patch = re.search(r"patch|update|upgrade|kernel|reboot|package|version", g)
    logs = re.search(r"\blogs?\b|siem|syslog|suricata|opnsense|loki|\bids\b|firewall", g)
    dns = re.search(r"\bdns\b|resolver|domain|nextdns", g)
    research = re.search(r"research|look up|how (do|to|can|should)|what is|best (way|practice)|compare|recommend|\bplan\b|set ?up|configure|guide|explain", g)
    if disk or backup:
        t = target or ("backups" if backup else "")
        add("capacity_planner", "forecast", {"target": t}, f"how fast is {t or 'each disk'} filling, and is it speeding up?")
        add("alert_triage", "metric_now", {"target": t}, f"how full is {t or 'each disk'} right now and how did it move in a day?")
        add("backup_warden", "report", {"guest": target if target and target not in STORAGE_WORDS else ""}, "what do the last backup runs hold, and what grew?")
        add("config_drift", "describe", {"item": "backup" if (backup or t == "backups") else (target or "storage")}, "what do the configuration and the retention settings say?")
        add("archivist", "history", {"query": t or (keywords(goal, 1)[0] if keywords(goal, 1) else "")}, "has this come up before, and what did you decide?")
    if net:
        for h in ips[:2]:
            add("sentinel", "network_activity", {"host": h}, f"what has {h} been talking to?")
            if vlan_of(h) == "50":
                add("iot_watcher", "device", {"host": h}, f"how does {h} normally behave?")
            add("exposure_auditor", "scan", {"hosts": [h]}, f"what does {h} have open itself?")
            add("archivist", "history", {"query": h}, f"has {h} come up before?")
        for gid, _ in guests[:1]:
            add("config_drift", "describe", {"item": gid}, f"how is {gid} configured?")
        if not ips:
            for a in ("sentinel", "iot_watcher", "dns_watcher"):
                add(a, "status", {}, "what do you see on the network right now?")
    for gid, _ in guests[:1]:                           # any named guest (VMs have no known address, so the network branch above can miss them)
        add("config_drift", "describe", {"item": gid}, f"how is {gid} configured?")
        if re.search(r"\b(chang|drift|modif|differ|before|after|what (?:was|were) (?:set|edited))", g):
            add("config_drift", "changes", {"item": gid}, f"what exactly changed in {gid}, before and after?")
            add("archivist", "history", {"query": gid}, f"what does the history say about {gid}?")
    if patch:
        add("patch_scout", "status", {}, "what is pending, and is a reboot needed?")
        add("backup_warden", "status", {}, "are the backups healthy before any risky work?")
        add("archivist", "history", {"query": "kernel"}, "has patching come up before?")
    if logs:
        add("ids_doctor", "status", {}, "is the firewall and intrusion log pipeline working?")
        add("sentinel", "status", {}, "what can the network agent see?")
        add("backlog_keeper", "tasks", {"query": "syslog opnsense loki logs"}, "what is already planned about logging?")
    if dns:
        add("dns_watcher", "status", {}, "how is DNS doing?")
        add("archivist", "history", {"query": "dns"}, "has DNS come up before?")
    if research:
        add("research_librarian", "research", {"query": re.sub(r"\s+", " ", goal).strip()[:160]}, "what does the web say about this?")
        add("backlog_keeper", "tasks", {"query": goal}, "what do the handoff documents already say about this?")
        if keywords(goal, 1):
            add("archivist", "history", {"query": keywords(goal, 1)[0]}, "has this come up before?")
    if not steps:
        add("backlog_keeper", "tasks", {"query": goal}, "what do the handoff documents already say?")
        if keywords(goal, 1):
            add("archivist", "history", {"query": keywords(goal, 1)[0]}, "has this come up before?")
        add("research_librarian", "research", {"query": re.sub(r"\s+", " ", goal).strip()[:160]}, "what does the web say?")
    return steps


def fill_args(spec, goal, store):
    """Arguments for a skill the model suggested, taken from the goal; None when a required one cannot be filled."""
    hosts, guests, storages = targets_of(goal, store)
    out = {}
    for a in spec.get("args", {}):
        if a == "target":
            out[a] = storages[0] if storages else (guests[0][1] or guests[0][0]) if guests else ""
        elif a in ("query",):
            out[a] = re.sub(r"\s+", " ", goal).strip()[:160]
        elif a == "item":
            out[a] = guests[0][0] if guests else (storages[0] if storages else "backup")
        elif a == "host":
            if not hosts:
                return None
            out[a] = hosts[0]
        elif a == "hosts":
            if not hosts:
                return None
            out[a] = hosts[:1]
        elif a == "guest":
            out[a] = guests[0][1] if guests else ""
        else:
            return None
    return out


def keep_execute(r):
    from .actionspec import check_execute
    try:
        return check_execute(r["execute"]) if r.get("execute") else None
    except ValueError:
        return None


def norm_result(res):
    """Whatever a skill returned -> the safe, bounded document stored in the thread. Outside addresses are masked."""
    res = res or {}
    follow = []
    for f in (res.get("followups") or [])[:4]:
        if isinstance(f, dict) and f.get("agent") and f.get("skill"):
            follow.append({"agent": str(f["agent"]), "skill": str(f["skill"]), "args": f["args"] if isinstance(f.get("args"), dict) else {}, "why": scrub(str(f.get("why", "")))[:200]})
    rec = []
    for r in (res.get("recommend") or [])[:3]:
        if isinstance(r, dict) and r.get("title") and r.get("steps") and r.get("rollback"):
            rec.append({"title": scrub(str(r["title"]))[:140], "risk": r.get("risk") if r.get("risk") in SEV_RISK else "medium",
                        "steps": [scrub(str(x))[:300] for x in r["steps"]][:6], "rollback": [scrub(str(x))[:300] for x in r["rollback"]][:5], **({"execute": keep_execute(r)} if keep_execute(r) else {})})
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    try:
        data = json.loads(scrub(json.dumps(data))) if len(json.dumps(data)) < 6000 else {}
    except (TypeError, ValueError):
        data = {}
    return {"summary": scrub(str(res.get("summary") or ""))[:300], "facts": [scrub(str(f))[:320] for f in (res.get("facts") or [])][:14], "key": [scrub(str(f))[:320] for f in (res.get("key") or [])][:3],
            "followups": follow, "recommend": rec, "data": data}


class Team:
    def __init__(self, runner, notifier=None, frontier=None):
        self.rt, self.store, self.bus, self.cfg = runner, runner.store, runner.bus, runner.cfg
        self.notifier, self.frontier = notifier, frontier
        self.workers, self.questions, self.takes = {}, {}, {}
        self.sem = asyncio.Semaphore(2)               # two projects work at once; the model gateway still serves one request at a time
        self.watch_task = None

    # ------------------------------------------------------------------ thread helpers
    def post(self, pid, author, kind, text, data=None):
        mid = self.store.msg_add(pid, author, kind, scrub(text), data)
        self.bus.publish("project.message", agent=author, project=pid, kind=kind, id=mid)
        return mid

    def catalogue(self, only=None):
        return self.rt.skill_catalogue(only or None)

    def title_of(self, name):
        a = self.rt.agents.get(name)
        return a.title if a else name

    # ------------------------------------------------------------------ entry points
    def create(self, goal, title=None, origin="owner", ref="", agents=None, approve_plan=False):
        goal = re.sub(r"\s+", " ", goal).strip()
        if len(goal) < 8:
            raise ValueError("describe the goal in a sentence (at least 8 characters)")
        agents = [a for a in (agents or []) if a in self.rt.agents]
        title = (title or goal)[:90]
        pid = self.store.project_create(title, goal, origin, ref, agents, approve_plan)
        self.store.audit("owner" if origin == "owner" else "agent:commander", "project.create", pid, f"{origin}: {title[:100]}")
        self.post(pid, "owner" if origin == "owner" else "commander", "goal", goal if origin == "owner" else f"(opened automatically) {goal}")
        self.bus.publish("project.created", agent="commander", project=pid, title=title)
        self._ensure(pid, plan=True)
        return pid

    def _ensure(self, pid, plan=False):
        t = self.workers.get(pid)
        if t and not t.done():
            return
        self.workers[pid] = asyncio.create_task(self._run(pid, plan))

    async def ask(self, pid, text):
        p = self.store.project_get(pid)
        if not p:
            raise KeyError(pid)
        text = re.sub(r"\s+", " ", text).strip()[:600]
        if not text:
            raise ValueError("write a question or an instruction")
        self.post(pid, "owner", "owner", text)
        self.questions.setdefault(pid, []).append(text)
        if p["status"] in ("done", "stopped", "interrupted", "failed"):
            self.store.project_update(pid, status="working")
        self._ensure(pid)

    def approve(self, pid):
        n = 0
        for t in self.store.tasks(pid, "waiting_ok"):
            self.store.task_update(pid, t["n"], status="pending", needs_ok=0); n += 1
        if n:
            self.post(pid, "owner", "owner", f"Approved {n} step(s).")
            self.store.project_update(pid, status="working")
            self._ensure(pid)
        return n

    def skip(self, pid, n):
        t = [x for x in self.store.tasks(pid) if x["n"] == n]
        if not t or t[0]["status"] not in ("pending", "waiting_ok"):
            return False
        self.store.task_update(pid, n, status="skipped", finished=time.time())
        self.post(pid, "owner", "owner", f"Skipped step {n} ({self.title_of(t[0]['agent'])}: {t[0]['skill']}).")
        return True

    def stop(self, pid):
        w = self.workers.get(pid)
        if w and not w.done():
            w.cancel()
        for t in self.store.tasks(pid):
            if t["status"] in ("pending", "waiting_ok", "running"):
                self.store.task_update(pid, t["n"], status="skipped", finished=time.time())
        self.store.project_update(pid, status="stopped")
        self.post(pid, "owner", "owner", "Stopped the project.")
        self.bus.publish("project.done", agent="commander", project=pid, status="stopped")

    def recover(self):
        """After a restart: projects that were working were cut off. They stay readable and the owner can ask the team to carry on."""
        for p in self.store.project_list(200):
            if p["status"] in ("planning", "working"):
                for t in self.store.tasks(p["id"]):
                    if t["status"] == "running":
                        self.store.task_update(p["id"], t["n"], status="pending")
                self.store.project_update(p["id"], status="interrupted")
                self.post(p["id"], "commander", "note", "This project was interrupted by a restart of the system. Ask the team to continue (any message in this thread) and it picks up the remaining steps.")

    # ------------------------------------------------------------------ the project run
    async def _run(self, pid, plan):
        async with self.sem:
            try:
                p = self.store.project_get(pid)
                if plan:
                    await self._plan(p)
                await self._work(pid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.project_update(pid, status="failed")
                self.post(pid, "commander", "note", f"The project stopped because of an error: {type(e).__name__}: {str(e)[:160]}")
                self.bus.publish("project.done", agent="commander", project=pid, status="failed")

    async def _plan(self, p, goal=None, requested_by="commander"):
        pid, goal = p["id"], goal or p["goal"]
        cat = self.catalogue(p["agents"])
        auto = p["origin"] == "auto"
        steps = route(goal, self.store, cat, auto)
        extra = await self._model_extra(p, goal, cat, steps, auto)
        steps += extra
        max_steps = self.cfg.get()["team"]["max_steps"]
        have = len(self.store.tasks(pid))
        steps = steps[: max(0, max_steps - have)]
        if not steps:
            self.post(pid, "commander", "plan", "I have no agent that can help with that. The team can look at disks and storage, backups, a machine's network behaviour, patches, logging, DNS, and can research or look up earlier decisions.")
            self.store.project_update(pid, status="done")
            return
        lines = []
        for s in steps:
            sp = cat[s["agent"]][s["skill"]]
            ok = bool(sp.get("active")) or bool(p["approve_plan"])
            n = self.store.task_add(pid, s["agent"], s["skill"], s["args"], s["why"], s["source"], requested_by, 0, ok)
            lines.append({"n": n, "agent": s["agent"], "skill": s["skill"], "args": s["args"], "why": s["why"], "source": s["source"], "needs_ok": ok, "active": bool(sp.get("active")), "external": bool(sp.get("external"))})
        agents = sorted({l["agent"] for l in lines})
        self.post(pid, "commander", "plan", f"Plan: {len(lines)} step(s) for {', '.join(self.title_of(a) for a in agents)}." + (" Steps marked 'needs your OK' scan the network and wait for you." if any(l["needs_ok"] for l in lines) else ""), {"tasks": lines})
        for a in agents:                                                  # each agent answers the call: what it will do
            sk = [l["skill"] for l in lines if l["agent"] == a]
            self.post(pid, a, "ack", "On it: " + "; ".join(cat[a][k]["about"] for k in dict.fromkeys(sk)) + ".")
        self.bus.publish("project.plan", agent="commander", project=pid, agents=agents)

    async def _model_extra(self, p, goal, cat, steps, auto):
        """The model may add up to two tasks, chosen from the menu. Anything not on the menu, or needing arguments we cannot fill, is ignored."""
        menu = [f"{a}.{s}: {sp['about']}" for a, sk in cat.items() for s, sp in sk.items() if s != "status" and a not in ("commander", "critic", "correlator", "watchdog")
                and not (auto and (sp.get("active") or sp.get("external")))]
        planned = [f"{s['agent']}.{s['skill']}" for s in steps]
        if not menu:
            return []
        sysm = "You plan a small infrastructure investigation. From the menu, name at most 2 MORE skills that would help the goal and are not already planned. Reply with only lines of the form agent.skill, or the word NONE."
        userm = f"Goal: {goal}\nAlready planned: {', '.join(planned) or 'nothing'}\nMenu:\n" + "\n".join(menu)
        text, by = None, "the Commander's model"
        if self.frontier and self.frontier.available("plan"):
            try:
                rr = await self.frontier.ask("plan", sysm, userm, 200); text = rr["text"]; by = ADVISOR_NAME.get(rr.get("advisor", "claude"), "Claude")
            except Exception:
                text = None
        if text is None:
            try:
                text = (await self.rt.gateway.chat("commander", [{"role": "system", "content": sysm}, {"role": "user", "content": userm}], options={"num_predict": 40}, timeout=60))["text"]
            except Exception:
                return []
        out = []
        for line in text.splitlines():
            m = re.fullmatch(r"\s*[-*]?\s*`?([a-z_]+)\.([a-z_]+)`?\s*", line.strip())
            if not m or f"{m.group(1)}.{m.group(2)}" in planned or len(out) >= 2:
                continue
            a, sk = m.groups()
            sp = cat.get(a, {}).get(sk)
            if not sp or sk == "status" or (auto and (sp.get("active") or sp.get("external"))):
                continue
            args = fill_args(sp, goal, self.store)
            if args is not None and any(len(str(v)) > 0 for v in args.values()) or (args == {} and sp.get("args") == {}):
                out.append({"agent": a, "skill": sk, "args": args, "why": f"suggested by {by}", "source": "claude" if by in ADVISOR_NAME.values() else "model"})
        return out

    async def _work(self, pid):
        self.store.project_update(pid, status="working")
        if not await self._drain(pid, True):
            return
        waiting = self.store.tasks(pid, "waiting_ok")
        if waiting:
            self.store.project_update(pid, status="awaiting_ok")
            self.post(pid, "commander", "note", f"{len(waiting)} step(s) wait for your OK: " + "; ".join(f"{self.title_of(t['agent'])}: {t['skill']}" for t in waiting) + ". Press 'Approve' to let them run, or skip them.")
        more = await self._synthesize(pid, final=not waiting)
        if more and await self._drain(pid, False):                              # the outside reviewer asked for more facts: one more round, then the summary is rewritten
            await self._synthesize(pid, final=not self.store.tasks(pid, "waiting_ok"), refined=True)

    async def _drain(self, pid, take_questions):
        """Run every queued task (and plan for the owner's questions). False when the project was stopped meanwhile."""
        cfg_steps = self.cfg.get()["team"]["max_steps"]
        while True:
            for q in (self.questions.pop(pid, []) if take_questions else []):                                  # the owner asked something: plan more steps
                p = self.store.project_get(pid)
                before = len(self.store.tasks(pid))
                await self._plan(p, goal=f"{q} (about: {p['goal'][:200]})", requested_by="owner")
                if len(self.store.tasks(pid)) == before:
                    self.post(pid, "commander", "note", "Nothing new to ask the team for that: the steps that apply have already been done. Check the summary below, or word the question differently.")
            todo = self.store.tasks(pid, "pending")
            if not todo:
                break
            batch = todo[:PARALLEL]
            for t in batch:
                self.store.task_update(pid, t["n"], status="running", started=time.time())
            await asyncio.gather(*(self._step(pid, t, cfg_steps) for t in batch))
            if self.store.project_get(pid)["status"] == "stopped":
                return False
        return True

    async def _step(self, pid, t, max_steps):
        agent, skill, n = t["agent"], t["skill"], t["n"]
        self.bus.publish("project.step", agent=agent, project=pid, n=n, status="running")
        try:
            res, rid = await self.rt.run_skill(agent, skill, t["args"], project=pid)
            r = norm_result(res)
            self.store.task_update(pid, n, status="done", result=r, run_id=rid, finished=time.time())
            self.post(pid, agent, "result", r["summary"], {"n": n, "skill": skill, "facts": r["facts"], "run_id": rid, "followups": [f"{f['agent']}.{f['skill']}" for f in r["followups"]], "recommend": [x["title"] for x in r["recommend"]]})
            take = await self._take(pid, agent, r) if await self._stronger() else None        # a small model misreads facts too often to be trusted with 'what this means'
            if take:
                self.post(pid, agent, "take", take)
            self._follow_up(pid, t, r, max_steps)
        except Exception as e:
            self.store.task_update(pid, n, status="failed", result={"summary": f"{type(e).__name__}: {str(e)[:200]}", "facts": [], "followups": [], "recommend": [], "data": {}}, finished=time.time())
            self.post(pid, agent, "result", f"I could not do '{skill}': {type(e).__name__}: {str(e)[:160]}", {"n": n, "skill": skill, "facts": [], "failed": True})
        self.bus.publish("project.step", agent=agent, project=pid, n=n, status="done")

    def _follow_up(self, pid, t, r, max_steps):
        """An agent's result may ask colleagues for help. Validated, unique, two levels deep, within the step budget."""
        p = self.store.project_get(pid)
        cat = self.catalogue(p["agents"])
        existing = self.store.tasks(pid)
        auto = p["origin"] == "auto"
        for f in r["followups"]:
            sp = cat.get(f["agent"], {}).get(f["skill"])
            if not sp or t["depth"] + 1 > MAX_DEPTH or len(existing) >= max_steps:
                continue
            if auto and (sp.get("active") or sp.get("external")):
                continue
            if any(x["agent"] == f["agent"] and x["skill"] == f["skill"] and norm_args(x["args"]) == norm_args(f["args"]) for x in existing):
                continue
            ok = bool(sp.get("active")) or bool(p["approve_plan"])
            n = self.store.task_add(pid, f["agent"], f["skill"], f["args"], f["why"], "request", t["agent"], t["depth"] + 1, ok)
            existing = self.store.tasks(pid)
            self.post(pid, t["agent"], "request", f"@{self.title_of(f['agent'])}: {f['why']}", {"n": n, "agent": f["agent"], "skill": f["skill"], "needs_ok": ok})

    async def _stronger(self):
        """The council model, when it can be used right now (quiet hours, or already loaded): the only time agents add their own reading."""
        cm = self.cfg.get()["gateway"].get("council_model") or ""
        gw = self.rt.gateway
        return bool(cm and (gw.quiet_now() or cm in await gw.loaded_models()))

    async def _take(self, pid, agent, r):
        """One short reading of the agent's own facts. Dropped if it adds a number that is not in the facts."""
        if len(r["facts"]) < 2 or self.takes.get(pid, 0) >= MAX_TAKES:
            return None
        self.takes[pid] = self.takes.get(pid, 0) + 1
        p = self.store.project_get(pid)
        facts = "\n".join(r["facts"][:8])
        try:
            out = await self.rt.gateway.chat(agent, model=self.cfg.get()["gateway"]["council_model"], messages=[{"role": "system", "content": f"You are {self.title_of(agent)}, a member of a small infrastructure team working on: {p['goal'][:200]}. In at most two plain sentences say what your findings mean for that goal. Use only the facts given; add no numbers or names that are not in them."},
                                                      {"role": "user", "content": facts}], options={"num_predict": 90}, timeout=60)
        except Exception:
            return None
        t = re.sub(r"\s+", " ", out["text"]).strip()
        if len(t) > 80 and not t.endswith((".", "!", "?")) and "." in t:
            t = t[:t.rindex(".") + 1]
        if not 20 < len(t) < 420 or not numbers(t) <= numbers(facts + p["goal"]):
            return None
        return t + "  (the model's reading)"

    # ------------------------------------------------------------------ summary and review
    async def _synthesize(self, pid, final=True, refined=False):
        """Writes (or rewrites) the summary and the review. Returns True when the outside reviewer asked the team for more facts."""
        p = self.store.project_get(pid)
        tasks = [t for t in self.store.tasks(pid) if t["status"] == "done" and t["result"]]
        if not tasks:
            self.store.project_update(pid, status="done" if final else p["status"])
            return False
        recs, seen = [], set()
        for t in tasks:
            for r in t["result"]["recommend"]:
                if r["title"] not in seen:
                    seen.add(r["title"]); recs.append({**r, "agent": t["agent"], "proposed": None})
        lead = [(t["agent"], t["result"]["summary"]) for t in tasks if t["result"]["summary"]]
        keyf, seen_k = [], set()
        for t in tasks:                                                    # each agent's most decision-relevant lines (its own 'key' facts, else the first two)
            for f in (t["result"].get("key") or t["result"]["facts"][:2]):
                if f not in seen_k and not f.startswith("  "):
                    seen_k.add(f); keyf.append(f"{self.title_of(t['agent'])}: {f}")
        facts = keyf[:14]
        ftxt = "\n".join(facts)
        narrative, source = None, "rules"
        use = self.cfg.get()["gateway"].get("council_model") if await self._stronger() else None
        try:
            out = await self.rt.gateway.chat("commander", [{"role": "system", "content": "You write the summary of a team investigation in 3 to 4 plain sentences. Say what is going on, name the biggest contributor and any setting that multiplies it if the facts show them, and say what to do first. Use only the facts given; add no numbers or names that are not in them."},
                                                           {"role": "user", "content": f"Goal: {p['goal'][:300]}\nKey facts from the team:\n{ftxt}"}], model=use, options={"num_predict": 220}, timeout=120)
            t = re.sub(r"\s+", " ", out["text"]).strip()
            if len(t) > 100 and not t.endswith((".", "!", "?")) and "." in t:
                t = t[:t.rindex(".") + 1]
            if 40 < len(t) < 900 and numbers(t) <= numbers(ftxt + p["goal"]):
                narrative, source = t, "model"
        except Exception:
            pass
        if not narrative:
            narrative = "What the team found: " + " ".join(f"{self.title_of(a)}: {s}" for a, s in lead[:4])
        review = await self._critique(p, recs)
        outside, more = None, False
        if self.frontier and self.frontier.available("review"):
            outside = await self._outside(p, ftxt, recs, narrative, review, refined)
            if outside:
                if outside.get("summary"):
                    narrative, source = outside["summary"], "claude"
                verdict_txt = {"agree": "agrees with the proposed steps", "caution": "advises caution", "disagree": "advises AGAINST acting yet"}[outside["verdict"]]
                self.post(pid, outside.get("advisor", "claude"), "review", f"Outside review by {ADVISOR_NAME.get(outside.get('advisor', 'claude'), 'Claude')} ({'subscription' if outside['backend'] == 'cli' else 'API'}, {outside['model']}): {verdict_txt}. " + " ".join(outside["concerns"])
                          + (f" I would also ask the team for: {', '.join(m['agent'] + '.' + m['skill'] for m in outside['missing'])}." if outside["missing"] else ""), {"verdict": outside["verdict"]})
                if outside["missing"] and not refined:
                    for m in outside["missing"]:
                        self.store.task_add(pid, m["agent"], m["skill"], m["args"], "asked for by the outside reviewer", "claude", "claude", 1, bool(self.catalogue(p["agents"])[m["agent"]][m["skill"]].get("active", False)))
                    more = True
        disputed = bool(outside and outside["verdict"] == "disagree" and source != "claude")
        res = {"disputed": disputed, "dispute": ({"advisor": outside.get("advisor", "claude"), "view": outside.get("view"), "concerns": outside["concerns"]} if disputed else None),
               "summary": scrub(narrative), "source": source, "outside": ({k: outside[k] for k in ("backend", "model", "verdict", "concerns")} if outside else None), "key_findings": facts[:8], "model": use or "default", "recommendations": recs, "critic": review, "agents": sorted({t["agent"] for t in tasks}), "done_at": time.time(), "version": ((p["result"] or {}).get("version", 0)) + 1}
        self.store.project_update(pid, status=("done" if final else p["status"]) if not more else "working", result=res)
        self.post(pid, "commander", "summary", res["summary"], {"source": source, "recommendations": [r["title"] for r in recs]})
        if disputed:
            adv = res["dispute"]["advisor"]
            self.post(pid, adv, "critique", f"{ADVISOR_NAME.get(adv, adv)} disputes this summary. " + (res["dispute"]["view"] or " ".join(res["dispute"]["concerns"])), {"verdict": "disagree"})
            self.bus.publish("project.disputed", agent=adv, project=pid, title=p["title"])
        if more:
            return True
        self.bus.publish("project.done", agent="commander", project=pid, status="done" if final else "awaiting_ok")
        if final and p["origin"] == "auto" and self.notifier:
            try:
                await self.notifier.team_done(self.store.project_get(pid))
            except Exception:
                pass
        return False

    async def _outside(self, p, ftxt, recs, local_summary, critic, refined):
        """Claude checks the local team's work: rewrites the summary from the facts, judges the proposed steps, and can ask for specific missing checks from the menu."""
        from .frontier import FrontierError, extract_json
        cat = self.catalogue(p["agents"])
        menu = [f"{a}.{s}: {sp['about']}" for a, sk in cat.items() for s, sp in sk.items() if s != "status" and a not in ("commander", "critic", "correlator", "watchdog") and not (p["origin"] == "auto" and (sp.get("active") or sp.get("external")))]
        system = ("You review the work of a small team of local AI agents that investigate a home lab (a Proxmox host with LXC guests, backups, network logs). You only see the digest below. "
                  "Reply with ONE JSON object and nothing else: {\"summary\": \"3 to 4 plain sentences: what is going on, the biggest contributor, and what to do first\", "
                  "\"verdict\": \"agree\" | \"caution\" | \"disagree\" (about the proposed manual steps), \"concerns\": [up to 4 short strings, what could go wrong or what the team got wrong], "
                  "\"missing\": [up to 2 strings of the form agent.skill from the menu that would settle an open question; [] if nothing is missing]}. "
                  "Use only the facts given; do not invent numbers or names.")
        steps = "\n".join(f"- {r['title']}: " + " / ".join(r["steps"]) + " (rollback: " + " / ".join(r["rollback"]) + ")" for r in recs) or "none"
        user = (f"Goal: {p['goal'][:300]}\nKey facts from the team:\n{ftxt}\n\nThe local summary: {local_summary}\n\nProposed manual steps:\n{steps}\n\n"
                f"The local Critic says: {critic['verdict']} {' '.join(critic['concerns'] + critic['blockers'])[:300]}\n\n"
                f"The owner's own notes (changes they made on purpose): {' | '.join(n['text'] for n in (self.store.kv_get('owner:notes') or [])[-8:] if time.time() - n['ts'] < 14 * 86400) or 'none'}\n\nMenu of skills:\n" + "\n".join(menu))
        try:
            r = await self.frontier.ask("review", system, user, 1400)
        except FrontierError:
            return None
        j = extract_json(r["text"])
        if not isinstance(j, dict) or j.get("verdict") not in ("agree", "caution", "disagree"):
            return None
        out = {"backend": r["backend"], "model": r["model"], "advisor": r.get("advisor", "claude"), "verdict": j["verdict"], "summary": None, "concerns": [], "missing": []}
        s = j.get("summary")
        out["view"] = scrub(re.sub(r"\s+", " ", s).strip())[:1600] if isinstance(s, str) and len(s) > 40 else None
        # adopt the advisor's summary as the headline only if every number in it came from the crew; otherwise it is still shown as the advisor's view when it disagrees
        if out["view"] and len(s) < 1600 and numbers(s) <= numbers(ftxt + p["goal"] + local_summary):
            out["summary"] = out["view"]
        out["concerns"] = [scrub(re.sub(r"\s+", " ", c).strip())[:220] for c in (j.get("concerns") or []) if isinstance(c, str)][:4]
        have = {(t["agent"], t["skill"], norm_args(t["args"])) for t in self.store.tasks(p["id"])}
        for item in (j.get("missing") or [])[:2]:
            m = re.fullmatch(r"\s*([a-z_]+)\.([a-z_]+)\s*", str(item))
            sp = cat.get(m.group(1), {}).get(m.group(2)) if m else None
            if not sp or m.group(2) == "status":
                continue
            args = fill_args(sp, p["goal"], self.store)
            if args is not None and (m.group(1), m.group(2), norm_args(args)) not in have:
                out["missing"].append({"agent": m.group(1), "skill": m.group(2), "args": args})
        return out

    async def _critique(self, p, recs):
        """The Critic reviews the proposed steps against what the other agents see now, then argues the other side."""
        from .agents.critic import assess
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cfg = self.cfg.get()
        qs, qe = (int(x.split(":")[0]) for x in cfg["gateway"]["quiet_hours"].split("-"))
        info = {"open": self.store.open_findings(300), "agents": {a["name"]: a for a in self.rt.describe()}, "now": time.time(), "local_hour": datetime.now(ZoneInfo(cfg["tz"])).hour, "quiet": (qs, qe)}
        steps = [s for r in recs for s in r["steps"]]
        if not recs:
            return {"verdict": "agree", "blockers": [], "concerns": [], "harmless_explanations": [], "verify_first": [], "note": "No steps were proposed, so there is nothing to object to."}
        risk = max((r["risk"] for r in recs), key=lambda x: SEV_RISK[x])
        fake = {"id": p["id"], "agent": "commander", "fingerprint": f"project:{p['id']}", "title": p["title"], "summary": "", "severity": "medium", "evidence": [1], "confidence": 0.8, "count": 2, "ts": time.time() - 7200,
                "proposed_action": {"kind": "manual", "risk": risk, "steps": steps, "rollback": ["see each recommendation"]}}
        rv = assess(fake, info)
        facts = f"Goal: {p['goal'][:200]}. Proposed: " + " | ".join(r["title"] for r in recs) + f". Checks already made: {'; '.join(rv['concerns'] + rv['blockers']) or 'none'}."
        try:
            out = await self.rt.gateway.chat("critic", [{"role": "system", "content": "You are a skeptical reviewer. In at most two short sentences, give the strongest honest reason these proposed steps might be unnecessary or risky. Use only the facts given; add no numbers or names that are not in them."},
                                                        {"role": "user", "content": facts}], options={"num_predict": 90}, timeout=60)
            t = re.sub(r"\s+", " ", out["text"]).strip()
            if 15 < len(t) < 420 and numbers(t) <= numbers(facts):
                rv["devils_advocate"] = t
        except Exception:
            pass
        rv["reviewed"] = time.time()
        self.post(p["id"], "critic", "critique", {"agree": "No objection to these steps.", "caution": "Proceed with caution.", "disagree": "Do NOT act yet."}[rv["verdict"]] + " " + " ".join(rv["blockers"] + rv["concerns"])[:500]
                  + (f" Devil's advocate (the model can be wrong): {rv['devils_advocate']}" if rv.get("devils_advocate") else ""), {"verdict": rv["verdict"], "verify_first": rv["verify_first"]})
        return rv

    def propose(self, pid, i):
        """Send one recommendation to the Approvals page as a finding with its steps and rollback plan. The owner still has to approve it there."""
        p = self.store.project_get(pid)
        recs = ((p or {}).get("result") or {}).get("recommendations") or []
        if not 0 <= i < len(recs):
            raise KeyError("no such recommendation")
        r = recs[i]
        if r.get("proposed"):
            return r["proposed"]
        o = (p["result"] or {}).get("outside")
        pa = {"kind": "manual", "risk": r["risk"], "steps": r["steps"], "rollback": r["rollback"], **({"execute": r["execute"]} if r.get("execute") else {})}
        f, _ = self.store.upsert_finding("commander", f"project:{pid}:rec{i}", "medium", r["title"], f"Proposed by the team in the project '{p['title']}'. " + (p["result"]["summary"][:300])
                                          + (f" Outside review ({o['model']}): {o['verdict']}. " + " ".join(o["concerns"])[:300] if o else ""),
                                          [{"type": "project", "ref": f"project {pid}", "excerpt": p["result"]["summary"][:300]}], pa, 0.7, evidence_ttl_s=7 * 86400)
        r["proposed"] = f["id"]
        self.store.project_update(pid, result=p["result"])
        self.store.audit("owner", "project.propose", f["id"], f"{r['title'][:100]} (project {pid})")
        self.post(pid, "owner", "owner", f"Sent to the Approvals page: {r['title']}")
        return f["id"]

    # ------------------------------------------------------------------ the team starts work on its own when a disk is about to fill
    async def watch_once(self, now=None):
        tm = self.cfg.get()["team"]
        if not tm["auto_projects"] or tm["max_auto_per_day"] <= 0:
            return 0
        now = now or time.time()
        day = self.store.kv_get("team:auto:day", {"d": "", "n": 0})
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if day["d"] != today:
            day = {"d": today, "n": 0}
        started = 0
        for f in self.store.open_findings(200):
            if f["agent"] not in ("capacity_planner", "alert_triage") or f["severity"] not in ("high", "critical"):
                continue
            if not f["fingerprint"].startswith(("capacity:", "storage:", "guest-disk:", "host-fs:")):
                continue
            last = self.store.kv_get(f"team:auto:{f['fingerprint']}", 0)
            last = last if isinstance(last, dict) else {"ts": last or 0, "sev": None, "pid": None}
            if now - last["ts"] < 24 * 3600 or day["n"] >= tm["max_auto_per_day"]:
                continue
            # Investigated already and nothing got worse: a new project every day changes nothing (it happened 3 nights in a row for a full disk).
            # Say once a day that it needs the owner, and look again only after a week or when the severity rises.
            prev = self.store.project_get(last["pid"]) if last.get("pid") else None
            if not prev:                                    # records from before 1.4 only kept the time: find the last auto project for this finding
                r = self.store.q("SELECT id FROM projects WHERE origin='auto' AND ref=? ORDER BY created DESC LIMIT 1", (f["id"],))
                prev = self.store.project_get(r[0][0]) if r else None
            if prev and prev["status"] == "done" and last.get("sev") in (None, f["severity"]) and now - prev["created"] < 7 * 86400:
                if now - last.get("told", 0) >= 24 * 3600:
                    last["told"] = now
                    self.store.kv_set(f"team:auto:{f['fingerprint']}", last)
                    self.bus.publish("team.repeat", agent="commander", project=prev["id"], title=f["title"],
                                     when=time.strftime("%a %d %b", time.localtime(prev["created"])))
                continue
            day["n"] += 1
            pid = self.create(f"Investigate: {f['title']}. Why is it happening and what should be done about it?", title=f"Investigate: {f['title']}"[:90], origin="auto", ref=f["id"])
            self.store.kv_set(f"team:auto:{f['fingerprint']}", {"ts": now, "sev": f["severity"], "pid": pid})
            started += 1
        self.store.kv_set("team:auto:day", day)
        return started

    async def _watch(self):
        await asyncio.sleep(90)
        while True:
            try:
                await self.watch_once()
            except Exception:
                pass
            await asyncio.sleep(300)

    def start(self):
        self.recover()
        self.watch_task = asyncio.create_task(self._watch())

    def tasks_running(self):
        return [t for t in self.workers.values() if not t.done()] + ([self.watch_task] if self.watch_task else [])
