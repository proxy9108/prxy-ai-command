"""Versioned, validated config (same idea as the Prxy-Bot dashboard): every save is a new version you can roll back to."""
import copy, json, os, re, time
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULTS = {
    "retention_hours": 24,               # how long agent evidence is kept (the 24 h privacy rule)
    "suppress_rejected_days": 30,        # a finding the owner rejected is not raised again for this long
    "tz": "America/Los_Angeles",
    "gateway": {
        "default_model": "llama3.2:3b",  # small enough to sit next to the Discord bot's model in VRAM
        "quiet_hours": "01:00-06:00",    # model swaps (loading a different model) are only allowed in this window
        "timeout_s": 180, "max_tokens": 400, "temperature": 0.2,
        "token_budget_per_agent_day": 20000,
        "council_model": "",             # the model the council debates with ("" = default_model). A bigger one is only loaded in the quiet hours.
    },
    "notify": {                          # push notifications (the webhook address itself lives in the server environment, never in this file)
        "enabled": True,
        "min_severity": "high",          # tell me right away about findings at or above this level
        "escalate_min": [30, 120, 360, 1440],   # minutes of silence before the 1st, 2nd, 3rd, 4th reminder; after that one a day, up to 7
        "mention": "@here",              # added to reminders so your phone buzzes; "" = never mention anyone
        "daily_briefing": True,          # one short message a day with the Commander's headline (also proves the system is alive)
        "resolved": True,                # say when a finding you were told about clears
    },
    "frontier": {                        # an outside model (Claude) that plans, reviews and checks the local team's work. OFF until you turn it on.
        "enabled": False,
        "backend": "auto",               # auto = the API key if there is one, else your Claude subscription through the Claude Code CLI | api | cli
        "model": "claude-sonnet-5-5",    # API model id
        "cli_model": "sonnet",           # CLI model alias (sonnet | opus | haiku)
        "effort": "medium",              # low | medium | high: how hard it thinks (and how many tokens it spends)
        "privacy": "masked",             # masked = outside addresses/MACs/secrets removed; anonymised = internal names and addresses replaced by labels too
        "monthly_budget_usd": 5.0,       # API only: stop calling outside when the estimated spend this month reaches this
        "daily_call_cap": 40,            # both backends: most outside calls per day (protects the bill, or your plan's usage limits)
        "price_in": 2.0, "price_out": 10.0,   # $ per million tokens of `model`, for the budget estimate
        "uses": {"summary": True, "plan": True, "review": True, "verify": True},
    },
    "actions": {                         # letting the team carry out a few approved, reversible actions itself. OFF until you turn it on.
        "enabled": False,
        "allowed": [],                   # which actions may run (names from actionspec.ACTIONS)
        "max_per_day": 3,
        "auto_rollback": True,           # undo an action by itself when the check afterwards fails
    },
    "team": {                            # the team workspace: projects the agents plan and carry out together
        "auto_projects": True,           # the Commander opens an investigation by itself when a disk is about to fill
        "max_auto_per_day": 2,
        "max_steps": 14,                 # most tasks one project may hand out (follow-ups included)
    },
    "advisors": {                        # outside advisors in High Command (keys only in the server env file: OPENAI_API_KEY, GEMINI_API_KEY). OFF until you turn each on.
        "openai": {"enabled": False, "model": "gpt-5-mini", "effort": "low", "monthly_budget_usd": 20.0, "daily_call_cap": 80, "price_in": 0.25, "price_out": 2.0},
        "gemini": {"enabled": False, "model": "gemini-2.5-flash", "effort": "", "monthly_budget_usd": 20.0, "daily_call_cap": 80, "price_in": 0.30, "price_out": 2.5},
        "jobs": {"plan": "claude", "review": "panel", "verify": "openai", "coach": "gemini", "research": "gemini"},   # who does what; "panel" = every advisor in `panel` answers
        "panel": ["claude", "openai", "gemini"],
        "coach_at": "08:00",             # daily coaching: an advisor reads the crew's state and starts read-only investigations; "" = off
        "coach_max_projects": 2,
    },
    "chat": {                            # the crew's break room: short in-character lines built from real events (nothing is invented)
        "enabled": True,
        "voice": True,                   # let the local model rephrase a line in the agent's voice (dropped if it changes a fact); only when the model is already loaded
        "voice_per_hour": 20,
        "standup_at": "07:30",           # daily stand-up in the break room (local time); "" = off
        "checkin_min": 120,              # a squad lead asks one of its workers for a status this often (minutes); 0 = off
        "nudge_counts": [25, 50, 100, 250, 500, 1000, 2500],   # Watchdog asks about a finding nobody decided on when its report count crosses one of these
        "away_hours": [8, 24, 72, 168],  # the crew wonders where the boss is after this long without a visit
        "recap_after_h": 4,              # coming back after this long gets a "while you were gone" recap
        "keep_days": 14,
    },
    "autopilot": {                       # standing permissions: kinds of missions the team may carry out WITHOUT your click (plain code checks every step)
        "enabled": True,                 # the master switch ("pause autopilot")
        "max_per_day": 10,
        "rules": {"diagnose": True, "cleanup": True, "backup_now": False, "restart_service": False, "ct_updates": False},
        "dry_run": True,                 # before a mission that changes things reaches you, run its read-only first steps and let Sage fix the plan from the real output
        "stick_check_h": 24,             # re-check a fixed finding this many hours later; 0 = off
        "cleanup_after_days": 7,         # the team deletes its own restore points this many days after a finished mission; 0 = never
    },
    "agents": {},                        # name -> {enabled, interval_s, model, token_budget, params}
}
AGENT_KEYS = {"enabled": bool, "interval_s": int, "model": str, "token_budget": int, "params": dict}
SEV_LEVELS = ("info", "low", "medium", "high", "critical")
PARAM_SPECS = {}                         # agent name -> {param: {default, min, max, label, quieter, step, rules}}, filled when agents register


