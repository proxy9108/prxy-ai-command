"""The outside model (Claude): a reviewer and planner for the local team, never an actor.

The local models gather facts and do routine work; the hard parts (planning an investigation, writing the summary, checking the team's proposed steps, checking
the result of an action) can be sent to Claude. Two ways to reach it, chosen in Settings:
  api  the Anthropic API with a key from the server environment (ANTHROPIC_API_KEY): pay per token, capped by a monthly budget you set
  cli  your Claude subscription through the official Claude Code CLI (`claude -p`), logged in once inside this container: no per-token bill, but the calls count
       against your plan's usage limits, so they are capped per day. The CLI is started with every tool, plugin, slash command and MCP server switched off:
       it can only answer text, it cannot read a file or run a command.
What leaves this machine: a short digest the team already wrote, after a filter that removes outside addresses, MAC addresses and anything that looks like a secret
(and, in 'anonymised' mode, replaces internal addresses and guest names by labels that are put back in the answer). Everything sent and received is kept for 14
days on the Settings page. Nothing is sent unless it is switched on, the budget and the daily cap are respected, and when anything fails the local path is used.
Claude only ever returns text; it is given no credential, no shell and no way to act."""
import asyncio, json, os, re, shutil, tempfile, time
from datetime import datetime

from .redact import scrub

SECRET = re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization)\b\s*[=:]\s*\S+|\bbearer\s+\S{12,}|\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")
PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
PURPOSES = ("summary", "plan", "review", "verify")


class FrontierError(Exception):
    pass


