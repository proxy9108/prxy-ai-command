"""Agent contract + scheduler.

An agent is a small class: set `name`, `group`, `title`, `autonomy` ("R" report, "P" propose, "A" act after approval) and either
`interval_s` or `at` (["HH:MM", ...] local time), then implement `async run(self, ctx)`. Deterministic code collects the facts;
call `ctx.llm(...)` only when judgement or plain-language text is needed. Every finding needs evidence.

How a run ends matters, because findings clear on their own when a run says 'this condition is gone':
  normal return            -> status "ok"; an agent with auto_resolve clears what it did not raise again
  ctx.skip("not due")      -> status "ok" but nothing is cleared: the agent did not look
  ctx.no_data("Loki down") -> status "degraded": the agent could not see, so nothing is cleared and the Watchdog hears about it
The runner records runs, emits events (the Office page animates from them) and writes the audit log."""
import asyncio, re, time, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import maintenance, trace
from .config import PARAM_SPECS, register_params
from .entities import entities_of, alias_map
from .gateway import GatewayError, AGENT

SEV_STRENGTH = {"info": 0.1, "low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}
SEV_ORDER = ["info", "low", "medium", "high", "critical"]


def bump(sev, n=1):
    return SEV_ORDER[max(0, min(len(SEV_ORDER) - 1, SEV_ORDER.index(sev) + n))]


class Agent:
    name = ""
    group = ""            # "security" | "operations" | "personal" | "housekeeping" | "council" | "test"
    title = ""
    autonomy = "R"
    interval_s = None
    at = None             # e.g. ["06:00"]
    auto_resolve = False  # True: open findings this run did not re-raise are marked resolved ("condition cleared")
    PARAMS = {}           # tunable thresholds: {name: {default, min, max, label, quieter: "up"|"down", step, rules: [fingerprint prefixes]}}
    SKILLS = {}           # what this agent can do ON REQUEST for a team project: {skill: {about, args: {name: meaning}, active?: bool, external?: bool}}
    # A skill is `async def skill_<name>(self, ctx, args) -> dict`. It only READS. It returns plain facts computed by code:
    #   {"summary": "one line", "facts": ["..."], "followups": [{"agent","skill","args","why"}], "recommend": [{"title","steps","rollback","risk"}], "data": {...}}
    # `followups` is how an agent asks a colleague for help; `recommend` is how it proposes manual steps (with a rollback plan).

    async def run(self, ctx):                   # pragma: no cover - overridden
        raise NotImplementedError

    async def skill_status(self, ctx, args):
        """Every agent can say what it currently sees: its last run and what it has open."""
        rt = ctx.runner
        st = rt.state.get(self.name, {})
        fs = rt.store.list_findings(self.name, "new", None, 12) + rt.store.list_findings(self.name, "seen", None, 12)
        return {"summary": f"{self.title}: {st.get('last_summary') or 'has not run yet'}; {len(fs)} open finding(s).",
                "facts": [f"{f['severity']}: {f['title']}" for f in fs[:8]] or ["nothing open"], "followups": [], "recommend": [], "data": {}}


class Context:
    def __init__(self, runner, agent, run_id):
        self.runner, self.agent, self.run_id = runner, agent, run_id
        self.created = 0
        self.summary = ""
        self.touched = set()
        self.trigger = "schedule"
        self.args = {}
        self.skipped = self.degraded = self.partial = False
        self.suppressed = 0
        self._checked = None

    @property
    def cfg(self):
        return self.runner.cfg.get()

    def working(self, kind="reading", detail=""):
        """Tell the Office what the agent is doing: reading | thinking | typing | scanning."""
        self.runner.bus.publish("agent.working", agent=self.agent.name, kind=kind, detail=detail[:120])

    def note(self, summary: str):
        self.summary = summary

    def skip(self, reason: str):
        """Nothing to do this time (not due, nothing to read): findings stay exactly as they are."""
        self.summary, self.skipped = reason, True

    def no_data(self, reason: str):
        """The agent could not see what it watches. Not a recovery: nothing is cleared, and the run is recorded as degraded."""
        self.summary, self.degraded = reason, True

    def checked(self, n):
        """How many things this run examined (flows, disks, guests...). The Watchdog notices an agent that 'succeeds' while examining nothing."""
        self._checked = (self._checked or 0) + int(n)

    def param(self, name):
        spec = PARAM_SPECS.get(self.agent.name, {}).get(name)
        v = self.cfg["agents"].get(self.agent.name, {}).get("params", {}).get(name)
        return v if v is not None else (spec["default"] if spec else None)

    def finding(self, fingerprint, severity, title, summary="", evidence=None, proposed_action=None, confidence=0.5, ttl_s=None):
        """Record a finding. `evidence` = [{"type": "log"|"metric"|"file"|..., "ref": "where it came from", "excerpt": "..."}].
        Held back (state 'maintenance') while the owner has a maintenance window open for what it is about."""
        if not evidence:
            raise ValueError("a finding without evidence is dropped")
        c = self.cfg
        if proposed_action:
            if proposed_action.get("kind", "manual") != "manual":
                raise ValueError("agents can only propose 'manual' actions: steps the owner carries out (nothing in this system executes changes)")
            if not proposed_action.get("rollback"):
                raise ValueError("a proposed action must carry a rollback plan")
            if proposed_action.get("execute") is not None:               # optional: the exact named action the team could carry out after the owner's approval
                from .actionspec import check_execute
                proposed_action = {**proposed_action, "execute": check_execute(proposed_action["execute"])}
        self.touched.add(fingerprint)
        store = self.runner.store
        w = maintenance.match(store, self.agent.name, fingerprint, title, summary)
        if w:
            self.suppressed += 1
            store.maint_hit(w["id"])
            seen = store.kv_get(f"maint:{w['id']}:seen", [])
            if fingerprint not in seen:
                store.kv_set(f"maint:{w['id']}:seen", (seen + [fingerprint])[-100:])
                store.audit(f"agent:{self.agent.name}", "finding.held_back", f"window#{w['id']}", f"{title[:100]} ({w['note'][:60]})")
            return None, "maintenance"
        f, state = store.upsert_finding(
            self.agent.name, fingerprint, severity, title, summary, evidence, proposed_action, confidence,
            evidence_ttl_s=c["retention_hours"] * 3600, ttl_s=ttl_s, suppress_days=c["suppress_rejected_days"], run_id=self.run_id)
        if state == "created":
            self.created += 1
            self.runner.bus.publish("finding.created", agent=self.agent.name, id=f["id"], severity=severity, title=title)
        elif state == "updated":
            self.runner.bus.publish("finding.updated", agent=self.agent.name, id=f["id"], severity=f["severity"], count=f["count"])
        return f, state

    def signal(self, entity, kind, strength, text):
        """Post a weak observation on the shared blackboard (below a finding's threshold). `entity` is 'host:<ip>' or 'net:all'. The Correlator reads these."""
        store = self.runner.store
        if maintenance.match(store, self.agent.name, f"signal:{kind}:{entity}", text, entity):
            return
        store.signal_add(self.agent.name, entity, kind, strength, text, self.run_id)

    def track(self, key, value, min_gap_s=3600, keep=200):
        """Remember a value over time and say how it has moved: returns {'v24': value ~24 h ago | None, 'delta24': change since then | None, 'age_h': hours of history}.
        Lets an agent speak about transitions ('jumped from 60 to 80 overnight') instead of only thresholds. Infrastructure numbers only."""
        store, now = self.runner.store, time.time()
        k = f"track:{self.agent.name}:{key}"
        ser = store.kv_get(k, [])
        if not ser or now - ser[-1][0] >= min_gap_s:
            ser = (ser + [[now, value]])[-keep:]
            store.kv_set(k, ser)
        old = [p for p in ser if p[0] <= now - 23 * 3600]
        ref = old[-1] if old else None
        return {"v24": ref[1] if ref else None, "delta24": (value - ref[1]) if ref else None, "age_h": (now - ser[0][0]) / 3600}

    def kv_get(self, key, default=None):
        return self.runner.store.kv_get(f"{self.agent.name}:{key}", default)

    def trace_note(self, kind, ref, detail="", ms=0):
        trace.record(kind, ref, detail, ms)

    def kv_set(self, key, value):
        self.runner.store.kv_set(f"{self.agent.name}:{key}", value)

    async def llm(self, messages, **kw):
        kw.setdefault("priority", AGENT)
        return await self.runner.gateway.chat(self.agent.name, messages, **kw)


class Runner:
    def __init__(self, cfg, store, bus, gateway):
        self.cfg, self.store, self.bus, self.gateway = cfg, store, bus, gateway
        self.agents, self.state, self.tasks = {}, {}, {}
        self.loop_task = None
        self.started = time.time()
        self.heartbeat = time.time()           # the scheduler loop stamps this every few seconds; the health feed reads its age
        self._quality = (0.0, {})

    def register(self, agent: Agent):
        self.agents[agent.name] = agent
        register_params(agent.name, agent.PARAMS)
        now = time.time()
        self.state[agent.name] = {"state": "idle", "last_run": None, "last_status": None, "last_summary": "",
                                  "next_run": now + 10 if agent.interval_s else None, "runs": 0}
        self.bus.publish("agent.registered", agent=agent.name, group=agent.group)

    def agent_cfg(self, name):
        return self.cfg.get()["agents"].get(name, {})

    def enabled(self, name):
        return self.agent_cfg(name).get("enabled", True)

    def interval(self, a):
        return self.agent_cfg(a.name).get("interval_s", a.interval_s)

    def _next_at(self, a):
        tz = ZoneInfo(self.cfg.get()["tz"]); now = datetime.now(tz)
        best = None
        for hm in a.at or []:
            h, m = map(int, hm.split(":"))
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now:
                t = (t + timedelta(days=1)).replace(hour=h, minute=m)   # same wall-clock time tomorrow (DST safe)
            best = t if best is None or t < best else best
        return best.timestamp() if best else None

    def trigger(self, name, how="manual", args=None):
        a = self.agents.get(name)
        if not a:
            raise KeyError(name)
        if name in self.tasks and not self.tasks[name].done():
            return False
        self.tasks[name] = asyncio.create_task(self._run(a, how, args or {}))
        return True

    async def _run(self, a, how, args=None):
        st = self.state[a.name]
        rid = self.store.run_start(a.name, how)
        trace.begin(self.store, rid)
        ctx = Context(self, a, rid)
        ctx.trigger, ctx.args = how, args or {}
        st.update(state="working")
        self.bus.publish("agent.started", agent=a.name, trigger=how)
        status, err = "ok", None
        try:
            await asyncio.wait_for(a.run(ctx), timeout=self.cfg.get()["gateway"]["timeout_s"] * 3)
        except asyncio.TimeoutError:
            status, err = "timeout", "run took too long"
        except GatewayError as e:
            status, err = "error", f"LLM: {e}"
        except Exception as e:
            status, err = "error", f"{type(e).__name__}: {e}"
            traceback.print_exc()
        if status == "ok" and ctx.degraded:
            status, err = "degraded", ctx.summary
        # 'condition cleared' is only believable when this run really looked: not when it was skipped, partial or blind
        if status == "ok" and getattr(a, "auto_resolve", False) and not (ctx.skipped or ctx.partial):
            for f in self.store.list_findings(a.name, limit=500):
                if f["status"] in ("new", "seen") and f["fingerprint"] not in ctx.touched:
                    self.store.set_status(f["id"], "resolved")
                    self.store.audit(f"agent:{a.name}", "finding.auto_resolved", f["id"], f"condition cleared: {f['title'][:100]}")
                    self.bus.publish("finding.status", agent=a.name, id=f["id"], status="resolved")
        self.store.run_end(rid, status, ctx.summary, ctx.created, err, ctx._checked)
        iv = self.interval(a)
        st.update(state="error" if status != "ok" else "idle", last_run=time.time(), last_status=status, last_summary=ctx.summary or (err or ""),
                  runs=st["runs"] + 1, next_run=(time.time() + iv) if iv else self._next_at(a))
        self.bus.publish("agent.finished" if status == "ok" else "agent.error", agent=a.name, status=status, findings=ctx.created, summary=(ctx.summary or err or "")[:160])
        self.store.audit(f"agent:{a.name}", "run", f"run#{rid}", f"{status}; {ctx.created} new findings; {how}" + (f"; {ctx.suppressed} held back by a maintenance window" if ctx.suppressed else ""))

    def skill_catalogue(self, only=None):
        """{agent: {skill: spec}} for every enabled agent: the menu the Commander plans from."""
        out = {}
        for a in self.agents.values():
            if a.name == "demo" or not self.enabled(a.name) or (only and a.name not in only):
                continue
            sk = {"status": {"about": "say what you currently see and what is open", "args": {}}, **a.SKILLS}
            out[a.name] = sk
        return out

    async def run_skill(self, name, skill, args, project=None, timeout=150):
        """One request from a team project: runs the agent's skill with its own run record, trace and (if it asks the model) its own budget.
        Returns the normalised result; raises on failure. The agent's scheduled state is not touched."""
        a = self.agents.get(name)
        fn = getattr(a, f"skill_{skill}", None) if a else None
        if not fn or skill not in {"status", *a.SKILLS}:
            raise KeyError(f"{name} has no skill '{skill}'")
        rid = self.store.run_start(name, f"project:{project or '-'}")
        trace.begin(self.store, rid)
        ctx = Context(self, a, rid)
        ctx.trigger, ctx.args = "project", args or {}
        self.bus.publish("agent.started", agent=name, trigger="project")
        status, err, res = "ok", None, None
        try:
            res = await asyncio.wait_for(fn(ctx, args or {}), timeout)
        except asyncio.TimeoutError:
            status, err = "timeout", "took too long"
        except Exception as e:
            status, err = "error", f"{type(e).__name__}: {e}"
        self.store.run_end(rid, status, (res or {}).get("summary", err or ""), 0, err, ctx._checked)
        self.bus.publish("agent.finished" if status == "ok" else "agent.error", agent=name, status=status, findings=0, summary=((res or {}).get("summary") or err or "")[:160])
        self.store.audit(f"agent:{name}", "skill", f"run#{rid}", f"{skill} for project {project or '-'}: {status}")
        if status != "ok":
            raise RuntimeError(err)
        return res, rid

    async def loop(self):
        for a in self.agents.values():
            if a.at and not a.interval_s:
                self.state[a.name]["next_run"] = self._next_at(a)
        last_purge = 0
        while True:
            now = time.time()
            self.heartbeat = now
            for a in self.agents.values():
                st = self.state[a.name]
                if st["next_run"] and now >= st["next_run"] and self.enabled(a.name) and not (a.name in self.tasks and not self.tasks[a.name].done()):
                    self.trigger(a.name, "schedule")
            if now - last_purge > 3600:
                r = self.store.purge(self.cfg.get()["retention_hours"] * 3600); last_purge = now
                if r["evidence_dropped"] or r["findings_expired"]:
                    self.store.audit("system", "retention.purge", "", str(r))
            await asyncio.sleep(5)

    def start(self):
        self.loop_task = asyncio.create_task(self.loop())

    def quality(self):
        if time.time() - self._quality[0] > 60:
            self._quality = (time.time(), self.store.agent_quality(time.time() - 30 * 86400))
        return self._quality[1]

    def describe(self):
        counts, qual = self.store.open_counts(), self.quality()
        return [{"name": a.name, "title": a.title, "group": a.group, "autonomy": a.autonomy, "enabled": self.enabled(a.name),
                 "interval_s": self.interval(a), "at": a.at, **self.state[a.name], "open_findings": counts.get(a.name, {}),
                 "quality": qual.get(a.name), "params": self.params_of(a.name)} for a in self.agents.values()]

    def params_of(self, name):
        specs = PARAM_SPECS.get(name)
        if not specs:
            return None
        cur = self.agent_cfg(name).get("params", {})
        return {k: {**sp, "value": cur.get(k, sp["default"])} for k, sp in specs.items()}
