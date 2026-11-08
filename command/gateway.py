"""LLM gateway: the ONLY way agents talk to Ollama.
- one job at a time (one GPU), priority queue: bot(0) > interactive(1) > agent(2) > background(3)
- model-swap guard: outside quiet hours an agent job that needs a model other than the one(s) loaded right now waits,
  so a big agent model cannot evict the Discord bot's model during the day
- per-agent daily token budget, timeouts, think:false, usage log."""
import asyncio, time
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from . import trace

BOT, INTERACTIVE, AGENT, BACKGROUND = 0, 1, 2, 3


class GatewayError(Exception):
    pass


class Gateway:
    def __init__(self, cfg, store, bus, ollama_url):
        self.cfg, self.store, self.bus, self.url = cfg, store, bus, ollama_url.rstrip("/")
        self.pending, self.current, self.seq = [], None, 0
        self.wake = asyncio.Event()
        self._ps, self._ps_at = [], 0.0
        self.task = None

    # ---- helpers
    def settings(self):
        return self.cfg.get()["gateway"]

    def day_start(self):
        tz = ZoneInfo(self.cfg.get()["tz"])
        n = datetime.now(tz)
        return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def quiet_now(self):
        tz = ZoneInfo(self.cfg.get()["tz"])
        a, b = self.settings()["quiet_hours"].split("-")
        now = datetime.now(tz).strftime("%H:%M")
        return (a <= now < b) if a <= b else (now >= a or now < b)

    async def loaded_models(self):
        if time.time() - self._ps_at < 5:
            return self._ps
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.url}/api/ps", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    self._ps = [m["name"] for m in (await r.json()).get("models", [])]
        except Exception:
            self._ps = []
        self._ps_at = time.time()
        return self._ps

    def budget_for(self, agent):
        a = self.cfg.get()["agents"].get(agent, {})
        return a.get("token_budget", self.settings()["token_budget_per_agent_day"])

    # ---- public API
    async def chat(self, agent, messages, *, model=None, priority=AGENT, options=None, allow_swap=False, timeout=None):
        """Queue one chat completion; resolves to {text, model, tokens, ms}. Raises GatewayError."""
        s = self.settings()
        model = model or self.cfg.get()["agents"].get(agent, {}).get("model") or s["default_model"]
        if self.budget_for(agent) and self.store.tokens_today(agent, self.day_start()) >= self.budget_for(agent):
            raise GatewayError(f"{agent} is over its daily token budget")
        fut = asyncio.get_running_loop().create_future()
        self.seq += 1
        job = {"agent": agent, "messages": messages, "model": model, "priority": priority, "seq": self.seq, "fut": fut,
               "options": options or {}, "allow_swap": allow_swap, "timeout": timeout or s["timeout_s"], "queued": time.time(), "run_id": trace.current()}
        self.pending.append(job)
        self.bus.publish("llm.queued", agent=agent, model=model, priority=priority, depth=len(self.pending))
        self.wake.set()
        return await fut

    def status(self):
        since = self.day_start()
        return {"queue": [{"agent": j["agent"], "model": j["model"], "priority": j["priority"], "waiting_s": round(time.time() - j["queued"])} for j in sorted(self.pending, key=lambda j: (j["priority"], j["seq"]))],
                "current": self.current, "loaded_models": self._ps, "quiet_now": self.quiet_now(), "quiet_hours": self.settings()["quiet_hours"],
                "usage_today": self.store.usage_summary(since)}

    # ---- worker
    async def _eligible(self):
        loaded = await self.loaded_models()
        quiet = self.quiet_now()
        for j in sorted(self.pending, key=lambda j: (j["priority"], j["seq"])):
            if j["priority"] <= INTERACTIVE or j["allow_swap"] or quiet or not loaded or j["model"] in loaded:
                return j
        return None                         # everything left would evict the bot's model: wait for quiet hours or a matching model

    async def run(self):
        while True:
            job = await self._eligible() if self.pending else None
            if job is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            self.pending.remove(job)
            await self._do(job)

    async def _do(self, job):
        s = self.settings()
        opts = {"temperature": s["temperature"], "num_predict": s["max_tokens"], **job["options"]}
        payload = {"model": job["model"], "messages": job["messages"], "stream": False, "think": False, "options": opts, "keep_alive": "5m"}
        self.current = {"agent": job["agent"], "model": job["model"], "since": time.time()}
        self.bus.publish("llm.started", agent=job["agent"], model=job["model"], waited_s=round(time.time() - job["queued"]))
        t0 = time.time()
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{self.url}/api/chat", json=payload, timeout=aiohttp.ClientTimeout(total=job["timeout"])) as r:
                    data = await r.json()
            if "message" not in data:
                raise GatewayError(data.get("error", "no reply from Ollama"))
            text = (data["message"].get("content") or "").strip()
            if not text:
                raise GatewayError("empty reply (a thinking model? use a non-reasoning model for agents)")
            ms = round((time.time() - t0) * 1000)
            pt, ct = data.get("prompt_eval_count", 0), data.get("eval_count", 0)
            self.store.usage_add(job["agent"], job["model"], pt, ct, ms, job["priority"], True, run_id=job.get("run_id"))
            if job.get("run_id"):
                self.store.trace_add(job["run_id"], "llm", f"{job['agent']} -> {job['model']}", f"{pt + ct} tokens, waited {round(t0 - job['queued'])} s", ms)
            self.bus.publish("llm.done", agent=job["agent"], model=job["model"], ms=ms, tokens=pt + ct)
            if not job["fut"].done():
                job["fut"].set_result({"text": text, "model": job["model"], "tokens": pt + ct, "ms": ms})
        except Exception as e:
            err = e if isinstance(e, GatewayError) else GatewayError(f"{type(e).__name__}: {e}")
            self.store.usage_add(job["agent"], job["model"], 0, 0, round((time.time() - t0) * 1000), job["priority"], False, str(err), run_id=job.get("run_id"))
            if job.get("run_id"):
                self.store.trace_add(job["run_id"], "llm", f"{job['agent']} -> {job['model']}", f"FAILED: {str(err)[:200]}", round((time.time() - t0) * 1000))
            self.bus.publish("llm.error", agent=job["agent"], model=job["model"], error=str(err)[:200])
            if not job["fut"].done():
                job["fut"].set_exception(err)
        finally:
            self.current = None

    def start(self):
        self.task = asyncio.create_task(self.run())
