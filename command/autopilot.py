"""Autopilot: kinds of missions the owner allows the team to carry out WITHOUT a click (standing permissions).

The AI never decides what counts: plain code checks EVERY step of the plan against the rule's exact command patterns, plus the usual safety
checks (block-list, targets, syntax, every change has an undo) and the reviewers' verdicts. If any step does not fit, the mission simply waits
for the owner's approval as before. Everything that runs on autopilot is signed, journaled, audited ("autopilot" as the actor) and listed on the
Autopilot page; the owner can pause it with one switch, and the runner's kill switch still stops everything."""
import re, time

from .missions import RULES, local_review, owner_review

SELF_CT = 139


def _ro(cmd):
    return RULES.readonly_ok(cmd) is None


def _lxc(t):
    m = re.fullmatch(r"lxc:(\d+)", t or "")
    return int(m.group(1)) if m else None


CLEAN_RUN = re.compile(r"^if pct listsnapshot (\d+) \| grep -qw prxy-m(\d+); then pct delsnapshot \1 prxy-m\2; fi$|^rm -f /root/backups/prxy-mission-\d+-etc\.tgz$")
BACKUP_RUN = re.compile(r"^vzdump (\d+)( --(storage|mode|compress|notes-template) [\w.:/{}-]+)*$")
RESTART_RUN = re.compile(r"^systemctl (restart|reload|start|reset-failed) [\w@.:-]+(\.service|\.timer|\.socket)?$")
UPDATE_RUN = re.compile(r"^(DEBIAN_FRONTEND=noninteractive )?apt-get( -q+)?( -y)? (update|upgrade|full-upgrade|--only-upgrade install( [\w.+:-]+)+)( -y)?( -o Dpkg::Options::=--force-conf(old|def))*$")


def _fits_change(s, rule):
    """Does a step that CHANGES something fit this rule exactly?"""
    run, t = s["run"].strip(), s["target"]
    ct = _lxc(t)
    if rule == "cleanup":
        return t == "host" and bool(CLEAN_RUN.match(run))
    if rule == "backup_now":
        return t == "host" and bool(BACKUP_RUN.match(run))
    if rule == "restart_service":
        return ct is not None and ct != SELF_CT and bool(RESTART_RUN.match(run))
    if rule == "ct_updates":
        return ct is not None and ct != SELF_CT and bool(UPDATE_RUN.match(run)) and ("-y" in run or " update" in run)
    return False


RULESET = [
    {"key": "diagnose", "label": "Read-only diagnosis", "default": True, "changes": False,
     "may": "Run commands that only read (logs, status, settings, ports) on the host and the containers, to find out what is wrong.",
     "never": "Anything that writes, restarts, installs or deletes; reading secret files or environment variables. A strict list of reading commands decides, not the AI."},
    {"key": "cleanup", "label": "Clean up the team's own restore points", "default": True, "changes": True,
     "may": "Delete the snapshots (prxy-m…) and /etc copies the team made before its own missions, once the mission is finished and older than the set number of days.",
     "never": "Touch any other snapshot or backup; restore points of a mission whose undo failed are always kept."},
    {"key": "backup_now", "label": "Back up a guest now", "default": False, "changes": True,
     "may": "Start an extra backup of a guest (vzdump to your backup storage), e.g. after a failed nightly backup.",
     "never": "Prune or delete backups, or change the backup job."},
    {"key": "restart_service", "label": "Restart a service in a container", "default": False, "changes": True,
     "may": "Start, restart or reload one service inside a container when it is down or stuck (the container is snapshotted first).",
     "never": "Restart services on the Proxmox host or in Prxy-AI Command's own container."},
    {"key": "ct_updates", "label": "Package updates in containers", "default": False, "changes": True,
     "may": "apt-get update / upgrade inside containers (the container is snapshotted first and the upgrade undone if a check fails).",
     "never": "Update the Proxmox host itself, install new packages or remove any."},
]
RULE_KEYS = [r["key"] for r in RULESET]


