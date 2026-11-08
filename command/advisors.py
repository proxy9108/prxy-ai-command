"""Outside advisors: Claude, OpenAI and Gemini as members of High Command who LEAD the local crew. They never act.

Each advisor is a hosted model reached with the owner's own API key (OpenAI, Gemini) or the existing Claude setup (frontier.py). They are given jobs:
  plan     turn a goal into tasks for the local agents (team projects)
  review   second opinion on proposed steps and on a project's findings ("panel" = every enabled advisor answers separately)
  verify   check that an approved action did what was intended
  coach    once a day: read the crew's state and start up to N read-only investigations for the local agents, with a note for the crew
What leaves this machine is exactly what frontier.py lets out (outside addresses and MACs masked, secret-looking strings removed, optional anonymising),
every call is logged for 14 days with its cost, and each advisor has its own monthly budget and daily call cap. When an advisor is off, over budget or failing,
the job falls back to another advisor and finally to the local models. Advisors only return text: no credential, no shell, no tool, no way to change the lab."""
import asyncio, json, os, re, time
from datetime import datetime

import aiohttp

from .frontier import FrontierError, extract_json

PROVIDERS = ("claude", "openai", "gemini")
JOBS = ("plan", "review", "verify", "coach", "research")
NAMES = {"claude": "Claude (Anthropic)", "openai": "OpenAI", "gemini": "Google Gemini"}
THINK_HEADROOM = 1024        # current OpenAI/Gemini models reason before answering and bill those tokens as output; without headroom a short allowance yields an empty answer