def register_params(agent, specs):
    if specs:
        PARAM_SPECS[agent] = specs


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
    full = merge(DEFAULTS, cfg)
    if not isinstance(full["retention_hours"], (int, float)) or not 1 <= full["retention_hours"] <= 720:
        raise ValueError("retention_hours must be 1-720")
    if not isinstance(full["suppress_rejected_days"], (int, float)) or not 0 <= full["suppress_rejected_days"] <= 365:
        raise ValueError("suppress_rejected_days must be 0-365")
    try:
        ZoneInfo(full["tz"])
    except Exception:
        raise ValueError("tz is not a valid time zone name")
    g = full["gateway"]
    if set(g) - set(DEFAULTS["gateway"]):
        raise ValueError(f"unknown gateway keys: {sorted(set(g) - set(DEFAULTS['gateway']))}")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", str(g["quiet_hours"])):
        raise ValueError("quiet_hours must look like 01:00-06:00")
    if not isinstance(g["default_model"], str) or not g["default_model"].strip():
        raise ValueError("gateway.default_model is required")
    for k, lo, hi in (("timeout_s", 10, 900), ("max_tokens", 16, 4096), ("temperature", 0, 2), ("token_budget_per_agent_day", 0, 5_000_000)):
        if not isinstance(g[k], (int, float)) or not lo <= g[k] <= hi:
            raise ValueError(f"gateway.{k} must be {lo}-{hi}")
    if not isinstance(g["council_model"], str):
        raise ValueError("gateway.council_model must be text")
    n = full["notify"]
    if not isinstance(n, dict) or set(n) - set(DEFAULTS["notify"]):
        raise ValueError(f"notify: allowed keys are {sorted(DEFAULTS['notify'])}")
    if n["min_severity"] not in SEV_LEVELS:
        raise ValueError(f"notify.min_severity must be one of {SEV_LEVELS}")
    if not isinstance(n["escalate_min"], list) or not 1 <= len(n["escalate_min"]) <= 8 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0.1 <= x <= 20160 for x in n["escalate_min"]) or n["escalate_min"] != sorted(n["escalate_min"]):
        raise ValueError("notify.escalate_min must be 1-8 increasing numbers of minutes")
    if not isinstance(n["mention"], str) or len(n["mention"]) > 40 or n["mention"] not in ("", "@here", "@everyone") and not re.fullmatch(r"<@&?\d{5,25}>", n["mention"]):
        raise ValueError('notify.mention must be "", "@here", "@everyone" or a Discord mention like <@123456789012345678>')
    for k in ("enabled", "daily_briefing", "resolved"):
        if not isinstance(n[k], bool):
            raise ValueError(f"notify.{k} must be true or false")
    fr = full["frontier"]
    if not isinstance(fr, dict) or set(fr) - set(DEFAULTS["frontier"]):
        raise ValueError(f"frontier: allowed keys are {sorted(DEFAULTS['frontier'])}")
    if not isinstance(fr["enabled"], bool):
        raise ValueError("frontier.enabled must be true or false")
    if fr["backend"] not in ("auto", "api", "cli") or fr["effort"] not in ("low", "medium", "high") or fr["privacy"] not in ("masked", "anonymised"):
        raise ValueError("frontier.backend must be auto/api/cli, effort low/medium/high, privacy masked/anonymised")
    for k in ("model", "cli_model"):
        if not isinstance(fr[k], str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{2,60}", fr[k]):
            raise ValueError(f"frontier.{k} must be a model name")
    for k, lo, hi in (("monthly_budget_usd", 0, 10000), ("daily_call_cap", 0, 1000), ("price_in", 0, 1000), ("price_out", 0, 1000)):
        if isinstance(fr[k], bool) or not isinstance(fr[k], (int, float)) or not lo <= fr[k] <= hi:
            raise ValueError(f"frontier.{k} must be {lo}-{hi}")
    if not isinstance(fr["uses"], dict) or set(fr["uses"]) - {"summary", "plan", "review", "verify"} or any(not isinstance(v, bool) for v in fr["uses"].values()):
        raise ValueError("frontier.uses: summary, plan, review, verify = true/false")
    ac = full["actions"]
    if not isinstance(ac, dict) or set(ac) - set(DEFAULTS["actions"]):
        raise ValueError(f"actions: allowed keys are {sorted(DEFAULTS['actions'])}")
    if not isinstance(ac["enabled"], bool) or not isinstance(ac["auto_rollback"], bool):
        raise ValueError("actions.enabled and actions.auto_rollback must be true or false")
    from .actionspec import ACTIONS as _ACTIONS
    if not isinstance(ac["allowed"], list) or any(a not in _ACTIONS for a in ac["allowed"]):
        raise ValueError("actions.allowed may list guest_start, lxc_mount_backup_flag, backup_job_exclude")
    if isinstance(ac["max_per_day"], bool) or not isinstance(ac["max_per_day"], int) or not 0 <= ac["max_per_day"] <= 20:
        raise ValueError("actions.max_per_day must be a whole number from 0 to 20")
    tm = full["team"]
    if not isinstance(tm, dict) or set(tm) - set(DEFAULTS["team"]):
        raise ValueError(f"team: allowed keys are {sorted(DEFAULTS['team'])}")
    if not isinstance(tm["auto_projects"], bool):
        raise ValueError("team.auto_projects must be true or false")
    for k, lo, hi in (("max_auto_per_day", 0, 20), ("max_steps", 3, 30)):
        if isinstance(tm[k], bool) or not isinstance(tm[k], int) or not lo <= tm[k] <= hi:
            raise ValueError(f"team.{k} must be a whole number from {lo} to {hi}")
    ad = full["advisors"]
    if not isinstance(ad, dict) or set(ad) - set(DEFAULTS["advisors"]):
        raise ValueError(f"advisors: allowed keys are {sorted(DEFAULTS['advisors'])}")
    for prov in ("openai", "gemini"):
        q = ad[prov]
        if not isinstance(q, dict) or set(q) - set(DEFAULTS["advisors"][prov]):
            raise ValueError(f"advisors.{prov}: allowed keys are {sorted(DEFAULTS['advisors'][prov])}")
        if not isinstance(q["enabled"], bool):
            raise ValueError(f"advisors.{prov}.enabled must be true or false")
        if not isinstance(q["model"], str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]{2,80}", q["model"]):
            raise ValueError(f"advisors.{prov}.model must be a model name")
        if q["effort"] not in ("", "minimal", "low", "medium", "high"):
            raise ValueError(f"advisors.{prov}.effort must be empty, minimal, low, medium or high")
        for k, lo, hi in (("monthly_budget_usd", 0, 500), ("price_in", 0, 200), ("price_out", 0, 500)):
            if isinstance(q[k], bool) or not isinstance(q[k], (int, float)) or not lo <= q[k] <= hi:
                raise ValueError(f"advisors.{prov}.{k} must be a number from {lo} to {hi}")
        if isinstance(q["daily_call_cap"], bool) or not isinstance(q["daily_call_cap"], int) or not 0 <= q["daily_call_cap"] <= 2000:
            raise ValueError(f"advisors.{prov}.daily_call_cap must be a whole number from 0 to 2000")
    jobs = ad["jobs"]
    if not isinstance(jobs, dict) or set(jobs) - set(DEFAULTS["advisors"]["jobs"]) or any(v not in ("claude", "openai", "gemini", "panel") for v in jobs.values()):
        raise ValueError("advisors.jobs: plan/review/verify/coach/research = claude, openai, gemini or panel")
    if not isinstance(ad["panel"], list) or not ad["panel"] or any(x not in ("claude", "openai", "gemini") for x in ad["panel"]) or len(set(ad["panel"])) != len(ad["panel"]):
        raise ValueError("advisors.panel must list claude, openai and/or gemini (each once)")
    if not isinstance(ad["coach_at"], str) or (ad["coach_at"] and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ad["coach_at"])):
        raise ValueError('advisors.coach_at must be "HH:MM" or ""')
    if isinstance(ad["coach_max_projects"], bool) or not isinstance(ad["coach_max_projects"], int) or not 0 <= ad["coach_max_projects"] <= 5:
        raise ValueError("advisors.coach_max_projects must be a whole number from 0 to 5")
    ch = full["chat"]
    if not isinstance(ch, dict) or set(ch) - set(DEFAULTS["chat"]):
        raise ValueError(f"chat: allowed keys are {sorted(DEFAULTS['chat'])}")
    for k in ("enabled", "voice"):
        if not isinstance(ch[k], bool):
            raise ValueError(f"chat.{k} must be true or false")
    for k, lo, hi in (("voice_per_hour", 0, 200), ("checkin_min", 0, 1440), ("recap_after_h", 1, 168), ("keep_days", 1, 60)):
        if isinstance(ch[k], bool) or not isinstance(ch[k], int) or not lo <= ch[k] <= hi:
            raise ValueError(f"chat.{k} must be a whole number from {lo} to {hi}")
    if not isinstance(ch["standup_at"], str) or (ch["standup_at"] and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ch["standup_at"])):
        raise ValueError('chat.standup_at must be "HH:MM" or ""')
    for k in ("nudge_counts", "away_hours"):
        v = ch[k]
        if not isinstance(v, list) or len(v) > 12 or any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in v):
            raise ValueError(f"chat.{k} must be a list of up to 12 positive whole numbers")
    ap = full["autopilot"]
    if not isinstance(ap, dict) or set(ap) - set(DEFAULTS["autopilot"]):
        raise ValueError(f"autopilot: allowed keys are {sorted(DEFAULTS['autopilot'])}")
    for k in ("enabled", "dry_run"):
        if not isinstance(ap[k], bool):
            raise ValueError(f"autopilot.{k} must be true or false")
    if not isinstance(ap["rules"], dict) or set(ap["rules"]) - set(DEFAULTS["autopilot"]["rules"]) or any(not isinstance(v, bool) for v in ap["rules"].values()):
        raise ValueError(f"autopilot.rules: {sorted(DEFAULTS['autopilot']['rules'])} each true or false")
    for k, lo, hi in (("max_per_day", 0, 100), ("stick_check_h", 0, 168), ("cleanup_after_days", 0, 90)):
        if isinstance(ap[k], bool) or not isinstance(ap[k], int) or not lo <= ap[k] <= hi:
            raise ValueError(f"autopilot.{k} must be a whole number from {lo} to {hi}")
    if not isinstance(full["agents"], dict):
        raise ValueError("agents must be an object")
    for name, a in full["agents"].items():
        if not isinstance(a, dict) or set(a) - set(AGENT_KEYS):
            raise ValueError(f"agents.{name}: allowed keys are {sorted(AGENT_KEYS)}")
        for k, v in a.items():
            if not isinstance(v, AGENT_KEYS[k]) or isinstance(v, bool) != (AGENT_KEYS[k] is bool):
                raise ValueError(f"agents.{name}.{k} has the wrong type")
        for pk, pv in (a.get("params") or {}).items():
            sp = PARAM_SPECS.get(name, {}).get(pk)
            if sp is None:
                raise ValueError(f"agents.{name}.params.{pk} is not a tunable setting of this agent")
            if isinstance(pv, bool) or not isinstance(pv, (int, float)) or not sp["min"] <= pv <= sp["max"]:
                raise ValueError(f"agents.{name}.params.{pk} must be a number from {sp['min']} to {sp['max']}")
        if "interval_s" in a and not 30 <= a["interval_s"] <= 7 * 86400:
            raise ValueError(f"agents.{name}.interval_s must be 30 seconds to 7 days")
    return cfg


