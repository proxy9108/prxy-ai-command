"""What's next: the Chief's short, ranked list of the next concrete actions, each with WHO does it and a one-click button.

Sources (plain code, no model needed): missions waiting for approval, missions waiting for the owner's part, missions that stopped and need a
decision, missions the team is working on, the owner's to-do list (things a mission found only the owner can do), and open medium+ findings
that have no mission yet. Ranked by severity x age, with the owner's quick wins (one click) first. Sage (the mission leader) may rewrite the
one-line "why" of each item, at most a few times a day and only when the list changed; without Sage the plain-code reason is shown."""
import asyncio, hashlib, json, re, time

from .frontier import FrontierError, extract_json
from .redact import scrub

SEV = {"critical": 90, "high": 70, "medium": 45, "low": 15, "info": 5}
ENDED = ("rejected", "failed", "rolled_back", "refused", "plan_failed", "rollback_failed", "lost")
ACTIVE = ("planning", "ready", "needs_revision", "approved", "running", "waiting_for_you", "checking_your_part")
LIMIT = 7


def tk(mid):
    return f"PRX-{int(mid):04d}"


def _age_factor(ts, now):
    return 1 + min(max(0.0, (now - (ts or now)) / 86400), 14) / 14          # up to x2 after two weeks


class NextSteps:
    def __init__(self, store, missions=None, advisors=None, bus=None):
        self.store, self.missions, self.adv, self.bus = store, missions, advisors, bus
        self.task = None

    # ------------------------------------------------------------------ the list
    def items(self, now=None, limit=LIMIT):
        now = now or time.time()
        out = []
        ms = self.store.mission_list(200)
        newest = {}
        for m in ms:
            if m["ref"]:
                newest[m["ref"]] = max(newest.get(m["ref"], 0), m["id"])
        for m in ms:
            p, t = m.get("plan") or {}, tk(m["id"])
            st = m["status"]
            if st == "waiting_for_you" and self.missions:
                for o in self.missions.owner_pending(m):
                    out.append({"key": f"part:{m['id']}:{o['i']}", "who": "you", "kind": "your_part", "ticket": t, "mission": m["id"], "score": 130,
                                "title": f"Your part of {t}: {o['what']}", "why": o.get("why") or f"The team's steps of “{m['title']}” are done; the ticket closes when your part is done"
                                + (" and the team's check of it passes." if o.get("check") else "."),
                                "actions": [{"label": "I did it", "post": f"/api/missions/{m['id']}/owner/{o['i']}/done", "style": "primary"},
                                            {"label": "Open the mission", "href": "#/missions"}]})
            elif st == "ready":
                before = [o for o in p.get("owner_steps") or [] if o["when"] == "before"]
                after = [o for o in p.get("owner_steps") or [] if o["when"] == "after"]
                why = f"{len(p.get('steps') or [])} steps, risk {p.get('risk', '?')}, each with a check and an undo."
                if before:
                    why += f" You do {len(before)} thing(s) first."
                if after:
                    why += f" Afterwards {len(after)} part(s) are yours."
                worst = (m.get("review") or {}).get("worst")
                if worst in ("caution", "disagree"):
                    why += f" A reviewer said: {'go carefully' if worst == 'caution' else 'wait'}."
                out.append({"key": f"approve:{m['id']}", "who": "you", "kind": "approve", "ticket": t, "mission": m["id"],
                            "score": 120 + {"high": -15, "medium": 0, "low": 5}.get(p.get("risk"), 0), "title": f"Approve {t}: {m['title']}", "why": why,
                            "actions": [{"label": "Read the plan and approve", "href": "#/missions", "style": "primary"}]})
            elif m["ref"] and newest.get(m["ref"], 0) > m["id"] and st in ENDED and st != "rollback_failed":
                continue                                 # a newer mission on the same thing replaced this attempt
            elif st in ("needs_revision", "rollback_failed", "lost") or (st in ENDED and st not in ("rejected", "plan_failed") and now - m["updated"] < 7 * 86400):
                words = {"needs_revision": "the safety checks found problems Sage could not fix", "rollback_failed": "it stopped and an undo failed: check the machine",
                         "lost": "the team lost track of the run", "rolled_back": "it stopped and was undone", "failed": "it failed", "refused": "the runner refused it"}.get(st, st)
                out.append({"key": f"decide:{m['id']}", "who": "you", "kind": "decide", "ticket": t, "mission": m["id"], "score": 140 if st == "rollback_failed" else 85,
                            "title": f"Decide on {t}: {m['title']}", "why": f"{words[0].upper() + words[1:]}. Replan it with a note, or reject it.",
                            "actions": [{"label": "Open the mission", "href": "#/missions", "style": "primary"}]})
            elif st in ("planning", "approved", "running", "checking_your_part"):
                what = {"planning": "Sage is planning it with Atlas and Nova", "approved": "approved, starting", "checking_your_part": "the team is checking your part",
                        "running": f"running: {sum(s.get('state') == 'ok' for s in (m.get('run') or {}).get('steps', []))} of {len(p.get('steps') or [])} steps done"}[st]
                out.append({"key": f"team:{m['id']}", "who": "team", "kind": "working", "ticket": t, "mission": m["id"], "score": 30,
                            "title": f"{t}: {m['title']}", "why": what[0].upper() + what[1:] + ".", "actions": [{"label": "Watch it", "href": "#/missions"}]})
        if self.missions:
            for td in self.missions.todos():
                out.append({"key": f"todo:{td['id']}", "who": "you", "kind": "chore", "score": 60 * _age_factor(td["ts"], now),
                            "title": td["what"], "why": td.get("why") or (f"From {td['src']}." if td.get("src") else "Only you can do this."),
                            "actions": [{"label": "I did it", "post": f"/api/next/todo/{td['id']}/done", "style": "primary"},
                                        {"label": "Not now", "post": f"/api/next/todo/{td['id']}/dismiss"}]})
        out += self._findings(ms, now)
        out.sort(key=lambda x: -x["score"])
        mine = [x for x in out if x["who"] == "you"]
        team = [x for x in out if x["who"] == "team"]
        picked = mine[:limit - min(2, len(team))] + team[:2]                 # the team's work in progress is shown, but never pushes out more than 2 of yours
        picked.sort(key=lambda x: -x["score"])
        cache = self.store.kv_get("next:why") or {}
        for x in picked:
            w = (cache.get("whys") or {}).get(x["key"])
            if w:
                x["why_plain"], x["why"], x["why_by"] = x["why"], w, cache.get("by")
            x["score"] = round(x["score"], 1)
        return picked

    def _findings(self, ms, now):
        busy, rejected, finished = set(), {}, {}
        for m in ms:
            if not m["ref"].startswith("finding:"):
                continue
            fid = m["ref"].split(":", 1)[1]
            if m["status"] in ACTIVE:
                busy.add(fid)
            if m["status"] == "rejected":
                rejected[fid] = m
            if m["status"] == "done" and m["id"] > finished.get(fid, {"id": 0})["id"]:
                finished[fid] = m
        fs = [f for f in self.store.open_findings(500) if SEV.get(f["severity"], 0) >= SEV["medium"] and f["id"] not in busy]
        by_agent = {}
        for f in fs:
            by_agent.setdefault(f["agent"], []).append(f)
        out = []
        for agent, group in by_agent.items():
            if len(group) >= 4:                       # many of the same kind (e.g. config drift): one line, not a wall
                worst = max(group, key=lambda f: SEV.get(f["severity"], 0))
                out.append({"key": f"group:{agent}", "who": "you", "kind": "review", "score": SEV[worst["severity"]] * 0.8 * _age_factor(min(f["ts"] for f in group), now),
                            "title": f"{len(group)} open findings from {agent.replace('_', ' ')}, e.g. “{worst['title']}”",
                            "why": "Mark the expected ones done (for config drift that re-baselines them) and plan a mission for the rest.",
                            "actions": [{"label": "Review them", "href": f"#/agent/{agent}", "style": "primary"}]})
                continue
            for f in group:
                score = SEV[f["severity"]] * _age_factor(f["ts"], now) * (0.7 if f.get("chronic") else 1)
                plan = {"label": "Plan a mission", "post": "/api/missions", "body": {"goal": f"Fix: {f['title']}. {scrub(f['summary'])[:300]}", "ref": f"finding:{f['id']}"}, "style": "primary"}
                accept = {"label": "Accept it", "post": f"/api/findings/{f['id']}/status", "body": {"status": "rejected"},
                          "confirm": "Accept this as it is? The crew stops reporting it for a while."}
                rj = rejected.get(f["id"])
                if rj:
                    out.append({"key": f"finding:{f['id']}", "who": "you", "kind": "decide", "score": score * 0.8, "title": f["title"],
                                "why": f"You rejected {tk(rj['id'])} for this. Accept it so it stops coming back, or have the team plan a different fix.",
                                "actions": [{**accept, "style": "primary"}, {**plan, "style": ""}]})
                    continue
                manual = (f.get("proposed_action") or {}).get("kind") == "manual"
                fin = finished.get(f["id"])
                out.append({"key": f"finding:{f['id']}", "who": "you", "kind": "plan", "score": score, "title": f["title"],
                            "why": (f"{tk(fin['id'])} finished but this is still open: " if fin else "") + scrub(f["summary"])[:220]
                            + (" Part of the fix may be yours by hand; the plan will say." if manual else ""),
                            "actions": [plan, accept, {"label": "Details", "href": f"#/finding/{f['id']}"}]})
        return out

    def lines(self, n=5):
        """Plain lines for the break room and the stand-up."""
        out = []
        for i, x in enumerate(self.items()[:n], 1):
            why = re.split(r"(?<=[.!?])\s", x["why"].strip(), 1)[0].rstrip(".")
            why = why if len(why) <= 90 else why[:87].rsplit(" ", 1)[0] + "…"
            what = {"approve": "approve it", "your_part": "press “I did it” when done", "plan": "plan a mission or accept it", "decide": "decide", "chore": "do it",
                    "review": "review them", "working": "in progress"}.get(x["kind"], "")
            out.append(f"{i}. {'You' if x['who'] == 'you' else 'The team'}: {x['title'][:100]}" + (f" → {what}" if what else ""))
        return out

    # ------------------------------------------------------------------ Sage's one-line why (optional)
    MAX_PER_DAY = 6
    MIN_GAP = 2 * 3600

    async def refresh_why(self, now=None, force=False):
        now = now or time.time()
        if not self.adv or not self.adv.ready("plan"):
            return False
        items = self.items(now)
        if not items:
            return False
        sig = hashlib.sha256(json.dumps([x["key"] for x in items]).encode()).hexdigest()[:16]
        cache = self.store.kv_get("next:why") or {}
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        n = cache.get("n", 0) if cache.get("day") == day else 0
        if not force and (cache.get("sig") == sig or now - cache.get("ts", 0) < self.MIN_GAP or n >= self.MAX_PER_DAY):
            return False
        text = "\n".join(f"- key={x['key']} | {('OWNER' if x['who'] == 'you' else 'TEAM')} | {x['title']} | facts: {x.get('why_plain', x['why'])}" for x in items)
        try:
            r = await self.adv.ask("plan", "You lead an AI operations team for a home lab. The owner sees this ranked list of next actions. For each key, write ONE short plain "
                                   "sentence (at most 25 words) saying why it matters now, using only the facts given. Reply with ONE JSON object: {\"<key>\": \"sentence\", ...}.",
                                   text, 600)
        except FrontierError:
            return False
        j = extract_json(r["text"]) or {}
        keys = {x["key"] for x in items}
        whys = {k: scrub(str(v))[:220] for k, v in j.items() if k in keys and isinstance(v, str) and 10 < len(v.strip())}
        self.store.kv_set("next:why", {"sig": sig, "ts": now, "day": day, "n": n + 1, "by": r.get("advisor"), "whys": whys})
        return bool(whys)

    async def run(self, every=600):
        await asyncio.sleep(120)
        while True:
            try:
                await self.refresh_why()
            except Exception as e:
                print(f"nextsteps: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(every)

    def start(self):
        self.task = asyncio.create_task(self.run())