class Advisors:
    """Drop-in for Frontier (`available(job)`, `ask(job, system, user, max_tokens)`), plus `panel()` and `coach()`."""

    def __init__(self, cfg, store, bus, frontier, keys=None, bases=None, team=None):
        self.cfg, self.store, self.bus, self.claude = cfg, store, bus, frontier
        keys = keys or {}
        self.keys = {"openai": keys.get("openai", os.getenv("OPENAI_API_KEY", "")), "gemini": keys.get("gemini", os.getenv("GEMINI_API_KEY", ""))}
        bases = bases or {}
        self.bases = {"openai": (bases.get("openai") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
                      "gemini": (bases.get("gemini") or os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")}
        self.team = team
        self.sems = {p: asyncio.Semaphore(1) for p in ("openai", "gemini")}
        self.last_error = {p: None for p in PROVIDERS}
        self.task = None

    # ------------------------------------------------------------------ policy
    def policy(self):
        return self.cfg.get()["advisors"]

    def _month(self):
        n = datetime.now(); return datetime(n.year, n.month, 1).timestamp()

    def _day(self):
        n = datetime.now(); return datetime(n.year, n.month, n.day).timestamp()

    def why_not(self, prov, job=None):
        """None when this advisor may take a call now, else the reason in plain words."""
        if prov == "claude":
            return self.claude.why_not(job if job in ("plan", "review", "verify", "summary") else None) if self.claude else "not configured"
        p = self.policy()[prov]
        if not p["enabled"]:
            return "switched off"
        if not self.keys.get(prov):
            return "no API key on the server"
        if self.store.frontier_stats(self._day(), prov)["n"] >= p["daily_call_cap"]:
            return "the daily call cap is reached"
        if self.store.frontier_stats(self._month(), prov)["cost"] >= p["monthly_budget_usd"]:
            return "this month's budget is used up"
        return None

    def order_for(self, job):
        """Advisors to try for a job, the assigned one first. 'panel' jobs list every panel member."""
        p = self.policy()
        who = p["jobs"].get(job, "claude")
        if who == "panel":
            return [x for x in p["panel"] if x in PROVIDERS]
        return [who] + [x for x in PROVIDERS if x != who]

    def ready(self, job):
        return [x for x in self.order_for(job) if self.why_not(x, job) is None]

    def available(self, job=None):
        return bool(self.ready(job or "review"))

    # Frontier compatibility for code that reads these
    def policy_frontier(self):
        return self.claude.policy() if self.claude else {}

    # ------------------------------------------------------------------ calls
    async def _openai(self, system, user, max_tokens, p):
        body = {"model": p["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_completion_tokens": max_tokens + THINK_HEADROOM}
        if p.get("effort"):
            body["reasoning_effort"] = p["effort"]
        hdr = {"Authorization": f"Bearer {self.keys['openai']}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(self.bases["openai"] + "/chat/completions", json=body, headers=hdr, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    d = await r.json(content_type=None)
                    if r.status in (401, 403):
                        raise FrontierError("the OpenAI key was refused")
                    if r.status == 429:
                        raise FrontierError("OpenAI says: rate limited or out of credit")
                    if r.status >= 400:
                        raise FrontierError(f"OpenAI answered HTTP {r.status}: {str((d.get('error') or {}).get('message', ''))[:120]}")
            except aiohttp.ClientError:
                raise FrontierError("could not reach OpenAI")
            except asyncio.TimeoutError:
                raise FrontierError("OpenAI took too long")
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        if not (msg.get("content") or "").strip() and ch.get("finish_reason") == "length":
            raise FrontierError("the model used its whole token allowance thinking and wrote no answer (lower 'effort' or pick a non-reasoning model)")
        if msg.get("refusal"):
            raise FrontierError("the model declined this request")
        u = d.get("usage") or {}
        return (msg.get("content") or "").strip(), d.get("model", p["model"]), u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

    async def _gemini(self, system, user, max_tokens, p):
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens + THINK_HEADROOM}}
        hdr = {"x-goog-api-key": self.keys["gemini"], "Content-Type": "application/json"}         # the key goes in a header, never in the URL (URLs end up in logs)
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(f"{self.bases['gemini']}/models/{p['model']}:generateContent", json=body, headers=hdr, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    d = await r.json(content_type=None)
                    if r.status in (401, 403):
                        raise FrontierError("the Gemini key was refused")
                    if r.status == 429:
                        raise FrontierError("Gemini says: rate limited or out of quota")
                    if r.status >= 400:
                        raise FrontierError(f"Gemini answered HTTP {r.status}: {str((d.get('error') or {}).get('message', ''))[:120]}")
            except aiohttp.ClientError:
                raise FrontierError("could not reach Gemini")
            except asyncio.TimeoutError:
                raise FrontierError("Gemini took too long")
        cands = d.get("candidates") or []
        if not cands:
            raise FrontierError("Gemini returned no answer" + (" (blocked)" if (d.get("promptFeedback") or {}).get("blockReason") else ""))
        c = cands[0]
        if c.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise FrontierError("the model declined this request")
        if c.get("finishReason") == "MAX_TOKENS" and not any(pt.get("text") and not pt.get("thought") for pt in (c.get("content") or {}).get("parts", [])):
            raise FrontierError("the model used its whole token allowance thinking and wrote no answer")
        text = "".join(pt.get("text", "") for pt in (c.get("content") or {}).get("parts", []) if not pt.get("thought")).strip()
        u = d.get("usageMetadata") or {}
        return text, d.get("modelVersion", p["model"]), u.get("promptTokenCount", 0), (u.get("candidatesTokenCount", 0) or 0) + (u.get("thoughtsTokenCount", 0) or 0)

    async def call(self, prov, job, system, user, max_tokens=1200):
        """One advisor, one call: privacy filter, budget, log, audit. -> {text, backend, model, advisor}. Raises FrontierError."""
        if prov == "claude":
            r = await self.claude.ask(job if job in ("plan", "review", "verify", "summary", "test") else "review", system, user, max_tokens)
            return {**r, "advisor": "claude"}
        why = self.why_not(prov, job)
        if why:
            raise FrontierError(why)
        p = self.policy()[prov]
        csys, _ = self.claude.prepare(system)
        cuser, mapping = self.claude.prepare(user)
        t0 = time.time()
        async with self.sems[prov]:
            try:
                text, model, tin, tout = await (self._openai if prov == "openai" else self._gemini)(csys, cuser, max_tokens, p)
                if not text:
                    raise FrontierError("an empty answer")
            except FrontierError as e:
                self.last_error[prov] = str(e)
                self.store.frontier_add(job, prov, p["model"], 0, 0, 0, False, str(e), f"{csys}\n---\n{cuser}", "")
                self.store.audit("system", "advisor.failed", f"{prov}:{job}", str(e)[:150])
                raise
        self.last_error[prov] = None
        cost = (tin * p["price_in"] + tout * p["price_out"]) / 1e6
        self.store.frontier_add(job, prov, model, tin, tout, cost, True, None, f"{csys}\n---\n{cuser}", text)
        self.store.audit("system", "advisor.call", f"{prov}:{job}", f"{model}: {len(cuser)} chars sent, {tin}+{tout} tokens, {time.time() - t0:.1f}s, ~${cost:.4f}")
        if self.bus:
            self.bus.publish("frontier.call", agent="commander", purpose=job, backend=prov)
        return {"text": self.claude.restore(text, mapping), "backend": prov, "model": model, "advisor": prov}

    async def ask(self, job, system, user, max_tokens=1200):
        """The assigned advisor answers; if it can't, the next one does. Same contract as Frontier.ask."""
        errs = []
        for prov in self.ready(job):
            try:
                return await self.call(prov, job, system, user, max_tokens)
            except FrontierError as e:
                errs.append(f"{prov}: {e}")
        raise FrontierError("; ".join(errs) or "no outside advisor is available for this job")

    async def panel(self, job, system, user, max_tokens=900):
        """Every ready panel member answers on its own (in parallel). -> [replies]; failures are left out."""
        who = [x for x in self.policy()["panel"] if x in PROVIDERS and self.why_not(x, job) is None]
        res = await asyncio.gather(*(self.call(p, job, system, user, max_tokens) for p in who), return_exceptions=True)
        return [r for r in res if isinstance(r, dict)]

    # ------------------------------------------------------------------ status for Settings
    def status(self):
        p, ms, ds = self.policy(), self._month(), self._day()
        out = {"jobs": p["jobs"], "panel": p["panel"], "coach_at": p["coach_at"], "coach_max_projects": p["coach_max_projects"], "advisors": []}
        for prov in PROVIDERS:
            if prov == "claude":
                fs = self.claude.status() if self.claude else {}
                api, cli = self.store.frontier_stats(ms, "api"), self.store.frontier_stats(ms, "cli")
                out["advisors"].append({"name": "claude", "provider": NAMES["claude"], "enabled": fs.get("enabled", False), "key_set": fs.get("api_key_set") or fs.get("cli_found"),
                                        "model": fs.get("model"), "spent_usd": round(api["cost"], 4), "budget_usd": (fs.get("policy") or {}).get("monthly_budget_usd"),
                                        "calls_month": api["n"] + cli["n"], "calls_today": self.store.frontier_stats(ds, "api")["n"] + self.store.frontier_stats(ds, "cli")["n"],
                                        "daily_cap": fs.get("daily_cap"), "blocked_because": self.why_not("claude"), "last_error": fs.get("last_error"), "via": fs.get("backend")})
                continue
            q, m = p[prov], self.store.frontier_stats(ms, prov)
            out["advisors"].append({"name": prov, "provider": NAMES[prov], "enabled": q["enabled"], "key_set": bool(self.keys.get(prov)), "model": q["model"],
                                    "spent_usd": round(m["cost"], 4), "budget_usd": q["monthly_budget_usd"], "calls_month": m["n"], "calls_today": self.store.frontier_stats(ds, prov)["n"],
                                    "daily_cap": q["daily_call_cap"], "blocked_because": self.why_not(prov), "last_error": self.last_error[prov], "via": "api"})
        rows = self.store.q("SELECT backend, purpose, COUNT(*) n, COALESCE(SUM(cost),0) cost, COALESCE(SUM(tin+tout),0) tok FROM frontier_log WHERE ts>=? AND ok=1 GROUP BY backend, purpose", (ms,))
        out["usage"] = [{"advisor": "claude" if r["backend"] in ("api", "cli") else r["backend"], "job": r["purpose"], "calls": r["n"], "cost_usd": round(r["cost"], 4), "tokens": r["tok"]} for r in rows]
        out["recent"] = [{"ts": r["ts"], "advisor": "claude" if r["backend"] in ("api", "cli") else r["backend"], "job": r["purpose"], "model": r["model"], "ok": bool(r["ok"]),
                          "tokens": (r["tin"] or 0) + (r["tout"] or 0), "cost_usd": round(r["cost"] or 0, 5), "error": r["error"]}
                         for r in self.store.q("SELECT ts, backend, purpose, model, ok, tin, tout, cost, error FROM frontier_log ORDER BY id DESC LIMIT 25")]
        return out

    async def test(self, prov):
        """A tiny call that proves the key and model work (counts against the budget: a few tokens)."""
        if prov not in PROVIDERS:
            raise FrontierError("unknown advisor")
        r = await self.call(prov, "test", "You are being tested. Reply with exactly: OK", "Reply with OK.", 40)
        return {"ok": True, "model": r["model"], "reply": r["text"][:40]}

    # ------------------------------------------------------------------ coaching: the advisors lead the local crew
    def digest(self):
        st = self.store
        fs = sorted([f for f in st.open_findings(500) if f["status"] in ("new", "seen")], key=lambda f: ({"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}[f["severity"]], -f["count"]))[:15]
        lines = [f"- [{f['severity']}] {f['title']} (raised by {f['agent']}, seen {f['count']} times{', unchanged for 2 weeks' if f.get('chronic') else ''})" for f in fs]
        su = st.kv_get("chat:standup:last")
        notes = [m["text"] for m in st.chat_list(f"standup:{su}", limit=12)] if su else []
        recent = [p["title"] for p in st.project_list(15) if time.time() - p["created"] < 7 * 86400]
        onotes = [n for n in (st.kv_get("owner:notes") or []) if time.time() - n["ts"] < 14 * 86400]
        known = [k for k in (st.kv_get("team:knowledge") or []) if time.time() - k["ts"] < 30 * 86400][-12:]
        return ("The owner's own notes (things they changed on purpose; do NOT send the crew to investigate these):\n" + ("\n".join(f"- {n['text']}" for n in onotes[-10:]) or "none") + "\n\n"
                "What missions already found out (do not send the crew to find it again):\n" + ("\n".join(f"- {k['ticket']}: {k['text']}" for k in known) or "none") + "\n\n"
                "Open findings (worst first):\n" + ("\n".join(lines) or "none")) + "\n\nLast stand-up:\n" + ("\n".join(notes) or "none") + "\n\nInvestigations already done this week:\n" + ("\n".join(f"- {t}" for t in recent) or "none")

    async def coach(self, now=None):
        """One coaching session: the coach advisor reads the crew's state and starts up to N read-only investigations. -> {'advisor', 'note', 'projects'} or None."""
        p = self.policy()
        if not self.team or p["coach_max_projects"] <= 0 or not self.ready("coach"):
            return None
        system = ("You lead a crew of small local AI agents that watch a home lab (a Proxmox host with LXC guests, backups, network logs, DNS, IoT). The agents can only READ and REPORT; "
                  "each investigation you start is planned and carried out by them, and any change still needs the owner's approval. From the state below pick at most "
                  f"{p['coach_max_projects']} investigations that would most help the owner today: something unexplained, getting worse, or blocking other work. Skip anything already investigated this week "
                  "unless it got worse. Reply with ONE JSON object: {\"note\": \"2 short sentences to the crew: what matters today and why\", \"investigations\": "
                  "[{\"goal\": \"one question the crew should answer, at most 25 words\", \"why\": \"one short reason\"}]}. Use only the facts given; invent no numbers or names.")
        try:
            r = await self.ask("coach", system, self.digest(), 700)
        except FrontierError:
            return None
        j = extract_json(r["text"])
        if not isinstance(j, dict):
            return None
        note = re.sub(r"\s+", " ", str(j.get("note") or "")).strip()[:400]
        started = []
        for inv in (j.get("investigations") or [])[:8]:
            if len(started) >= p["coach_max_projects"]:
                break
            if not isinstance(inv, dict) or not isinstance(inv.get("goal"), str):
                continue
            goal = re.sub(r"\s+", " ", inv["goal"]).strip()[:240]
            if len(goal) < 12:
                continue
            try:
                pid = self.team.create(goal, title=("Advisor: " + goal)[:90], origin="auto", ref=f"advisor:{r['advisor']}")    # 'auto' = read-only skills, no scans, no web
            except ValueError:
                continue
            started.append({"id": pid, "goal": goal, "why": str(inv.get("why") or "")[:200]})
        self.store.audit(f"advisor:{r['advisor']}", "advisor.coach", "", f"{len(started)} investigation(s) started; {note[:120]}")
        if self.bus:
            self.bus.publish("advisor.coach", agent=r["advisor"], note=note, projects=started)
        return {"advisor": r["advisor"], "note": note, "projects": started}

    async def run(self):
        await asyncio.sleep(120)
        while True:
            try:
                p = self.policy()
                if p["coach_at"]:
                    from zoneinfo import ZoneInfo
                    d = datetime.now(ZoneInfo(self.cfg.get()["tz"]))
                    h, m = map(int, p["coach_at"].split(":"))
                    today = d.strftime("%Y-%m-%d")
                    if (h, m) <= (d.hour, d.minute) <= (h + 3, m) and self.store.kv_get("advisors:coach:last") != today and self.ready("coach"):
                        self.store.kv_set("advisors:coach:last", today)
                        await self.coach()
            except Exception as e:
                print(f"advisors: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(120)

    def start(self):
        self.task = asyncio.create_task(self.run())