class Autopilot:
    def __init__(self, missions):
        self.ms, self.store, self.cfg = missions, missions.store, missions.rt.cfg

    def policy(self):
        return self.cfg.get()["autopilot"]

    def today(self, now=None):
        day = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
        k = self.store.kv_get("autopilot:day") or {}
        return k.get("n", 0) if k.get("d") == day else 0

    def _count(self):
        day = time.strftime("%Y-%m-%d")
        n = self.today()
        self.store.kv_set("autopilot:day", {"d": day, "n": n + 1})

    def rule_for(self, m):
        """-> (rule key or None, reason in plain words)."""
        p = self.policy()
        plan = m.get("plan") or {}
        steps = plan.get("steps") or []
        if not p["enabled"]:
            return None, "Autopilot is paused"
        if self.today() >= p["max_per_day"]:
            return None, f"today's autopilot limit ({p['max_per_day']}) is reached"
        if m["status"] != "ready" or not steps:
            return None, "not ready"
        problems, _ = local_review(steps, self.ms.allowed_cts())
        if problems or owner_review(plan.get("owner_steps") or [], self.ms.allowed_cts()):
            return None, "the safety checks found problems"
        if any(o["when"] == "before" for o in plan.get("owner_steps") or []):
            return None, "you have to do something first"
        panel = (m.get("review") or {}).get("panel") or []
        if any(x["verdict"] == "disagree" for x in panel):
            return None, "a reviewer disagrees"
        changing = [s for s in steps if not (_ro(s["run"]) and _ro(s["check"]))]
        if any(not _ro(s["check"]) for s in steps):
            return None, "a check command is not read-only"
        if not changing:
            return ("diagnose", "every step only reads") if p["rules"].get("diagnose") else (None, "read-only diagnosis is not allowed on autopilot")
        if plan.get("risk") != "low":
            return None, f"risk is {plan.get('risk')}, autopilot only takes low-risk changes"
        if any(x["verdict"] == "caution" for x in panel):
            return None, "a reviewer said go carefully"
        for key in ("cleanup", "backup_now", "restart_service", "ct_updates"):
            if all(_fits_change(s, key) for s in changing):
                if p["rules"].get(key):
                    return key, f"every changing step fits '{self.label(key)}'"
                return None, f"'{self.label(key)}' is not allowed on autopilot"
        return None, "it changes things no autopilot rule covers"

    @staticmethod
    def label(key):
        return next((r["label"] for r in RULESET if r["key"] == key), key)

    async def consider(self, mid):
        """Called when a mission becomes ready. Runs it if a standing permission covers it; otherwise it waits for the owner as before."""
        m = self.store.mission_get(mid)
        if not m:
            return None
        rule, why = self.rule_for(m)
        if not rule:
            if m["status"] == "ready":
                self.ms.trail(mid, "commander", f"Waiting for you (autopilot: {why}).", "note")
            return None
        self._count()
        self.ms.trail(mid, "commander", f"Autopilot: you allowed '{self.label(rule)}' without a click, and {why}. Carrying it out now.", "auto")
        try:
            await self.ms.approve(mid, ack=True, auto=rule)
        except ValueError as e:
            self.ms.trail(mid, "commander", f"Autopilot could not start it: {e}. It waits for you.", "problems")
            return None
        return rule

    def status(self):
        p = self.policy()
        log = []
        for m in self.store.mission_list(200):
            if m.get("auto_rule"):
                log.append({"id": m["id"], "ticket": m["ticket"], "title": m["title"], "status": m["status"], "rule": m["auto_rule"], "rule_label": self.label(m["auto_rule"]),
                            "approved_at": m.get("approved_at"), "stick": (m.get("result") or {}).get("stick"), "error": (m.get("result") or {}).get("error")})
        sticks = [{"ticket": f"PRX-{x['mid']:04d}", "due": x["due"], "title": x.get("title", "")} for x in (self.store.kv_get("missions:stick") or [])]
        return {"enabled": p["enabled"], "max_per_day": p["max_per_day"], "today": self.today(), "dry_run": p["dry_run"], "stick_check_h": p["stick_check_h"],
                "cleanup_after_days": p["cleanup_after_days"],
                "rules": [{**{k: r[k] for k in ("key", "label", "may", "never", "changes")}, "on": bool(p["rules"].get(r["key"]))} for r in RULESET],
                "log": log[:40], "stick_pending": sticks}