def extract_json(text):
    """The first JSON object in a reply (models like to wrap it in a sentence or a code fence), or None."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i = t.find("{")
    while i != -1:
        depth = 0
        for j in range(i, len(t)):
            depth += t[j] == "{"
            depth -= t[j] == "}"
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except ValueError:
                    break
        i = t.find("{", i + 1)
    return None


class Frontier:
    def __init__(self, cfg, store, bus=None, api_key=None, api_base=None, cli_path=None):
        self.cfg, self.store, self.bus = cfg, store, bus
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self.api_base = api_base or None
        self.cli_path = cli_path if cli_path is not None else (os.getenv("CLAUDE_CLI") or shutil.which("claude") or "")
        self.sem = asyncio.Semaphore(1)                 # one outside call at a time: gentle on rate limits and on the plan
        self.last_error = None

    # ------------------------------------------------------------------ state
    def policy(self):
        return self.cfg.get()["frontier"]

    def backend(self):
        p = self.policy()
        if p["backend"] == "api":
            return "api" if self.api_key else None
        if p["backend"] == "cli":
            return "cli" if self.cli_path and os.path.exists(self.cli_path) else None
        if self.api_key:
            return "api"
        return "cli" if self.cli_path and os.path.exists(self.cli_path) else None

    def _month_start(self):
        n = datetime.now()
        return datetime(n.year, n.month, 1).timestamp()

    def _day_start(self):
        n = datetime.now()
        return datetime(n.year, n.month, n.day).timestamp()

    def why_not(self, purpose=None):
        """None when a call is allowed right now, else the reason in plain words."""
        p = self.policy()
        if not p["enabled"]:
            return "the outside model is switched off"
        if purpose and purpose != "test" and not p["uses"].get(purpose, False):
            return f"'{purpose}' is switched off"
        b = self.backend()
        if not b:
            return "no API key is set and the Claude CLI is not installed"
        if self._claude_calls(self._day_start()) >= p["daily_call_cap"]:                  # Claude's own calls only (OpenAI/Gemini have their own caps)
            return "the daily call cap is reached"
        if b == "api" and self.store.frontier_stats(self._month_start(), "api")["cost"] >= p["monthly_budget_usd"]:
            return "the monthly budget is used up"
        return None

    def available(self, purpose=None):
        return self.why_not(purpose) is None

    def _claude_calls(self, since):
        return self.store.frontier_stats(since, "api")["n"] + self.store.frontier_stats(since, "cli")["n"]

    def status(self):
        p, ms, ds = self.policy(), self._month_start(), self._day_start()
        api, cli = self.store.frontier_stats(ms, "api"), self.store.frontier_stats(ms, "cli")
        return {"enabled": p["enabled"], "backend": self.backend(), "api_key_set": bool(self.api_key), "cli_found": bool(self.cli_path and os.path.exists(self.cli_path)),
                "model": p["model"] if self.backend() == "api" else p["cli_model"], "month": {"api_cost_usd": round(api["cost"], 4), "api_calls": api["n"], "cli_calls": cli["n"], "budget_usd": p["monthly_budget_usd"]},
                "today_calls": self._claude_calls(ds), "daily_cap": p["daily_call_cap"], "blocked_because": self.why_not(), "last_error": self.last_error, "privacy": p["privacy"], "policy": p}

    # ------------------------------------------------------------------ what leaves this machine
    def prepare(self, text):
        """-> (clean text, {label: original}). Outside addresses and MACs masked, secret-looking strings removed; anonymised mode replaces internal names too."""
        t = scrub(text or "")
        t = SECRET.sub("[removed]", t)
        mapping = {}
        if self.policy()["privacy"] == "anonymised":
            names = []
            base = (self.store.kv_get("config_drift:baseline") or {}).get("items", {})
            for c in base.values():
                nm = str(c.get("hostname") or c.get("name") or "")
                if len(nm) >= 3 and nm not in names:
                    names.append(nm)
            for i, nm in enumerate(sorted(names, key=len, reverse=True)):
                if re.search(r"(?<![\w-])" + re.escape(nm) + r"(?![\w-])", t):
                    lab = f"machine-{i + 1}"; mapping[lab] = nm
                    t = re.sub(r"(?<![\w-])" + re.escape(nm) + r"(?![\w-])", lab, t)
            for i, ip in enumerate(dict.fromkeys(PRIVATE_IP.findall(t))):
                lab = f"address-{chr(65 + i % 26)}{i // 26 or ''}"; mapping[lab] = ip
                t = t.replace(ip, lab)
        return t, mapping

    @staticmethod
    def restore(text, mapping):
        for lab, orig in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(lab, orig)
        return text

    # ------------------------------------------------------------------ the two backends
    async def _api(self, system, user, max_tokens):
        import anthropic
        p = self.policy()
        client = anthropic.AsyncAnthropic(api_key=self.api_key, base_url=self.api_base, max_retries=1, timeout=90.0)
        try:
            r = await client.messages.create(model=p["model"], max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}], output_config={"effort": p["effort"]})
        except anthropic.AuthenticationError:
            raise FrontierError("the API key was refused")
        except anthropic.RateLimitError:
            raise FrontierError("rate limited by the API")
        except anthropic.APIStatusError as e:
            raise FrontierError(f"the API answered HTTP {e.status_code}")
        except anthropic.APIConnectionError:
            raise FrontierError("could not reach the API")
        finally:
            await client.close()
        if r.stop_reason == "refusal":
            raise FrontierError("the model declined this request")
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        tin = r.usage.input_tokens + (getattr(r.usage, "cache_creation_input_tokens", 0) or 0) + (getattr(r.usage, "cache_read_input_tokens", 0) or 0)
        cost = (tin * p["price_in"] + r.usage.output_tokens * p["price_out"]) / 1e6
        return text, p["model"], tin, r.usage.output_tokens, cost

    async def _cli(self, system, user, max_tokens):
        p = self.policy()
        cwd = tempfile.mkdtemp(prefix="prxy-claude-")                     # an empty directory: even a misbehaving CLI would find nothing to read
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME") and k}
        env["HOME"] = os.getenv("CLAUDE_HOME") or env.get("HOME") or "/var/lib/prxy-command"
        args = [self.cli_path, "-p", "--model", p["cli_model"], "--effort", p["effort"], "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                "--setting-sources", "", "--exclude-dynamic-system-prompt-sections", "--output-format", "json", "--max-budget-usd", "1", "--system-prompt", system]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env)
            try:
                out, err = await asyncio.wait_for(proc.communicate(user.encode()), 170)
            except asyncio.TimeoutError:
                proc.kill()
                raise FrontierError("the Claude CLI took too long")
        except OSError as e:
            raise FrontierError(f"could not start the Claude CLI: {e.strerror}")
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        try:
            d = json.loads(out.decode(errors="replace"))
        except ValueError:
            raise FrontierError("the Claude CLI did not answer (is it logged in? run it once as the service user)" if proc.returncode else "the Claude CLI returned something unreadable")
        if d.get("is_error") or d.get("subtype", "").startswith("error"):
            raise FrontierError(("the Claude CLI reported: " + str(d.get("result", ""))[:160]) or "the Claude CLI reported an error")
        u = d.get("usage") or {}
        tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        return str(d.get("result", "")).strip(), p["cli_model"], tin, u.get("output_tokens") or 0, 0.0        # a subscription has no per-token bill

    # ------------------------------------------------------------------ the one entry point
    async def ask(self, purpose, system, user, max_tokens=1200):
        """-> {'text', 'backend', 'model'}; raises FrontierError (callers fall back to the local path). Everything sent and received is logged."""
        why = self.why_not(purpose)
        if why:
            raise FrontierError(why)
        b = self.backend()
        csys, _ = self.prepare(system)
        cuser, mapping = self.prepare(user)
        t0 = time.time()
        async with self.sem:
            try:
                text, model, tin, tout, cost = await (self._api if b == "api" else self._cli)(csys, cuser, max_tokens)
                if not text:
                    raise FrontierError("an empty answer")
            except FrontierError as e:
                self.last_error = str(e)
                self.store.frontier_add(purpose, b, self.policy()["model" if b == "api" else "cli_model"], 0, 0, 0, False, str(e), f"{csys}\n---\n{cuser}", "")
                self.store.audit("system", "frontier.failed", purpose, str(e)[:150])
                raise
        self.last_error = None
        reply = self.restore(text, mapping)
        self.store.frontier_add(purpose, b, model, tin, tout, cost, True, None, f"{csys}\n---\n{cuser}", text)
        self.store.audit("system", "frontier.call", purpose, f"{b} {model}: {len(cuser)} chars sent, {tin}+{tout} tokens, {time.time() - t0:.1f}s" + (f", ~${cost:.4f}" if cost else ""))
        if self.bus:
            self.bus.publish("frontier.call", agent="commander", purpose=purpose, backend=b)
        return {"text": reply, "backend": b, "model": model}
