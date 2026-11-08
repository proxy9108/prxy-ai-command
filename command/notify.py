"""Push notifications and escalation.

Who is told what:
  * a NEW finding at or above notify.min_severity -> one Discord message right away (title and a short summary only; no evidence, outside
    addresses masked, because the message leaves this machine);
  * the same finding getting WORSE -> another message;
  * a finding nobody has acknowledged (still 'new': 'seen', rejected, done... all count as acknowledged) -> reminders after 30 min, 2 h, 6 h,
    24 h and then daily, each with the @mention so a phone actually buzzes, and also on the second channel (ntfy) when that is set up;
  * a finding that clears -> a short 'cleared' note;
  * once a day -> the Commander's headline, which doubles as 'the system is alive'.
Findings held back by a maintenance window are never announced. If sending fails the message is retried; the Watchdog reports a notifier that cannot send.
The webhook address is read from the environment (DISCORD_WEBHOOK_URL) and is never stored in the config, logged or shown."""
import asyncio, os, time
from datetime import datetime, timezone
import aiohttp

from . import maintenance
from .redact import scrub

SEV_ORDER = ["info", "low", "medium", "high", "critical"]
COLOR = {"info": 0x9A9A94, "low": 0x7FB0D8, "medium": 0xF6A04A, "high": 0xE06A5F, "critical": 0xFF3B30, "ok": 0x5FBF7A}
ICON = {"info": "ℹ️", "low": "🔹", "medium": "🟠", "high": "🔴", "critical": "🚨"}
MAX_REMINDERS = 7
TICK_S = 20


def rank(sev):
    return SEV_ORDER.index(sev) if sev in SEV_ORDER else 0