class VersionedConfig:
    def __init__(self, path, store=None):
        self.path = Path(path)
        self.hist = self.path.parent / "history"
        self.store = store
        self.hist.mkdir(parents=True, exist_ok=True)
        self._mtime, self._data = None, {"version": 0}

    def raw(self) -> dict:
        """A copy: callers build the next config from it, and a rejected save must not leave half-edited settings in the live cache."""
        return copy.deepcopy(self._read())

    def _read(self) -> dict:
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return self._data
        if m != self._mtime:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self._mtime, self._data = m, d
            except (OSError, ValueError):
                pass                      # keep the last good copy
        return self._data

    def get(self) -> dict:
        r = self._read()
        return merge(DEFAULTS, {k: v for k, v in r.items() if k in DEFAULTS})

    def version(self) -> int:
        return int(self._read().get("version", 0))

    def save(self, cfg: dict, actor="owner", note=""):
        cfg = validate(cfg)
        v = self.version() + 1
        doc = {**cfg, "version": v, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": str(note)[:200]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        (self.hist / f"v{v:04d}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = None
        if self.store:
            self.store.audit(actor, "config.save", f"v{v}", note)
        return doc

    def history(self):
        out = []
        for f in sorted(self.hist.glob("v*.json"), reverse=True)[:50]:
            try:
                d = json.loads(f.read_text(encoding="utf-8")); out.append({"version": d["version"], "updated_at": d.get("updated_at"), "note": d.get("note", "")})
            except (OSError, ValueError, KeyError):
                pass
        return out

    def rollback(self, version: int, actor="owner"):
        f = self.hist / f"v{int(version):04d}.json"
        old = json.loads(f.read_text(encoding="utf-8"))
        body = {k: v for k, v in old.items() if k in DEFAULTS}
        return self.save(body, actor, f"rollback to v{version}")
