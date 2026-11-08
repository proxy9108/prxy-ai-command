"""The break room: the crew talks to each other, in character, about what really happened.

Every line starts from a fact (an event on the bus, a finding, a run, the boss's last visit). The persona (crew.py) only adds a voice.
If `chat.voice` is on and the local model is already loaded, the line may be rephrased by the model; the rephrased line is dropped
unless it keeps every number and quoted title of the original and adds no new numbers. Nothing here changes anything in the lab.

Conversations:
  * a worker reports a new medium+ finding to its squad lead; the lead escalates high ones to the Commander
  * cleared findings, failed runs and recoveries are reported up the chain
  * the boss's decisions (done / rejected) are passed down by the Commander
  * Watchdog asks about findings nobody has decided on as their report count climbs
  * the crew wonders where the boss is after long absences, and greets them with a recap when they come back
  * squad leads check in with their workers now and then
  * a daily stand-up: each lead reports for its squad, the Commander names the one thing for the boss
  * the boss can post in the break room; mentioned agents answer with what they really see"""
import asyncio, re, time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from . import crew
from .crew import COMMAND, callsign, lead_of, line, squad_of
from .gateway import BACKGROUND
from .roster import ROSTER
from .team import numbers, targets_of
from .intent import classify, match_findings

BREAK = "breakroom"
TITLES = {r[0]: r[1] for r in ROSTER}
SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
OPEN = ("new", "seen")


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def human_span(s):
    h = s / 3600
    if h < 1.5:
        return plural(max(1, round(s / 60)), "minute")
    if h < 36:
        return plural(round(h), "hour")
    return plural(round(h / 24), "day")


def quoted(t):
    return set(re.findall(r"“([^”]{3,})”", t or ""))


def sev_counts(fs):
    c = {}
    for f in fs:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    order = ["critical", "high", "medium", "low", "info"]
    return ", ".join(f"{c[k]} {k}" for k in order if c.get(k)) or "nothing open"


def top_of(fs):
    """The finding that matters most: highest severity, not a standing (chronic) condition if possible, most reports."""
    if not fs:
        return None
    return sorted(fs, key=lambda f: (-SEV_RANK.get(f["severity"], 0), bool(f.get("chronic")), -f.get("count", 1)))[0]