class Notifier:
    def __init__(self, cfg, store, bus, webhook=None, ntfy_url=None, ntfy_token=None, public_url=None):
        self.cfg, self.store, self.bus = cfg, store, bus
        self.webhook = webhook if webhook is not None else os.getenv("DISCORD_WEBHOOK_URL", "")
        self.ntfy_url = ntfy_url if ntfy_url is not None else os.getenv("NTFY_URL", "")
        self.ntfy_token = ntfy_token if ntfy_token is not None else os.getenv("NTFY_TOKEN", "")
        self.public_url = (public_url or os.getenv("PUBLIC_URL", "https://10.20.40.139:8320")).rstrip("/")
        self.tasks, self.fails, self._last_send, self.gap_s = [], 0, 0.0, 1.1

    # ---- state
    def policy(self):
        return self.cfg.get()["notify"]

    def _state(self):
        return self.store.kv_get("notify:state", {"seeded": None, "items": {}})

    def _status(self):
        return self.store.kv_get("notify:status", {"last_ok": None, "last_error": None, "fails": 0, "recent": []})

    def configured(self):
        return bool(self.webhook or self.ntfy_url)

    def status(self):
        st = self._status()
        return {"discord": bool(self.webhook), "ntfy": bool(self.ntfy_url), "policy": self.policy(), "last_ok": st["last_ok"], "last_error": st["last_error"],
                "fails": st["fails"], "recent": st["recent"][-20:], "tracking": len(self._state()["items"])}

    def healthy(self):
        """False when notifications are wanted and configured but the last attempts failed (the Watchdog raises that)."""
        if not self.policy()["enabled"] or not self.configured():
            return True
        return self._status()["fails"] < 3

    def _note(self, ok, kind, title, error=None):
        st = self._status()
        if ok:
            if st["fails"] >= 3:
                self.store.audit("system", "notify.recovered", "", "notifications work again")
            st.update(last_ok=time.time(), last_error=None, fails=0)
            st["recent"] = (st["recent"] + [{"ts": time.time(), "kind": kind, "title": scrub(title)[:120]}])[-30:]
        else:
            st["fails"] += 1; st["last_error"] = str(error)[:200]
            if st["fails"] == 3:
                self.store.audit("system", "notify.failing", "", st["last_error"])
        self.store.kv_set("notify:status", st)
        self.fails = st["fails"]

    # ---- sending
    def link(self, fid=None):
        return f"{self.public_url}/#/finding/{fid}" if fid else f"{self.public_url}/#/findings"

    async def _post(self, payload):
        """One POST to the webhook; waits out a rate limit once. Returns (ok, error)."""
        gap = self.gap_s - (time.time() - self._last_send)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            async with aiohttp.ClientSession() as s:
                for attempt in range(2):
                    async with s.post(self.webhook, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as r:
                        self._last_send = time.time()
                        if r.status == 429 and attempt == 0:
                            try:
                                wait = float((await r.json()).get("retry_after", 2))
                            except Exception:
                                wait = 2.0
                            await asyncio.sleep(min(wait, 10))
                            continue
                        if r.status in (200, 204):
                            return True, None
                        return False, f"Discord answered HTTP {r.status}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return False, "rate limited"

    async def _ntfy(self, title, body, priority, fid=None):
        if not self.ntfy_url:
            return
        h = {"Title": title.encode("ascii", "replace").decode()[:120], "Priority": str(priority), "Tags": "warning", "Click": self.link(fid)}
        if self.ntfy_token:
            h["Authorization"] = "Bearer " + self.ntfy_token
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(self.ntfy_url, data=body.encode()[:900], headers=h, timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass                                   # the second channel is best-effort; Discord's failure is what the Watchdog tracks

    def embed(self, f, kind, minutes=None):
        head = {"new": "New", "worse": "Got worse", "remind": f"Still unacknowledged after {self._dur(minutes)}", "cleared": "Cleared"}[kind]
        sev = f["severity"]
        e = {"title": f"{ICON.get(sev, '')} {head}: {scrub(f['title'])}"[:250], "description": scrub(f.get("summary", ""))[:350], "url": self.link(f["id"]),
             "color": COLOR["ok"] if kind == "cleared" else COLOR.get(sev, COLOR["info"]),
             "fields": [{"name": "Agent", "value": f["agent"], "inline": True}, {"name": "Severity", "value": sev, "inline": True}, {"name": "Seen", "value": f"{f.get('count', 1)}x", "inline": True}],
             "footer": {"text": "Prxy-AI Command · open the link to acknowledge"}, "timestamp": datetime.now(timezone.utc).isoformat()}
        return e

    @staticmethod
    def _dur(m):
        m = int(m or 0)
        return f"{m} min" if m < 120 else f"{m // 60} h" if m < 2880 else f"{m // 1440} days"

    async def send_embeds(self, embeds, content="", mention=False):
        if not self.webhook:
            return True, None
        payload = {"username": "Prxy Command", "embeds": embeds[:10], "allowed_mentions": {"parse": ["everyone"] if mention else []}}
        if content:
            payload["content"] = content[:1900]
        return await self._post(payload)

    async def send_test(self):
        e = {"title": "Notifications are connected", "description": "This is a test message from Prxy-AI Command. High and critical findings will arrive here, with reminders until you acknowledge them.",
             "color": COLOR["ok"], "footer": {"text": "Prxy-AI Command"}, "timestamp": datetime.now(timezone.utc).isoformat()}
        ok, err = await self.send_embeds([e])
        self._note(ok, "test", "test message", err)
        return {"ok": ok, "error": err, "discord": bool(self.webhook)}

    # ---- the decision loop
    async def tick(self, now=None):
        pol = self.policy()
        if not pol["enabled"] or not self.configured():
            return 0
        now = now or time.time()
        st = self._state()
        items = st["items"]
        minrank = rank(pol["min_severity"])
        open_f = [f for f in self.store.open_findings(500) if rank(f["severity"]) >= minrank and f["agent"] != "demo"
                  and not maintenance.match(self.store, f["agent"], f["fingerprint"], f["title"], f["summary"], now)]
        open_ids = {f["id"] for f in open_f}
        out, commit = [], []                      # messages to send, and the state changes to keep only if sending worked
        if st["seeded"] is None:                  # first start: one digest of what is open now (it also proves the webhook works), not one message per old finding
            lines = [f"{ICON.get(f['severity'], '')} {scrub(f['title'])[:90]}" for f in sorted(open_f, key=lambda f: -rank(f["severity"]))[:10]]
            e = {"title": "Notifications are on", "color": COLOR["ok"], "url": self.link(), "footer": {"text": "Prxy-AI Command"}, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "description": (f"Open right now at {pol['min_severity']} or above ({len(open_f)}):\n" + "\n".join(lines)) if open_f else f"Nothing at {pol['min_severity']} or above is open. You will hear from me when something is."}
            ok, err = await self.send_embeds([e])
            self._note(ok, "start", "notifications on", err)
            if ok:
                st["seeded"] = now
                for f in open_f:
                    items[f["id"]] = {"first": now, "sev": f["severity"], "n": 0, "last": now}
                self.store.kv_set("notify:state", st)
            return 1 if ok else 0
        for f in open_f:
            it = items.get(f["id"])
            if it is None:
                out.append(("new", f, None)); commit.append(lambda f=f: items.__setitem__(f["id"], {"first": now, "sev": f["severity"], "n": 0, "last": now}))
            elif rank(f["severity"]) > rank(it["sev"]):
                out.append(("worse", f, None)); commit.append(lambda f=f, it=it: it.update(sev=f["severity"], last=now))
            elif f["status"] == "new" and not f["chronic"] and it["n"] < MAX_REMINDERS:
                steps = pol["escalate_min"]
                due = it["first"] + steps[it["n"]] * 60 if it["n"] < len(steps) else it["last"] + 86400
                if now >= due:
                    out.append(("remind", f, (now - it["first"]) / 60)); commit.append(lambda it=it: it.update(n=it["n"] + 1, last=now))
            elif f["status"] != "new" and it["sev"] != f["severity"]:
                commit.append(lambda f=f, it=it: it.update(sev=f["severity"]))
        for fid in [k for k in items if k not in open_ids]:       # no longer open (or no longer severe enough)
            f = self.store.get(fid)
            if f and f["status"] in ("resolved", "done") and pol["resolved"] and rank(f["severity"]) >= minrank:
                out.append(("cleared", f, None))
            commit.append(lambda fid=fid: items.pop(fid, None))
        for fid in [k for k, v in items.items() if now - v["first"] > 30 * 86400]:
            commit.append(lambda fid=fid: items.pop(fid, None))
        sent = 0
        if out:
            ok, err = True, None
            if len(out) > 4:                      # a burst becomes one message: fewer pings, and Discord's rate limit is never an issue
                lines = [f"{ICON.get(f['severity'], '')} **{k}**: {scrub(f['title'])[:100]}" for k, f, _ in out[:15]]
                e = {"title": f"{len(out)} updates", "description": "\n".join(lines), "url": self.link(), "color": COLOR["high"], "footer": {"text": "Prxy-AI Command"}, "timestamp": datetime.now(timezone.utc).isoformat()}
                reminders = any(k == "remind" for k, _, _ in out)
                ok, err = await self.send_embeds([e], content=pol["mention"] if reminders and pol["mention"] else "", mention=reminders and bool(pol["mention"]))
                self._note(ok, "digest", f"{len(out)} updates", err)
            else:
                for kind, f, minutes in out:
                    mention = kind == "remind" and bool(pol["mention"])
                    ok, err = await self.send_embeds([self.embed(f, kind, minutes)], content=(pol["mention"] + " unacknowledged") if mention else "", mention=mention)
                    self._note(ok, kind, f["title"], err)
                    if not ok:
                        break
                    self.store.audit("system", f"notify.{kind}", f["id"], scrub(f["title"])[:100])
            if ok:
                sent = len(out)
                for kind, f, minutes in out:      # second channel: reminders and anything critical
                    if kind == "remind" or f["severity"] == "critical":
                        await self._ntfy(scrub(f["title"])[:100], scrub(f.get("summary", ""))[:300], 5 if f["severity"] == "critical" else 4, f["id"])
            else:
                return 0                          # nothing is committed: the same messages are tried again on the next tick
        for fn in commit:
            fn()
        self.store.kv_set("notify:state", st)
        return sent

    async def briefing(self, bid):
        pol = self.policy()
        if not pol["enabled"] or not pol["daily_briefing"] or not self.webhook:
            return
        b = self.store.get_briefing(bid)
        if not b:
            return
        d = b["doc"]
        top = [f"{ICON.get(g['severity'], '')} {scrub(g['title'])[:100]}" for g in d.get("top", [])[:4]]
        c = d.get("counts", {})
        e = {"title": "Morning briefing", "description": scrub(d.get("headline", ""))[:400] + ("\n\n" + "\n".join(top) if top else ""), "url": f"{self.public_url}/#/briefing/{bid}",
             "color": COLOR["high"] if c.get("topics_attention") else COLOR["ok"], "footer": {"text": f"{c.get('open', 0)} open · {c.get('new_24h', 0)} new in 24 h · {len(d.get('blind_spots', []))} blind spots"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
        ok, err = await self.send_embeds([e])
        self._note(ok, "briefing", "morning briefing", err)

    async def action_event(self, a):
        """The team carried out (or tried, or undid) an action: always tell the owner, whatever the severity setting."""
        if not self.webhook or not a:
            return
        from . import actionspec
        what = actionspec.describe(a["action"], a["params"])
        titles = {"done": ("Done", COLOR["ok"]), "failed": ("FAILED", COLOR["high"]), "verify_failed": ("Check failed", COLOR["high"]), "rolled_back": ("Rolled back", COLOR["medium"]), "rollback_failed": ("ROLLBACK FAILED: needs you", COLOR["critical"]), "blocked": ("Blocked", COLOR["medium"])}
        head, color = titles.get(a["status"], (a["status"], COLOR["info"]))
        o = a.get("outside") or {}
        e = {"title": f"Action {head}: {what}"[:250], "url": f"{self.public_url}/#/approvals", "color": color, "timestamp": datetime.now(timezone.utc).isoformat(),
             "description": scrub(a.get("note") or "")[:500] + (f"\nSecond look ({o.get('backend')}): {o.get('verdict')} - {scrub(o.get('note', ''))[:200]}" if o else ""),
             "footer": {"text": "Prxy-AI Command · carried out only after your approval · see the audit log"}}
        bad = a["status"] in ("failed", "verify_failed", "rollback_failed")
        ok, err = await self.send_embeds([e], content=self.policy()["mention"] if bad and self.policy()["mention"] else "", mention=bad and bool(self.policy()["mention"]))
        self._note(ok, "action", what, err)

    async def team_done(self, p):
        """An investigation the team opened by itself is finished: one message with the summary and a link."""
        pol = self.policy()
        if not pol["enabled"] or not self.webhook:
            return
        r = p.get("result") or {}
        recs = r.get("recommendations") or []
        e = {"title": f"Team finished: {scrub(p['title'])}"[:250], "url": f"{self.public_url}/#/team/{p['id']}", "color": COLOR["ok"], "timestamp": datetime.now(timezone.utc).isoformat(),
             "description": (scrub(r.get("summary", ""))[:700] + ("\n\nProposed: " + "; ".join(scrub(x["title"])[:80] for x in recs[:3]) if recs else "")),
             "footer": {"text": f"{len(recs)} recommendation(s) · the Critic says: {(r.get('critic') or {}).get('verdict', 'n/a')} · nothing was changed"}}
        ok, err = await self.send_embeds([e])
        self._note(ok, "team", p["title"], err)

    async def owner_part(self, ev):
        """A mission's team steps are done and it now waits for something only the owner can do: one message with the exact steps."""
        pol = self.policy()
        if not pol["enabled"] or not self.webhook:
            return
        parts = [scrub(str(p))[:300] for p in (ev.get("parts") or [])][:5]
        failed = ev.get("type") == "mission.owner_check_failed"
        e = {"title": (f"{ev.get('ticket')}: your part isn't confirmed yet" if failed else f"{ev.get('ticket')}: your part is next")[:250], "url": f"{self.public_url}/#/missions",
             "color": COLOR["medium"] if failed else COLOR["info"], "timestamp": datetime.now(timezone.utc).isoformat(),
             "description": (f"“{scrub(str(ev.get('title') or ''))[:200]}”\n\n" + ("The team's check said: " + scrub(str(ev.get("output") or ""))[:300] + "\n\n" if failed else "The team's steps are done and checked.\n\n")
                             + "\n".join(f"• {p}" for p in parts))[:1500],
             "footer": {"text": "Press “I did it” on the Missions page; the team checks it, then closes the ticket"}}
        ok, err = await self.send_embeds([e])
        self._note(ok, "mission", str(ev.get("ticket")), err)

    async def run(self):
        q = self.bus.subscribe()
        listener = asyncio.create_task(self._listen(q))
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:                # never let a bad tick kill the loop
                    self._note(False, "tick", "", f"{type(e).__name__}: {e}")
                await asyncio.sleep(min(300, TICK_S * (2 ** min(self.fails, 4))))      # back off while Discord is unreachable
        finally:
            listener.cancel(); self.bus.unsubscribe(q)

    async def _listen(self, q):
        while True:
            ev = await q.get()
            if ev.get("type") in ("mission.waiting_for_you", "mission.owner_check_failed"):
                try:
                    await self.owner_part(ev)
                except Exception as e:
                    self._note(False, "mission", "", f"{type(e).__name__}: {e}")
                continue
            if ev.get("type") == "council.briefing" and ev.get("trigger") == "schedule":
                try:
                    await self.briefing(ev["id"])
                except Exception as e:
                    self._note(False, "briefing", "", f"{type(e).__name__}: {e}")

    def start(self):
        self.tasks = [asyncio.create_task(self.run())]