class Crew:
    def __init__(self, runner):
        self.runner, self.store, self.bus, self.cfg = runner, runner.store, runner.bus, runner.cfg
        self.tasks = []
        self.gap = 2.5                       # seconds between lines of one conversation (tests set 0)
        self._voice_ts = deque()
        self._last_ok = {n: (s.get("last_status") in (None, "ok")) for n, s in runner.state.items()}
        self._cleared = {}                   # agent -> [titles]: resolved findings are batched for a few seconds
        self._flush = {}
        self._lock = asyncio.Lock()

    def policy(self):
        return self.cfg.get()["chat"]

    def tz(self):
        return ZoneInfo(self.cfg.get()["tz"])

    def when(self, ts):
        if not ts:
            return "never"
        d = datetime.fromtimestamp(ts, self.tz())
        now = datetime.now(self.tz())
        if d.date() == now.date():
            return d.strftime("today %H:%M")
        if (now.date() - d.date()).days == 1:
            return d.strftime("yesterday %H:%M")
        return d.strftime("%a %d %b %H:%M")

    def active(self, name):
        return name in self.runner.agents and self.runner.enabled(name)

    # ------------------------------------------------------------------ saying things
    def _once(self, key, every_s, now=None):
        """True (and remembered) if `key` was not used in the last `every_s` seconds."""
        now = now or time.time()
        k = f"chat:rl:{key}"
        if now - (self.store.kv_get(k, 0) or 0) < every_s:
            return False
        self.store.kv_set(k, now)
        return True

    async def voice(self, author, text):
        p = self.policy()
        if not p["voice"] or author not in crew.PERSONAS:
            return text
        now = time.time()
        while self._voice_ts and now - self._voice_ts[0] > 3600:
            self._voice_ts.popleft()
        if len(self._voice_ts) >= p["voice_per_hour"]:
            return text
        gw = self.runner.gateway
        model = gw.settings()["default_model"]
        try:
            loaded = await gw.loaded_models()
        except Exception:
            return text
        if not (gw.quiet_now() or model in loaded):          # never load a model just to chat
            return text
        self._voice_ts.append(now)
        sys = (f"You are {callsign(author)}, an AI agent on a homelab operations crew. Personality: {crew.personality(author)} "
               "Rewrite the line you are given in your own voice: one or two short sentences, at most 30 words. Keep every fact, name, number and “quoted” title exactly as "
               "it is. Do not add facts, numbers, advice or promises. Reply with the rewritten line only, no quotes.")
        try:
            r = await asyncio.wait_for(gw.chat(author, [{"role": "system", "content": sys}, {"role": "user", "content": text}], priority=BACKGROUND,
                                               options={"temperature": 0.8, "num_predict": 80}), 45)
            out = " ".join((r.get("text") or "").split()).strip().strip('"').strip()
        except Exception:
            return text
        if not out or len(out) > 260 or len(out) < 3:
            return text
        if numbers(out) != numbers(text) or any(q not in out for q in quoted(text)):
            return text                                      # it changed, added or dropped a fact
        return out

    async def post(self, author, text, to="", kind="say", thread=BREAK, ref="", data=None, voice=True):
        if not text:
            return None
        if voice and kind not in ("owner", "system"):
            text = await self.voice(author, text)
        mid = self.store.chat_add(thread, author, text, to, kind, ref, data)
        self.bus.publish("chat.message", agent=author, to=to, id=mid, thread=thread, kind=kind, text=text[:160])
        return mid

    async def convo(self, lines, thread=BREAK, ref=""):
        """[(author, to, text, kind), ...] posted in order with a short pause, like people taking turns."""
        async with self._lock:
            ids = []
            for i, ln in enumerate(lines):
                a, to, text, kind = ln[:4]
                if i and self.gap:
                    await asyncio.sleep(self.gap)
                ids.append(await self.post(a, text, to, kind, thread, ref, ln[4] if len(ln) > 4 else None, voice=len(ln) <= 4))
            return ids

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks = [x for x in self.tasks if not x.done()] + [t]
        return t

    # ------------------------------------------------------------------ reacting to events
    async def on_event(self, ev):
        if not self.policy()["enabled"]:
            return None
        t, a = ev.get("type"), ev.get("agent")
        if t == "finding.created" and a and squad_of(a):
            return await self.on_finding(ev)
        if t == "finding.status" and ev.get("quiet"):
            return None                                              # the boss did it from the break room: already answered there
        if t == "finding.status" and a and squad_of(a):
            if ev.get("status") == "resolved":
                return self.on_cleared(ev)
            if ev.get("status") in ("done", "rejected"):
                return await self.on_decision(ev)
        if t in ("agent.finished", "agent.error") and a and squad_of(a) and ev.get("status") != "skipped":
            return await self.on_run(ev)
        if t == "project.created":
            return await self.convo([(COMMAND, "", f"New job for the team: “{ev.get('title', 'a project')}”.", "say")], ref=f"project:{ev.get('project')}")
        if t == "project.plan" and ev.get("agents"):
            names = [n for n in ev["agents"] if n in crew.PERSONAS and n != COMMAND][:5]
            if names:
                first = names[0]
                return await self.convo([(COMMAND, first, f"{', '.join(callsign(n) for n in names)}: you're on it.", "say"),
                                         (first, COMMAND, line(first, "ack", ev.get("project", "")), "ack")], ref=f"project:{ev.get('project')}")
        if t == "project.done":
            p = self.store.project_get(ev.get("project")) if ev.get("project") else None
            title = (p or {}).get("title", "the project")
            txt = f"Job done: “{title}”. The summary is on the Team page." if ev.get("status") == "done" else f"Job “{title}” stopped ({ev.get('status')})."
            return await self.convo([(COMMAND, "", txt, "say")], ref=f"project:{ev.get('project')}")
        if t == "council.verdict" and self._once(f"verdict:{ev.get('host')}:{ev.get('verdict')}", 6 * 3600):
            host = ev.get("host", "a machine").replace("host:", "")
            return await self.convo([(COMMAND, "", f"Council verdict on {host}: {ev.get('verdict')}.", "say"),
                                     ("critic", COMMAND, line("critic", "ack", host), "say")], ref=f"incident:{ev.get('incident')}")
        tk = ev.get("ticket") or ""
        if t == "mission.learned":
            nxt = f" I've planned the next step as {ev.get('next_ticket')}; it's waiting for your approval." if ev.get("next_ticket") else " No further mission is needed from what we found."
            return await self.convo([(a if a in crew.ADVISORS else "claude", "owner", f"{tk} found out: {ev.get('conclusion') or str(ev.get('facts')) + ' facts filed'}." + nxt, "report", {"mission": ev.get("next") or ev.get("id")}),
                                     ("archivist", "", f"Filed {plural(ev.get('facts') or 0, 'fact')} from {tk} in the shared memory.", "say")])
        if t == "mission.replanning":
            return await self.convo([("claude", "owner", f"{tk}: attempt {int(ev.get('attempt') or 2) - 1} of “{ev.get('title')}” stopped, so I'm replanning it from the failure output. Atlas and Nova will review the new plan before it comes back to you.", "report", {"mission": ev.get("id")})])
        if t == "mission.planned" and ev.get("auto"):
            return None                                              # mission.started says it (on autopilot)
        if t == "mission.regressed":
            return await self.convo([(COMMAND, "owner", f"{tk} did not hold: “{ev.get('title')}” came back after the fix. I've opened {ev.get('next_ticket')} to find out why.", "escalate", {"mission": ev.get("next")})])
        if t == "mission.planned":
            if ev.get("status") == "ready":
                att = f" (attempt {ev.get('attempt')})" if (ev.get("attempt") or 1) > 1 else ""
                return await self.convo([("claude", "owner", f"{tk} ready for your approval{att}: “{ev.get('title')}”, {plural(ev.get('steps') or 0, 'step')}, each with a check and an undo. It's on the Approvals page.", "suggest", {"mission": ev.get("id")}),
                                         ("critic", "claude", "I've read it. My notes are on the card.", "say")])
            return await self.convo([("claude", "owner", f"{tk}: I couldn't produce a safe plan for “{ev.get('title')}” yet. The problems are on the Missions page; tell me what to change.", "report", {"mission": ev.get("id")})])
        if t == "mission.started":
            if ev.get("auto"):
                from .autopilot import Autopilot
                return await self.convo([(COMMAND, "owner", f"{tk} runs on autopilot: “{ev.get('title')}” ({plural(ev.get('steps') or 0, 'step')}). You allowed “{Autopilot.label(ev['auto'])}” without a click; "
                                          "everything it does is on the Autopilot page.", "report", {"mission": ev.get("id")})])
            return await self.convo([(COMMAND, "", f"{tk} approved: “{ev.get('title')}”. The team is carrying it out now ({plural(ev.get('steps') or 0, 'step')}). Restore points first.", "say", {"mission": ev.get("id")})])
        if t == "mission.waiting_for_you":
            parts = ev.get("parts") or []
            return await self.convo([(COMMAND, "owner", f"{tk}: the team's steps of “{ev.get('title')}” are done and checked. Now it's your part: " + " | ".join(p[:160] for p in parts[:3])
                                      + ". Press “I did it” on the Missions page or in What's next; the team checks it and then closes the ticket.", "suggest", {"mission": ev.get("id")})])
        if t == "mission.owner_check_failed":
            return await self.convo([(COMMAND, "owner", f"{tk}: I checked your part and it isn't there yet: {(ev.get('output') or 'no output')[:200]}. Still to do: "
                                      + " | ".join(p[:140] for p in (ev.get("parts") or [])[:3]) + ".", "report", {"mission": ev.get("id")})])
        if t == "mission.finished":
            st8 = ev.get("status")
            if st8 == "done":
                v = ev.get("verify") or {}
                lines = [(COMMAND, "owner", (f"{tk} complete: “{ev.get('title')}”. Your part checked out too; ticket closed." if ev.get("owner") else
                                             f"{tk} complete: “{ev.get('title')}”. Every step passed its check."), "report", {"mission": ev.get("id")})]
                if v.get("note"):
                    lines.append((v.get("advisor") or "openai", "owner", ("Verified: " if v.get("verdict") == "ok" else "One concern: ") + v["note"], "review"))
                return await self.convo(lines)
            txt = {"refused": f"{tk}: the runner refused “{ev.get('title')}”: {ev.get('error')}. Nothing was changed.",
                   "rolled_back": f"{tk} stopped: {ev.get('error')}. The steps that had run were undone.",
                   "rollback_failed": f"{tk} stopped and an undo step FAILED: {ev.get('error')}. Restore points exist: please look now."}.get(st8, f"{tk} failed: {ev.get('error')}.")
            return await self.convo([(COMMAND, "owner", txt, "escalate" if st8 == "rollback_failed" else "report", {"mission": ev.get("id")})])
        if t == "research.asked":
            return await self.convo([("research_librarian", "owner", f"On it: “{ev.get('question')}”. Searching and reading now.", "say")])
        if t == "research.done":
            if ev.get("status") == "done":
                who = ev.get("by")
                txt = f"Done: “{ev.get('question')}”. The answer is on the Research page" + (f", checked and improved by {callsign(who)}." if who in crew.ADVISORS else ".")
            else:
                txt = f"I couldn't write a sourced answer for “{ev.get('question')}”. The sources I found are on the Research page."
            return await self.convo([("research_librarian", "owner", txt, "report")])
        if t == "project.disputed" and a in crew.ADVISORS:
            return await self.convo([(a, COMMAND, f"I disagree with the crew's summary of “{ev.get('title')}”. My view is at the top of the project page.", "review"),
                                     ("critic", a, line("critic", "ack", str(ev.get("project"))), "say")], ref=f"project:{ev.get('project')}")
        if t == "advisor.review" and a in crew.ADVISORS and self._once(f"advrev:{a}:{ev.get('id')}", 24 * 3600):
            word = {"agree": "no objection", "caution": "go carefully", "disagree": "wait"}.get(ev.get("verdict"), ev.get("verdict"))
            txt = f"On “{ev.get('title')}”: {word}." + (f" {ev['concern']}" if ev.get("concern") else "")
            return await self.convo([(a, "critic", txt, "review")], ref=f"finding:{ev.get('id')}")
        if t == "advisor.coach" and a in crew.ADVISORS:
            ps = ev.get("projects") or []
            lines = [(a, "", (ev.get("note") or "Coaching notes for today.") + (" I've asked the crew to look into: " + "; ".join(f"“{p['goal']}”" for p in ps) + "." if ps else " No new investigations today."), "coach")]
            if ps:
                lines.append((COMMAND, a, line(COMMAND, "ack", str(ps[0]["id"])) + " Leads, the plan is on the Projects page.", "ack"))
            return await self.convo(lines)
        if t == "team.repeat":
            return await self.convo([(COMMAND, "", f"We already investigated “{ev.get('title')}” ({ev.get('when')}). Nothing has changed since, so another project won't help: it needs the boss's decision.", "escalate"),
                                     ("critic", COMMAND, line("critic", "nudge", ev.get("title", "")), "say")])
        return None

    async def on_finding(self, ev):
        sev, a = ev.get("severity"), ev["agent"]
        if SEV_RANK.get(sev, 0) < SEV_RANK["medium"]:
            return None
        f = self.store.get(ev["id"]) if ev.get("id") else None
        if not f or not self._once(f"found:{a}:{f['fingerprint']}", 12 * 3600):
            return None
        lead, what = lead_of(a), f"new {sev}: “{f['title']}”."
        lines = [(a, lead, line(a, "report", f["id"], what=what), "report")]
        high = SEV_RANK.get(sev, 0) >= SEV_RANK["high"]
        if lead and lead != COMMAND:
            lines.append((lead, a, line(lead, "ack", f["id"]), "ack"))
            if high:
                lines.append((lead, COMMAND, line(lead, "escalate", f["id"], what=f"“{f['title']}” ({sev}, from {callsign(a)})."), "escalate"))
                lines.append((COMMAND, lead, line(COMMAND, "escalated", f["id"]), "ack"))
        elif lead == COMMAND:
            lines.append((COMMAND, a, line(COMMAND, "escalated" if high else "ack", f["id"]), "ack"))
        return await self.convo(lines, ref=f"finding:{f['id']}")

    def on_cleared(self, ev):
        """Batch resolved findings per agent for a few seconds (Config Drift can clear many at once)."""
        a = ev["agent"]
        f = self.store.get(ev["id"]) if ev.get("id") else None
        if not f or SEV_RANK.get(f["severity"], 0) < SEV_RANK["low"]:
            return None
        self._cleared.setdefault(a, []).append(f)
        if a not in self._flush:
            self._flush[a] = self.spawn(self._flush_cleared(a))
        return self._flush[a]

    async def _flush_cleared(self, a, wait=8):
        await asyncio.sleep(wait if self.gap else 0)
        fs = self._cleared.pop(a, [])
        self._flush.pop(a, None)
        if not fs:
            return None
        fs = sorted(fs, key=lambda f: -SEV_RANK.get(f["severity"], 0))
        names = "; ".join(f"“{f['title']}”" for f in fs[:3]) + (f" and {len(fs) - 3} more" if len(fs) > 3 else "")
        lead = lead_of(a) or COMMAND
        lines = [(a, lead, f"Cleared: {names}.", "report"), (lead, a, line(lead, "fixed", fs[0]["id"]), "ack")]
        if SEV_RANK.get(fs[0]["severity"], 0) >= SEV_RANK["high"]:
            lines.append(("critic", a, line("critic", "fixed", fs[0]["id"]), "say"))
        return await self.convo(lines, ref=f"finding:{fs[0]['id']}")

    async def on_decision(self, ev):
        f = self.store.get(ev["id"]) if ev.get("id") else None
        if not f:
            return None
        a = ev["agent"]
        how = "dealt with" if ev["status"] == "done" else "not a problem (it stays quiet for 30 days)"
        return await self.convo([(COMMAND, a, f"The boss marked “{f['title']}” as {how}.", "say"),
                                 (a, COMMAND, line(a, "fixed" if ev["status"] == "done" else "ack", f["id"]), "ack")], ref=f"finding:{f['id']}")

    async def on_run(self, ev):
        a = ev["agent"]
        ok = ev.get("status") == "ok"
        was = self._last_ok.get(a, True)
        self._last_ok[a] = ok
        lead = lead_of(a) or COMMAND
        if not ok and was and self._once(f"runfail:{a}", 6 * 3600):
            why = (ev.get("summary") or ev.get("status") or "error")[:140].rstrip(".")
            return await self.convo([(a, lead, line(a, "report", f"fail:{why}", what=f"My last run didn't finish ({ev.get('status')}): {why}."), "report"),
                                     (lead, a, line(lead, "ack", why), "ack")], ref=f"agent:{a}")
        if ok and not was:
            what = (ev.get("summary") or "run ok")[:140].rstrip(".")
            return await self.convo([(a, lead, f"Back to normal: {what}.", "report"), (lead, a, line(lead, "fixed", what), "ack")], ref=f"agent:{a}")
        return None

    # ------------------------------------------------------------------ things that happen on a clock
    async def tick(self, now=None):
        p = self.policy()
        if not p["enabled"]:
            return
        now = now or time.time()
        if not self.store.kv_get("chat:intro"):
            await self.roll_call(now)
        if self._once("purge", 3600, now):
            self.store.chat_purge(p["keep_days"] * 86400)
        await self.maybe_standup(now)
        await self.remind_parts(now)
        await self.nudges(now)
        await self.wonder(now)
        await self.checkin(now)

    async def maybe_standup(self, now):
        at = self.policy()["standup_at"]
        if not at:
            return None
        d = datetime.fromtimestamp(now, self.tz())
        h, m = map(int, at.split(":"))
        if (d.hour, d.minute) < (h, m) or (d.hour, d.minute) > (h + 3, m):           # only in the three hours after the slot (no catch-up at night)
            return None
        if self.store.kv_get("chat:standup:last") == d.strftime("%Y-%m-%d"):
            return None
        return await self.standup(now)

    async def remind_parts(self, now):
        """Once a day (not in quiet hours) the Chief reminds the boss of mission parts only they can do."""
        ms = getattr(self, "missions", None)
        if not ms or self.runner.gateway.quiet_now():
            return None
        waiting = [(m, ms.owner_pending(m)) for m in self.store.mission_list(100) if m["status"] == "waiting_for_you"]
        waiting = [(m, p) for m, p in waiting if p and now - m["updated"] > 6 * 3600]
        if not waiting or not self._once("owner-parts", 86400, now):
            return None
        txt = "Reminder: " + " ".join(f"{m['ticket']} waits for your part: {p[0]['what'][:140]}." for m, p in waiting[:3])
        return await self.convo([(COMMAND, "owner", txt, "nudge", {"mission": waiting[0][0]["id"]})])

    async def nudges(self, now):
        """Watchdog asks about a finding the boss has not decided on, each time its report count crosses a threshold (one finding per 15 min)."""
        if not self._once("nudge-gap", 15 * 60, now):
            return None
        th = sorted(self.policy()["nudge_counts"])
        best = None
        for f in self.store.open_findings(500):
            if f["status"] not in OPEN or not squad_of(f["agent"]) or f["agent"] == "watchdog":
                continue
            crossed = [x for x in th if f["count"] >= x]
            if not crossed or self.store.kv_get(f"chat:nudge:{f['id']}", 0) >= crossed[-1]:
                continue
            key = (SEV_RANK.get(f["severity"], 0), f["count"])
            if best is None or key > best[0]:
                best = (key, f, crossed[-1])
        if not best:
            self.store.kv_set("chat:rl:nudge-gap", 0)       # nothing to say: try again next tick
            return None
        _, f, mark = best
        self.store.kv_set(f"chat:nudge:{f['id']}", mark)
        a = f["agent"]
        lines = [("watchdog", a, line("watchdog", "nudge", f"{f['id']}:{mark}", agent_call=callsign(a), title=f["title"], count=f["count"]), "nudge"),
                 (a, "watchdog", line(a, "worry", f"{f['id']}:{mark}"), "say")]
        if SEV_RANK.get(f["severity"], 0) >= SEV_RANK["medium"]:
            lines.append(("archivist", "", line("archivist", "nudge", f["id"], since=self.when(f["ts"])), "say"))
            lines.append((COMMAND, "", line(COMMAND, "nudge", f"{f['id']}:{mark}"), "say"))
        return await self.convo(lines, ref=f"finding:{f['id']}")

    def owner_last(self):
        return self.store.kv_get("owner:last_seen")

    def waiting(self):
        """What is waiting for the boss: proposals nobody decided on, and open high/critical findings."""
        props = self.store.pending_proposals()
        highs = [f for f in self.store.open_findings(500) if f["status"] in OPEN and SEV_RANK.get(f["severity"], 0) >= SEV_RANK["high"]]
        return props, highs

    async def wonder(self, now):
        """The crew notices the boss has been away (once per step of `chat.away_hours` per absence)."""
        last = self.owner_last()
        if not last:
            return None
        away = now - last
        steps = [h for h in sorted(self.policy()["away_hours"]) if away >= h * 3600]
        if not steps:
            return None
        k = f"chat:away:{int(last)}"
        if (self.store.kv_get(k, 0) or 0) >= steps[-1]:
            return None
        self.store.kv_set(k, steps[-1])
        props, highs = self.waiting()
        seed = f"{int(last)}:{steps[-1]}"
        leads = [s["lead"] for s in crew.SQUADS.values()]
        lead = leads[len(steps) % len(leads)]
        waiting = plural(len(props), "proposal") + " and " + plural(len(highs), "high finding") + " are waiting for them."
        return await self.convo([
            ("watchdog", "", line("watchdog", "absent", seed, away=human_span(away)), "absent"),
            (lead, "watchdog", line(lead, "absent", seed, last_seen=self.when(last)), "absent"),
            ("archivist", "", line("archivist", "absent", seed, last_seen=self.when(last)), "absent"),
            (COMMAND, "", line(COMMAND, "absent", seed) + " " + waiting, "absent"),
        ])

    async def checkin(self, now):
        mins = self.policy()["checkin_min"]
        if not mins or self.runner.gateway.quiet_now() or not self._once("checkin", mins * 60, now):
            return None
        keys = list(crew.SQUADS)
        i = (self.store.kv_get("chat:checkin:i", 0) or 0) % len(keys)
        self.store.kv_set("chat:checkin:i", i + 1)
        sq = crew.SQUADS[keys[i]]
        cands = [m for m in sq["members"] if self.active(m) and self.runner.state.get(m, {}).get("last_run")]
        if not cands or not self.active(sq["lead"]):
            return None
        w = cands[int(now // 60) % len(cands)]
        st = self.runner.state[w]
        what = (st.get("last_summary") or "nothing to report").rstrip(".")[:160]
        n = sum((self.runner.store.open_counts().get(w) or {}).values())
        tail = f" {plural(n, 'thing')} open." if n else " Nothing open."
        return await self.convo([(sq["lead"], w, line(sq["lead"], "ask", f"{w}:{int(now)}"), "ask"),
                                 (w, sq["lead"], line(w, "status", f"{w}:{int(now)}", what=what + "." + tail), "report"),
                                 (sq["lead"], w, line(sq["lead"], "ack", f"{w}:{int(now)}"), "ack")])

    async def roll_call(self, now=None):
        """Once, the first time the crew starts: the Chief explains the new structure and each lead introduces its squad (real sizes and counts)."""
        self.store.kv_set("chat:intro", now or time.time())
        lines = [(COMMAND, "", "Roll call. From today we work as one crew: three squads, each with a lead, and the Council staff with me. Leads, introduce your squads.", "say")]
        for key, sq in crew.SQUADS.items():
            r = self.squad_report(key, (now or time.time()) - 86400)
            n = 1 + len(sq["members"])
            lines.append((sq["lead"], COMMAND, line(sq["lead"], "status", "intro", what=f"{sq['name']}, {n} of us. {sq['about']} Right now: {sev_counts(r['open'])}."), "say"))
        lines.append(("critic", "", "Same rules as before: we read, we report, we propose. Nothing changes in the lab without the boss.", "say"))
        lines.append(("watchdog", "", "And I'll be counting who shows up for work.", "say"))
        return await self.convo(lines)

    # ------------------------------------------------------------------ stand-up
    def squad_report(self, key, since):
        sq = crew.SQUADS[key]
        names = [sq["lead"], *sq["members"]]
        allf = [f for f in self.store.open_findings(500) if f["agent"] in names]
        openf = [f for f in allf if f["status"] in OPEN]
        new = [f for f in self.store.q("SELECT * FROM findings WHERE ts >= ? AND agent IN (%s)" % ",".join("?" * len(names)), (since, *names))]
        cleared = self.store.q("SELECT count(*) FROM findings WHERE decided_at >= ? AND status IN ('resolved','done') AND agent IN (%s)" % ",".join("?" * len(names)), (since, *names))[0][0]
        new = [self.store._f(r) for r in new]
        return {"open": openf, "new": new, "cleared": cleared, "top": top_of(openf)}

    async def standup(self, now=None, trigger="schedule"):
        now = now or time.time()
        d = datetime.fromtimestamp(now, self.tz())
        date = d.strftime("%Y-%m-%d")
        thread = f"standup:{date}"
        self.store.kv_set("chat:standup:last", date)
        since = now - 86400
        lines = [(COMMAND, "", line(COMMAND, "open", date), "standup")]
        overall = []
        for key, sq in crew.SQUADS.items():
            r = self.squad_report(key, since)
            lead = sq["lead"]
            top = r["top"]
            what = (f"{sq['name']}: {sev_counts(r['open'])}. {len(r['new'])} new and {r['cleared']} cleared since yesterday."
                    + (f" Top: “{top['title']}” ({top['severity']})." if top else ""))
            lines.append((lead, COMMAND, line(lead, "report", f"standup:{date}", what=what), "standup"))
            for f in sorted([f for f in r["new"] if SEV_RANK.get(f["severity"], 0) >= SEV_RANK["medium"] and f["agent"] != lead], key=lambda f: -SEV_RANK[f["severity"]])[:2]:
                lines.append((f["agent"], lead, line(f["agent"], "report", f"standup:{f['id']}", what=f"new {f['severity']}: “{f['title']}”."), "standup"))
            overall += r["open"]
        bad = [n for n, s in self.runner.state.items() if squad_of(n) and self.active(n) and s.get("last_status") not in (None, "ok")]
        lines.append(("watchdog", COMMAND, ("Every agent's last run was fine." if not bad else f"Trouble on the last run: {', '.join(callsign(n) for n in bad[:5])}."), "standup"))
        props = self.store.pending_proposals()
        disputed = [f for f in props if (f.get("critic") or {}).get("verdict") == "disagree"]
        if props:
            lines.append(("critic", COMMAND, f"{plural(len(props), 'proposal')} waiting for the boss" + (f"; I disagree with {len(disputed)} of them." if disputed else ", none I object to."), "standup"))
        top = top_of([f for f in overall if not f.get("chronic")] or overall)
        nx = [x for x in (self.next.items() if getattr(self, "next", None) else []) if x["who"] == "you"]
        if nx:
            lines.append((COMMAND, "", f"Next for the boss: {nx[0]['title']}." + (f" Then: {nx[1]['title']}." if len(nx) > 1 else "") + " The full list is under What's next on Home. That's all. Back to work.", "standup"))
        else:
            lines.append((COMMAND, "", (f"One thing for the boss: “{top['title']}” ({top['severity']}, {callsign(top['agent'])})." if top else "Nothing needs the boss today.") + " That's all. Back to work.", "standup"))
        await self.convo(lines, thread=thread)
        await self.post(COMMAND, "Stand-up is done. The notes are on Home.", kind="say", ref=thread, voice=False)
        self.store.audit("agent:commander", "chat.standup", thread, f"{len(lines)} lines; {trigger}")
        return thread

    # ------------------------------------------------------------------ the boss
    def touch_owner(self, now=None):
        """Called when the boss opens the dashboard. Returns the recap if they were away long enough to deserve one."""
        now = now or time.time()
        last = self.owner_last()
        if last and now - last < 60:
            return None
        self.store.kv_set("owner:last_seen", now)
        if not last or now - last < self.policy()["recap_after_h"] * 3600:
            return None
        rc = self.recap(last, now)
        self.store.kv_set("recap:current", rc)
        if self.policy()["enabled"]:
            props, highs = self.waiting()
            top = rc.get("top")
            lines = [(COMMAND, "", f"{line(COMMAND, 'greet', int(now))} You were away {human_span(now - last)}. {rc['headline']}", "greet")]
            if top:
                lines.append((top["agent"], "", line(top["agent"], "report", f"greet:{int(now)}", what=f"The one for you: “{top['title']}” ({top['severity']})."), "greet"))
            if props:
                lines.append(("critic", "", f"{plural(len(props), 'proposal')} on the Approvals page. Read the rollback plans first.", "greet"))
            self.spawn(self.convo(lines))
        return rc

    def recap(self, since, now=None):
        now = now or time.time()
        st = self.store
        runs = st.q("SELECT count(*), sum(status != 'ok') FROM runs WHERE started >= ?", (since,))[0]
        new = [st._f(r) for r in st.q("SELECT * FROM findings WHERE ts >= ? ORDER BY ts DESC", (since,))]
        cleared = st.q("SELECT count(*) FROM findings WHERE decided_at >= ? AND status = 'resolved'", (since,))[0][0]
        projects = st.q("SELECT count(*) FROM projects WHERE created >= ?", (since,))[0][0]
        standups = st.q("SELECT count(DISTINCT thread) FROM chat WHERE ts >= ? AND thread LIKE 'standup:%'", (since,))[0][0]
        changes = st.q("SELECT count(*) FROM actions WHERE created >= ?", (since,))[0][0]
        props, highs = self.waiting()
        top = top_of([f for f in highs + props if not f.get("chronic")] or highs + props)
        highlights = [m for m in st.chat_list(BREAK, since=since, limit=400) if m["kind"] in ("escalate", "report", "nudge")][-8:]
        newc = sev_counts(new) if new else "no new findings"
        headline = (f"{plural(runs[0] or 0, 'run')}, {newc.replace('nothing open', 'no new findings')}, {cleared} cleared. "
                    + ("No changes were made to the lab." if not changes else f"{plural(changes, 'approved action')} ran."))
        return {"since": since, "until": now, "away_s": now - since, "away": human_span(now - since), "headline": headline,
                "runs": runs[0] or 0, "failed_runs": runs[1] or 0, "new_findings": len(new), "new_by_severity": sev_counts(new) if new else "",
                "cleared": cleared, "projects": projects, "standups": standups, "changes": changes,
                "waiting": {"proposals": len(props), "high": len(highs)},
                "top": ({k: top[k] for k in ("id", "agent", "title", "severity", "count")} | {"callsign": callsign(top["agent"])}) if top else None,
                "highlights": [{"id": m["id"], "author": m["author"], "callsign": callsign(m["author"]), "text": m["text"], "ts": m["ts"]} for m in highlights],
                "dismissed": False}

    def overview(self):
        """The Chief's short answer to 'update' / 'status': what the team is doing, what waits for the boss, what's next."""
        ms = self.store.mission_list(60)
        running = [m for m in ms if m["status"] in ("running", "approved", "checking_your_part", "planning")]
        auto = [m for m in ms if m.get("auto_rule") and time.time() - (m.get("approved_at") or 0) < 86400]
        parts = []
        if running:
            parts.append("Working on: " + "; ".join(f"{m['ticket']} “{m['title'][:70]}” ({m['status'].replace('_', ' ')})" for m in running[:3]) + ".")
        if auto:
            parts.append(f"On autopilot in the last 24 h: {plural(len(auto), 'mission')}.")
        nx = [x for x in (self.next.items() if getattr(self, "next", None) else []) if x["who"] == "you"]
        if nx:
            parts.append("For you, most important first:\n" + "\n".join(f"{i}. {x['title'][:110]}" for i, x in enumerate(nx[:3], 1)) + "\n")
        else:
            parts.append("Nothing needs you right now.")
        fs = [f for f in self.store.open_findings(500) if f["status"] in OPEN]
        parts.append(f"Open findings: {sev_counts(fs)}. Reply to any line to ask about it.")
        return " ".join(parts)

    def status_line(self, name):
        if name == COMMAND:
            fs = [f for f in self.store.open_findings(500) if f["status"] in OPEN]
            top = top_of([f for f in fs if not f.get("chronic")] or fs)
            return f"Across the crew: {sev_counts(fs)}." + (f" Top: “{top['title']}” ({top['severity']}, {callsign(top['agent'])})." if top else "")
        if not self.active(name):
            return "I'm off duty right now (disabled)." if name in self.runner.agents else "I haven't been hired yet."
        st = self.runner.state.get(name, {})
        fs = [f for f in self.store.open_findings(500) if f["agent"] == name and f["status"] in OPEN]
        last = (st.get("last_summary") or "I haven't run yet").rstrip(".")[:160]
        return f"{last}. Last run {self.when(st.get('last_run'))}, {sev_counts(fs)}."

    def mentioned(self, text):
        t = text.lower()
        out = []
        for n in crew.everyone():
            title = TITLES.get(n, n).lower().replace("the ", "")
            keys = {callsign(n).lower(), n, n.replace("_", " "), title}
            if any(re.search(r"(?<![\w])@?" + re.escape(k) + r"(?![\w])", t) for k in keys if k):
                out.append(n)
        return out[:3]

    async def owner_says(self, text, reply_to=None):
        text = " ".join(str(text or "").split())[:400]
        if not text:
            raise ValueError("say something")
        rmsg = self.store.chat_get(int(reply_to)) if reply_to else None
        if rmsg and rmsg["thread"] != BREAK:
            rmsg = None
        partner = self.partner_for(rmsg)
        data = {"reply_to": rmsg["id"], "quote": {"callsign": callsign(rmsg["author"]) if rmsg["author"] != "owner" else "You", "text": rmsg["text"][:160]}} if rmsg else None
        mid = await self.post("owner", text, to=partner or "", kind="owner", voice=False, data=data)
        self.store.audit("owner", "chat.say", f"chat#{mid}", text[:120])
        if rmsg:
            self.spawn(self.understand(text, mid, reply=rmsg))
            return mid
        low = text.lower()
        tm = re.search(r"\bprx-?(\d{1,6})\b", low)
        if tm and len(low.split()) > 6 and not re.search(r"\b(status|progress|how far|where is|update on)\b", low):
            tm = None                                       # a real question about a ticket: answered in conversation, not with the status line
        if tm and getattr(self, "missions", None):
            mm = self.store.mission_get(int(tm.group(1)))
            if not mm:
                self.spawn(self.convo([(COMMAND, "owner", f"There is no ticket PRX-{int(tm.group(1)):04d}.", "reply")]))
                return mid
            steps = len((mm.get("plan") or {}).get("steps") or [])
            done = sum(s.get("state") == "ok" for s in (mm.get("run") or {}).get("steps", []))
            words = {"planning": "Sage is planning it", "ready": "waiting for your approval", "needs_revision": "needs changes before it can run", "running": f"running: {done} of {steps} steps done",
                     "done": "done, every step passed its check", "rolled_back": "stopped and undone", "rejected": "rejected", "approved": "approved, starting",
                     "waiting_for_you": "the team's steps are done; waiting for your part: " + " | ".join(o["what"][:120] for o in self.missions.owner_pending(mm)),
                     "checking_your_part": "the team is checking your part"}.get(mm["status"], mm["status"])
            last = (mm["history"] or [{}])[-1]
            self.spawn(self.convo([(COMMAND, "owner", f"{mm['ticket']} “{mm['title']}”: {words} (attempt {mm['attempt']})." + (f" Last attempt: {last.get('error')}." if last.get("error") and mm["status"] != "done" else ""), "reply", {"mission": mm["id"]})]))
            return mid
        if re.search(r"\b(what'?s next|what is next|next steps?|what (should|do|can|must) (i|we) (do|fix|tackle|start)|what now|to-?do|where (do|should) (i|we) start|"
                     r"(do|fix|tackle) first|first thing|priorit(y|ies|ise|ize)|most important|what needs (me|doing|attention))\b", low) and getattr(self, "next", None):
            ls = self.next.lines(3)
            self.spawn(self.convo([(COMMAND, "owner", ("Most important first:\n" + "\n".join(ls) + "\nThe buttons and reasons are under What's next on Home. Reply to me to talk about any of them.")
                                    if ls else "Nothing is waiting: no approvals, no open fixes, nothing for you to do.", "reply", {"convo": True})]))
            return mid
        if re.fullmatch(r"(any )?(update|updates|status|sitrep|report|hi|hello|hey|yo|morning|good morning|how are we|how'?s it going|what'?s up)[?!. ]*", low):
            self.spawn(self.convo([(COMMAND, "owner", self.overview(), "reply", {"convo": True})]))
            return mid
        if re.search(r"\bstand-?up\b", low):
            self.spawn(self.standup(trigger="owner"))
            return mid
        if re.search(r"\b(recap|while i was (gone|away)|what happened|what did i miss)\b", low):
            rc = self.recap(self.store.kv_get("chat:recap:asked") or time.time() - 86400)
            self.store.kv_set("chat:recap:asked", time.time())
            self.spawn(self.convo([(COMMAND, "owner", f"Since {self.when(rc['since'])}: {rc['headline']}" + (f" The one for you: “{rc['top']['title']}”." if rc["top"] else ""), "reply")]))
            return mid
        self.spawn(self.understand(text, mid))
        return mid

    # ------------------------------------------------------------------ understanding the boss
    async def _llm(self, messages):
        from .gateway import INTERACTIVE
        return await asyncio.wait_for(self.runner.gateway.chat("commander", messages, priority=INTERACTIVE, options={"num_predict": 80, "temperature": 0}, timeout=45), 50)

    def notes(self, days=14):
        return [n for n in (self.store.kv_get("owner:notes") or []) if time.time() - n["ts"] < days * 86400]

    def add_note(self, text, targets):
        hosts, guests, storages = targets
        notes = [n for n in (self.store.kv_get("owner:notes") or []) if time.time() - n["ts"] < 60 * 86400][-199:]
        note = {"id": int(time.time() * 1000), "ts": time.time(), "text": text[:400], "guests": [g for g, _ in guests], "hosts": list(hosts), "storages": list(storages)}
        self.store.kv_set("owner:notes", notes + [note])
        self.store.audit("owner", "note.add", ",".join(note["guests"] + note["hosts"])[:80], text[:150])
        return note

    async def understand(self, text, mid, reply=None):
        if reply is not None:
            # the owner answered a specific message: the agent who wrote it answers back, with what that message was about
            return await self.converse(self.partner_for(reply) or (self.mentioned(text) or [COMMAND])[0], text, mid, reply)
        it = await classify(text, self._llm)
        own = self.store.chat_get(mid) or {}
        self.store.chat_set_data(mid, {**(own.get("data") or {}), "intent": {k: it[k] for k in ("kind", "action", "by")}})
        targets = targets_of(text, self.store)
        who = self.mentioned(text)
        if not who and it["kind"] in ("question", "chat"):
            cont = self.continuing(mid)
            if cont:
                return await self.converse(cont, text, mid, None)
        if it["kind"] == "update":
            note = self.add_note(text, targets)
            fs = match_findings(text, self.store.open_findings(500), targets)
            lines = [("archivist", "owner", f"Noted: “{text[:200]}”. It's in the shared memory, so the crew and the advisors will know.", "reply")]
            if fs:
                top = max({f["agent"] for f in fs}, key=lambda a: sum(f["agent"] == a for f in fs))
                titles = "; ".join(f"“{f['title']}”" for f in fs[:3]) + (f" and {len(fs) - 3} more" if len(fs) > 3 else "")
                lines.append((top, "owner", f"That would explain {plural(len(fs), 'open finding')}: {titles}.", "report"))
                lines.append(("archivist", "owner", f"Mark {'it' if len(fs) == 1 else 'them'} as expected? Nothing changes until you press the button.", "suggest", 
                              {"suggest": {"status": "done", "finding_ids": [f["id"] for f in fs], "label": f"Yes, mark {plural(len(fs), 'finding')} as expected", "note": note["id"], "done": False}}))
            return await self.convo(lines)
        if it["kind"] == "instruction" and it["action"] == "research" and getattr(self, "desk", None):
            try:
                self.desk.ask(it["topic"])
            except ValueError as e:
                return await self.convo([("research_librarian", "owner", f"I need a bit more to go on: {e}.", "reply")])
            return None                                              # Shelf announces it (research.asked)
        if it["kind"] == "instruction" and it["action"] == "mission" and getattr(self, "missions", None):
            try:
                mid2 = self.missions.create(it["topic"])
            except ValueError as e:
                return await self.convo([(COMMAND, "owner", f"I need a clearer mission: {e}.", "reply")])
            return await self.convo([(COMMAND, "owner", "Understood. Sage is planning the mission with the crew's facts: goal, every step with its check and its undo. "
                                      "Nothing runs until you approve it on the Missions page.", "reply", {"mission": mid2})])
        if it["kind"] == "instruction" and it["action"] == "investigate" and getattr(self, "team", None):
            try:
                pid = self.team.create(it["topic"] if len(it["topic"]) > 12 else text)
            except ValueError as e:
                return await self.convo([(COMMAND, "owner", f"I need a clearer goal: {e}.", "reply")])
            return await self.convo([(COMMAND, "owner", "On it. I've opened a project and the crew is planning it now. Follow it on the Projects page.", "reply", {"project": pid})])
        if it["kind"] == "instruction" and it["action"] == "silence":
            fs = match_findings(it["topic"] or text, self.store.open_findings(500), targets_of(it["topic"] or text, self.store))
            if not fs:
                return await self.convo([(COMMAND, "owner", "I couldn't find an open warning that matches that. Name the machine or the warning's words and I'll look again.", "reply")])
            titles = "; ".join(f"“{f['title']}”" for f in fs[:3]) + (f" and {len(fs) - 3} more" if len(fs) > 3 else "")
            return await self.convo([(COMMAND, "owner", f"These match: {titles}. Silence {'it' if len(fs) == 1 else 'them'} for 30 days?", "suggest",
                                      {"suggest": {"status": "rejected", "finding_ids": [f["id"] for f in fs], "label": f"Yes, silence {plural(len(fs), 'finding')} for 30 days", "done": False}})])
        if not who and it["kind"] in ("question", "chat"):
            hosts, guests, storages = targets
            if hosts or guests or storages:
                fs = match_findings(text, self.store.open_findings(500), targets)
                about = ", ".join([g for g, _ in guests] + list(hosts) + list(storages))
                txt = (f"On {about}: {sev_counts(fs)}." + (" " + "; ".join(f"“{f['title']}” ({f['severity']}, {callsign(f['agent'])})" for f in fs[:4]) + "." if fs else " Nothing open."))
                return await self.convo([(COMMAND, "owner", txt, "reply")])
        if await self.can_talk():
            return await self.converse((who or [COMMAND])[0], text, mid, None)
        who = who or [COMMAND]
        lines = [(n, "owner", line(n, "status", f"owner:{mid}", what=self.status_line(n)) if n != COMMAND else self.overview(), "reply") for n in who]
        return await self.convo(lines)

    # ------------------------------------------------------------------ conversation: the boss talks with one agent, back and forth
    CONVO_WINDOW = 15 * 60            # a follow-up within this time goes to the same agent
    CLAUDE_CHAT_PER_DAY = 20          # Sage's calls are the planning budget: chat takes only a small share

    def partner_for(self, msg):
        if not msg:
            return None
        a = msg["author"]
        if a == "owner":
            return msg.get("to_agent") or None
        return a if (a in crew.PERSONAS or a in crew.ADVISORS) else None

    def continuing(self, mid):
        """The agent the boss was just talking with (it answered the boss's previous message a few minutes ago), or None."""
        recent = [m for m in self.store.chat_list(BREAK, limit=30) if m["id"] < mid][-12:]
        for m in reversed(recent):
            if time.time() - m["ts"] > self.CONVO_WINDOW:
                return None
            if m["author"] != "owner" and m.get("to_agent") == "owner" and m["kind"] == "reply" and ((m.get("data") or {}).get("convo")):
                return m["author"]
        return None

    async def can_talk(self):
        """True when someone can actually hold the conversation: an outside advisor is free, or the local model is already loaded (never load one just to chat)."""
        adv = self.talker()
        if adv and any(adv.why_not(p, "review" if p == "claude" else "chat") is None for p in crew.ADVISORS):
            return True
        return await self._local_ready()

    async def _local_ready(self):
        gw = self.runner.gateway
        if not getattr(gw, "task", None) or gw.task.done():          # the model queue is not running: don't wait in it
            return False
        try:
            return gw.settings()["default_model"] in await asyncio.wait_for(gw.loaded_models(), 5)
        except Exception:
            return False

    def talker(self):
        """The advisors object, if it can hold a conversation."""
        adv = getattr(self.runner, "frontier", None)
        return adv if adv is not None and hasattr(adv, "call") and hasattr(adv, "why_not") else None

    def _mission_facts(self, mm):
        p = mm.get("plan") or {}
        rv = mm.get("review") or {}
        steps = "; ".join(f"{s['n']}. {s['title']} [{s['target']}]: {s['run'][:140]}" for s in p.get("steps") or [])
        own = "; ".join(f"{o['when']}: {o['what'][:140]}" for o in p.get("owner_steps") or [])
        concerns = "; ".join(f"{x['advisor']}: {c}" for x in rv.get("panel") or [] for c in x.get("concerns") or [])
        res = mm.get("result") or {}
        learned = (res.get("learned") or {})
        return (f"Mission {mm['ticket']} “{mm['title']}”, status {mm['status']}, attempt {mm['attempt']}. Goal: {p.get('goal') or mm['goal']}. Why: {p.get('why', '')}. "
                f"Risk {p.get('risk', '?')}, downtime {p.get('downtime', '?')}. Steps: {steps or 'none yet'}. Owner's part: {own or 'none'}. Reviewer concerns: {concerns or 'none'}. "
                f"Problems: {'; '.join(rv.get('problems') or []) or 'none'}. Result: {res.get('error') or res.get('state') or 'not run'}. "
                f"Learned: {'; '.join(learned.get('facts') or []) or 'nothing yet'}")[:3000]

    def context_for(self, partner, text, mid, reply):
        bits = []
        mids = set()
        if reply and (reply.get("data") or {}).get("mission"):
            mids.add(int(reply["data"]["mission"]))
        for x in re.findall(r"\bprx-?(\d{1,6})\b", (text + " " + (reply or {}).get("text", "")).lower()):
            mids.add(int(x))
        for x in sorted(mids)[:2]:
            mm = self.store.mission_get(x)
            if mm:
                bits.append(self._mission_facts(mm))
        fids = []
        if reply and str(reply.get("ref") or "").startswith("finding:"):
            fids.append(reply["ref"].split(":", 1)[1])
        fids += ((reply or {}).get("data") or {}).get("suggest", {}).get("finding_ids", [])[:2] if reply else []
        for fid in fids[:3]:
            f = self.store.get(fid)
            if f:
                ev = "; ".join(e.get("excerpt", "")[:160] for e in (f.get("evidence") or [])[:3])
                bits.append(f"Finding “{f['title']}” ({f['severity']}, from {callsign(f['agent'])}, status {f['status']}, seen {f['count']}x): {f['summary'][:400]} Evidence: {ev}")
        if not bits:
            targets = targets_of(text, self.store)
            fs = match_findings(text, self.store.open_findings(500), targets)[:4]
            bits += [f"Open finding “{f['title']}” ({f['severity']}, {callsign(f['agent'])}): {f['summary'][:200]}" for f in fs]
        if partner in self.runner.agents or partner == COMMAND:
            bits.append(f"Your own status: {self.status_line(partner)}")
        nx = getattr(self, "next", None)
        if nx:
            bits.append("What's next for the owner and the team: " + " ".join(nx.lines(5)))
        ms = getattr(self, "missions", None)
        if ms:
            bits.append("What the team learned recently: " + " | ".join(k["text"] for k in ms.knowledge()[-8:]))
        hist = []
        for m in self.store.chat_list(BREAK, limit=60):
            if m["id"] >= mid or time.time() - m["ts"] > 2 * 3600:
                continue
            if m["author"] in ("owner", partner) or m.get("to_agent") in ("owner", partner):
                hist.append(f"{'Owner' if m['author'] == 'owner' else callsign(m['author'])}: {m['text'][:300]}")
        return "\n".join(bits)[:6000], hist[-10:]

    async def _talk(self, partner, system, user):
        """-> (text, who wrote it). Advisors speak for themselves; crew members are voiced by a cheap outside model, else the local model."""
        adv = self.talker()
        day = time.strftime("%Y-%m-%d")
        k = self.store.kv_get("chat:claude:day") or {}
        claude_ok = (k.get("d") != day or k.get("n", 0) < self.CLAUDE_CHAT_PER_DAY)
        order = [partner] if partner in crew.ADVISORS else []
        order += [x for x in ("gemini", "openai", "claude") if x not in order]
        if adv:
            for prov in order:
                if prov == "claude" and not claude_ok:
                    continue
                if adv.why_not(prov, "review" if prov == "claude" else "chat"):
                    continue
                try:
                    r = await adv.call(prov, "review" if prov == "claude" else "chat", system, user, 500)
                except Exception:
                    continue
                if prov == "claude":
                    self.store.kv_set("chat:claude:day", {"d": day, "n": (k.get("n", 0) if k.get("d") == day else 0) + 1})
                return r.get("text") or "", prov
        if not await self._local_ready():
            return "", None
        try:
            from .gateway import INTERACTIVE
            r = await asyncio.wait_for(self.runner.gateway.chat(partner if partner in self.runner.agents else "commander", [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                                                priority=INTERACTIVE, options={"num_predict": 220, "temperature": 0.3}, timeout=60), 60)
            return r.get("text") or "", "local"
        except Exception:
            return "", None

    async def converse(self, partner, text, mid, reply):
        partner = partner if (partner in crew.PERSONAS or partner in crew.ADVISORS) else COMMAND
        facts, hist = self.context_for(partner, text, mid, reply)
        title = TITLES.get(partner, partner)
        system = (f"You are {callsign(partner)} ({title}) on the owner's home-lab AI operations crew (Prxy-AI Command). Personality: {crew.personality(partner)} "
                  "You are talking with the owner in the team's break room. Answer the owner's message directly, like a colleague: plain words, 1 to 4 short sentences, no lists "
                  "unless asked. Use ONLY the facts given; if they don't answer it, say what you don't know and how the team could find out. You cannot change anything from the chat: "
                  "never say you did something. If the owner asks for a change, propose it. Reply with ONE JSON object: {\"reply\": \"what you say\", \"propose\": null "
                  "or {\"kind\": \"revise\", \"ticket\": \"PRX-n\", \"note\": \"what Sage should change in that mission's plan\"} (only for a mission that is waiting for approval or stopped) "
                  "or {\"kind\": \"mission\", \"goal\": \"a new mission in one or two sentences\"}}.")
        user = (f"Facts:\n{facts}\n\nConversation so far (oldest first):\n" + ("\n".join(hist) or "(none)")
                + (f"\n\nThe owner is replying to this message from {callsign(reply['author']) if reply['author'] != 'owner' else 'themselves'}: “{reply['text'][:400]}”" if reply else "")
                + f"\n\nThe owner says: {text}")
        raw, by = await self._talk(partner, system, user)
        from .frontier import extract_json
        j = extract_json(raw) if raw else None
        say = str((j or {}).get("reply") or "").strip() if isinstance(j, dict) else raw.strip().strip('"')
        if not say or len(say) < 2:
            say = (self.overview() if partner == COMMAND else self.status_line(partner) if partner in self.runner.agents
                   else "I can't answer that right now (no model is free). Ask again in a minute.")
            by = None
        data = {"convo": True, "reply_to": mid, "by": by}
        pr = (j or {}).get("propose") if isinstance(j, dict) else None
        ms = getattr(self, "missions", None)
        if isinstance(pr, dict) and ms:
            if pr.get("kind") == "revise":
                tmid = re.search(r"(\d{1,6})", str(pr.get("ticket") or ""))
                mm = self.store.mission_get(int(tmid.group(1))) if tmid else None
                note = str(pr.get("note") or text).strip()[:500]
                if mm and mm["status"] in ("ready", "needs_revision", "plan_failed", "rolled_back", "failed", "refused", "rollback_failed", "lost") and len(note) >= 5:
                    data["suggest"] = {"kind": "revise", "mission": mm["id"], "note": note, "label": f"Send {mm['ticket']} back to Sage with this note", "done": False}
                    data["mission"] = mm["id"]
            elif pr.get("kind") == "mission" and len(str(pr.get("goal") or "")) >= 15:
                data["suggest"] = {"kind": "mission", "goal": str(pr["goal"]).strip()[:600], "label": "Plan this as a mission", "done": False}
        if reply and (reply.get("data") or {}).get("mission") and "mission" not in data:
            data["mission"] = reply["data"]["mission"]
        return await self.convo([(partner, "owner", say[:590], "reply", data)])

    def accept(self, mid):
        """The boss pressed the button under a suggestion: apply the status to the listed findings. -> number changed."""
        m = self.store.chat_get(mid)
        sg = ((m or {}).get("data") or {}).get("suggest")
        if not sg or sg.get("done"):
            raise ValueError("nothing to accept (already done?)")
        if sg.get("kind") in ("revise", "mission"):
            ms = getattr(self, "missions", None)
            if not ms:
                raise ValueError("missions are not available")
            if sg["kind"] == "revise":
                ms.revise(sg["mission"], sg["note"])
                txt, nid = f"Sent back to Sage: “{sg['note'][:200]}”. The new plan comes back to you for approval.", sg["mission"]
            else:
                nid = ms.create(sg["goal"])
                txt = f"Planning it as PRX-{nid:04d}. Nothing runs until you approve it."
            sg["done"], sg["changed"] = True, 1
            self.store.chat_set_data(mid, {**(m.get("data") or {}), "suggest": sg})
            self.spawn(self.convo([(COMMAND, "owner", txt, "reply", {"mission": nid})]))
            return 1
        n = 0
        for fid in sg["finding_ids"]:
            f = self.store.get(fid)
            if f and f["status"] in ("new", "seen"):
                self.store.set_status(fid, sg["status"])
                self.store.audit("owner", f"finding.{sg['status']}", fid, f"from the break room: {f['title'][:100]}")
                self.bus.publish("finding.status", agent=f["agent"], id=fid, status=sg["status"], quiet=True)
                n += 1
        sg["done"], sg["changed"] = True, n
        self.store.chat_set_data(mid, {**(m.get("data") or {}), "suggest": sg})
        self.spawn(self.convo([("archivist", "owner", f"Done: {plural(n, 'finding')} marked {'as expected' if sg['status'] == 'done' else 'not a problem for 30 days'}.", "reply")]))
        return n

    # ------------------------------------------------------------------ running
    async def _listen(self, q):
        while True:
            ev = await q.get()
            if ev.get("type", "").startswith("chat."):
                continue
            self.spawn(self._safe(self.on_event(ev)))               # never block the bus queue: conversations pause between lines

    async def _safe(self, coro):
        try:
            return await coro
        except Exception as e:                                       # a chat problem must never disturb the agents
            print(f"chat: {type(e).__name__}: {e}", flush=True)

    async def run(self):
        q = self.bus.subscribe()
        listener = asyncio.create_task(self._listen(q))
        await asyncio.sleep(30)
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:
                    print(f"chat tick: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(60)
        finally:
            listener.cancel(); self.bus.unsubscribe(q)

    def start(self):
        self.main = asyncio.create_task(self.run())
        self.tasks.append(self.main)

    def running(self):
        return [t for t in self.tasks if not t.done()]
