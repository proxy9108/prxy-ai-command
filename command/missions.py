"""Missions: the AI team plans a job, you approve it once, the team carries it out.

  1. PLAN     the local crew gathers the facts (matching findings, the target machines' settings, your notes); the advisor on the 'plan' job (Sage by default)
              writes the mission: goal, why, risk, and a MOP = numbered steps, each with WHERE it runs (the host or a container), the EXACT command, a CHECK
              command with what it must show, and a ROLLBACK command
  2. REVIEW   the same block-list the runner enforces (loaded from tools/runner/prxy-runner.py), target checks, "every change has an undo", and the advisor
              panel's second opinions. Hard problems send it back to the planner once with the problems listed; what remains is shown to you
  3. APPROVE  you read the exact steps and approve once. The text is hashed and signed: the runner refuses anything else, and nothing can be added later
  4. EXECUTE  the runner on the Proxmox host makes restore points (container snapshots, a copy of /etc), runs each step, checks it, and on a failure undoes the
              completed steps in reverse. Progress is shown live
  5. REPORT   what ran, the output of every step and check, and the 'verify' advisor's reading of the result. A finding the mission was about is closed
Nothing runs without your approval, and the runner is a separate service you installed yourself (tools/install-runner.sh)."""
import asyncio, hashlib, hmac, importlib.util, json, os, re, subprocess, time
from pathlib import Path

import aiohttp

from .frontier import FrontierError, extract_json
from .redact import scrub

ROOT = Path(__file__).resolve().parent.parent
MAX_STEPS = 12
WRITE_VERBS = re.compile(r"(?:^|[\s;|&(])\d?>>?\s*(?!/dev/null|&\d)[\w/~$\"'.]|"          # a real redirect into a file (not 2>/dev/null, not awk's NR>1)
                         r"\b(apt(-get)?\s+(install|remove|purge|full-upgrade|upgrade|dist-upgrade)|systemctl\s+(enable|disable|restart|start|stop|reload)|sed\s+-i|tee\b|cp\b|mv\b|"
                         r"mkdir\b|chmod\b|chown\b|useradd\b|pip\s+install|docker\s+(run|rm|stop|compose)|pvesh\s+(set|create)|pct\s+set|ln\s+-s|install\s+-m)", re.I)


def _runner_rules():
    spec = importlib.util.spec_from_file_location("prxy_runner_rules", ROOT / "tools" / "runner" / "prxy-runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RULES = _runner_rules()


def plural_s(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def ticket(mid):
    return f"PRX-{int(mid):04d}"


def run_id(m):
    """What the runner knows this attempt as: each attempt of a ticket is a separate, signed, run-once job."""
    return int(m["id"]) * 100 + int(m.get("attempt") or 1)


def canonical(m):
    return json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mission_hash(mid, goal, steps):
    return hashlib.sha256(canonical({"id": mid, "goal": goal, "steps": steps}).encode()).hexdigest()


def clean_steps(raw):
    """The planner's steps -> the runner's format, or raises ValueError."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("the plan has no steps")
    out = []
    for i, s in enumerate(raw[:MAX_STEPS], 1):
        if not isinstance(s, dict):
            raise ValueError(f"step {i} is not an object")
        target = str(s.get("target", "")).strip().lower().replace("lxc/", "lxc:").replace("ct:", "lxc:")
        if target in ("proxmox", "pve", "node", "hypervisor"):
            target = "host"
        exp = str(s.get("expect") or "exit0")
        if not (exp == "exit0" or exp.startswith(("contains:", "absent:"))):
            exp = "exit0"
        try:
            timeout = max(10, min(int(s.get("timeout") or 300), 3600))
        except (TypeError, ValueError):
            timeout = 300
        out.append({"n": i, "title": str(s.get("title") or f"Step {i}")[:140], "target": target, "run": str(s.get("run") or "").strip()[:2000],
                    "check": str(s.get("check") or "").strip()[:1000], "expect": exp[:200], "rollback": str(s.get("rollback") or "").strip()[:2000], "timeout": timeout})
    return out


OWNER_MAX = 5


def clean_owner_steps(raw):
    """The planner's 'Your part' list -> [{i, when, what, why, check, target, expect}]. Only 'before' (the owner does it before the team starts) and 'after'."""
    out = []
    for s in (raw if isinstance(raw, list) else [])[:OWNER_MAX]:
        if not isinstance(s, dict) or not str(s.get("what") or "").strip():
            continue
        when = "before" if str(s.get("when") or "").strip().lower() == "before" else "after"
        target = str(s.get("target") or "host").strip().lower().replace("lxc/", "lxc:").replace("ct:", "lxc:")
        if target in ("proxmox", "pve", "node", "hypervisor", ""):
            target = "host"
        exp = str(s.get("expect") or "exit0")
        if not (exp == "exit0" or exp.startswith(("contains:", "absent:"))):
            exp = "exit0"
        out.append({"i": len(out) + 1, "when": when, "what": scrub(str(s["what"]).strip())[:500], "why": scrub(str(s.get("why") or "").strip())[:300],
                    "check": str(s.get("check") or "").strip()[:1000], "target": target, "expect": exp[:200]})
    return out


def owner_review(osteps, allowed_cts=None):
    """Hard problems in 'Your part': the team's check of what the owner did must be a valid, read-only command."""
    problems = []
    for o in osteps:
        if not o["check"]:
            continue
        if o["target"] != "host" and not re.fullmatch(r"lxc:\d+", o["target"]):
            problems.append(f"your part {o['i']}: target '{o['target']}' must be 'host' or 'lxc:<id>'")
        elif allowed_cts is not None and o["target"] != "host" and int(o["target"].split(":")[1]) not in allowed_cts:
            problems.append(f"your part {o['i']}: container {o['target'].split(':')[1]} is not one missions may work in")
        why = RULES.blocked(o["check"])
        if why:
            problems.append(f"your part {o['i']}: the check is refused ({why})")
        err = syntax_error(o["check"])
        if err:
            problems.append(f"your part {o['i']}: the check has a shell syntax error ({err})")
        if WRITE_VERBS.search(o["check"]):
            problems.append(f"your part {o['i']}: the team's check of your part must only read, not change anything")
    return problems


def owner_hash(osteps):
    return hashlib.sha256(canonical(osteps or []).encode()).hexdigest()


def syntax_error(cmd):
    """bash -n: parses the command without running anything. -> the error text, or None."""
    if not (cmd or "").strip():
        return None
    try:
        p = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return re.sub(r"^bash: -c: line \d+: ", "", (p.stderr or "").strip().splitlines()[0])[:160] if p.returncode else None


def local_review(steps, allowed_cts=None):
    """Hard problems (must be fixed) and concerns (shown to the owner)."""
    problems, concerns = [], []
    for s in steps:
        if s["target"] != "host" and not re.fullmatch(r"lxc:\d+", s["target"]):
            problems.append(f"step {s['n']}: target '{s['target']}' must be 'host' or 'lxc:<id>'")
        elif allowed_cts is not None and s["target"] != "host" and int(s["target"].split(":")[1]) not in allowed_cts:
            problems.append(f"step {s['n']}: container {s['target'].split(':')[1]} is not one missions may work in")
        for k in ("run", "check", "rollback"):
            why = RULES.blocked(s[k])
            if why:
                problems.append(f"step {s['n']}: the {k} command is refused ({why})")
            err = syntax_error(s[k])
            if err:
                problems.append(f"step {s['n']}: the {k} command has a shell syntax error ({err})")
        if not s["run"]:
            problems.append(f"step {s['n']}: nothing to run")
        if not s["check"]:
            problems.append(f"step {s['n']}: no check command (how do we know it worked?)")
        if WRITE_VERBS.search(s["run"]) and not s["rollback"]:
            problems.append(f"step {s['n']}: it changes something but has no rollback")
        if re.search(r"\b(apt|apt-get)\s+(install|upgrade|full-upgrade|dist-upgrade)\b", s["run"]) and not re.search(r"-y\b|--yes|DEBIAN_FRONTEND", s["run"]):
            problems.append(f"step {s['n']}: apt must run non-interactively (-y)")
        if s["target"] == "host":
            concerns.append(f"step {s['n']} runs on the Proxmox host itself")
    return problems, concerns


class Missions:
    def __init__(self, runner, advisors=None, crew=None, url=None, secret=None, http=None):
        self.rt, self.store, self.bus, self.adv, self.crew = runner, runner.store, runner.bus, advisors, crew
        self.url = (url if url is not None else os.getenv("RUNNER_URL", "")).rstrip("/")
        self.secret = (secret if secret is not None else os.getenv("RUNNER_SECRET", "")).encode()
        self.http = http                        # tests inject a fake: async (method, path, body, headers) -> (status, json)
        self.tasks = {}
        self.autopilot = None                    # set by the server (command/autopilot.py)

    def ap_policy(self):
        try:
            return self.rt.cfg.get()["autopilot"]
        except Exception:
            return {"enabled": False, "rules": {}, "dry_run": False, "stick_check_h": 0, "cleanup_after_days": 0, "max_per_day": 0}

    def configured(self):
        return bool((self.url or self.http) and self.secret)

    def allowed_cts(self):
        base = (self.store.kv_get("config_drift:baseline") or {}).get("items", {})
        return {int(g.split("/")[1]) for g in base if g.startswith("lxc/")} or None

    # ------------------------------------------------------------------ 1. plan
    def facts(self, goal, ref=""):
        from .intent import match_findings
        from .team import targets_of
        targets = targets_of(goal, self.store)
        fs = match_findings(goal, self.store.open_findings(500), targets)
        if ref.startswith("finding:"):
            f = self.store.get(ref.split(":", 1)[1])
            if f and f not in fs:
                fs = [f] + fs
        base = (self.store.kv_get("config_drift:baseline") or {}).get("items", {})
        guests = [g for g, _ in targets[1]]
        cfgs = [f"{g}: " + ", ".join(f"{k}={v}" for k, v in base.get(g, {}).items() if k in ("hostname", "name", "memory", "cores", "rootfs", "mp0", "mp1", "net0", "unprivileged", "features"))[:400] for g in guests]
        lxcs = sorted(f"{g.split('/')[1]}={c.get('hostname', '')}" for g, c in base.items() if g.startswith("lxc/"))
        notes = [n["text"] for n in (self.store.kv_get("owner:notes") or []) if time.time() - n["ts"] < 30 * 86400][-8:]
        known = [f"{k['ticket']}: {k['text']}" for k in self.knowledge()][-15:]
        lines = [f"- [{f['severity']}] {f['title']} (from {f['agent']}): {scrub(f['summary'])[:300]}" + (f" Proposed steps: {' / '.join((f.get('proposed_action') or {}).get('steps', []))[:300]}" if f.get("proposed_action") else "")
                 for f in fs[:8]]
        return ("What the crew knows:\n" + ("\n".join(lines) or "- no matching findings") +
                "\n\nSettings of the machines named:\n" + ("\n".join(cfgs) or "- none named") +
                "\n\nContainers (id=hostname): " + ", ".join(lxcs) +
                "\n\nThe owner's own notes: " + (" | ".join(notes) or "none") +
                "\n\nWhat earlier missions found out (do not re-check what is already known):\n" + ("\n".join(f"- {k}" for k in known) or "- nothing yet") +
                "\n\nThe Proxmox host runs Proxmox VE 9 on Debian 13. Containers are mostly Debian 12 or 13 LXC. Commands run as root. Address a container as lxc:<id>.")

    def system_map(self, goal, limit=6000):
        """What Claude (the mission leader) knows about the whole lab: the build notes, reduced to the guest table and the parts that match the goal."""
        docs = Path(os.getenv("CMD_DATA", "/var/lib/prxy-command")) / "docs"
        if self.rt and getattr(self.rt, "data_dir", None):
            docs = Path(self.rt.data_dir) / "docs"
        text = ""
        for name in ("HOMELAB_HANDOFF.md", "PRXY_AI_COMMAND_HANDOFF.md"):
            p = docs / name
            if p.exists():
                text += "\n" + p.read_text(errors="replace")
        if not text:
            return ""
        lines = text.splitlines()
        table = [l for l in lines if re.match(r"^\|\s*\.?1\d\d\s*\|", l)][:45]
        words = {w for w in re.findall(r"[a-z0-9][a-z0-9._/-]{3,}", goal.lower())} - {"make", "sure", "every", "that", "with", "then", "only", "fix", "this", "from"}
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[-#*] )", text) if 40 < len(p.strip()) < 1500]
        scored = sorted(paras, key=lambda p: -sum(p.lower().count(w) for w in words))
        picked, n = [], sum(len(l) for l in table)
        for p in scored[:12]:
            if n + len(p) > limit:
                continue
            picked.append(p); n += len(p)
        return "Guests (from the build notes):\n" + "\n".join(table) + "\n\nBuild notes that match this mission:\n" + "\n---\n".join(picked)

    SYSTEM = ("You are Sage, the planner of an AI operations team for a home lab (one Proxmox host with LXC containers). Write a MISSION the team will carry out after the "
              "owner approves it: exact shell commands, run as root, non-interactive. Reply with ONE JSON object only:\n"
              "{\"title\": \"short\", \"goal\": \"what will be true when done\", \"why\": \"1-2 sentences\", \"risk\": \"low|medium|high\", \"downtime\": \"none or what and how long\", "
              "\"reasoning\": [\"3 to 6 short points: how you approached it, what you checked in the facts, what you chose not to do and why\"], "
              "\"steps\": [{\"title\": \"...\", \"target\": \"host\" or \"lxc:<id>\", \"run\": \"exact command\", \"check\": \"command that proves it worked\", "
              "\"expect\": \"exit0\" or \"contains:<text>\" or \"absent:<text>\", \"rollback\": \"exact command that undoes this step, or empty if it changes nothing\", \"timeout\": seconds}], "
              "\"owner_steps\": [{\"when\": \"before\" or \"after\", \"what\": \"exactly what the owner does by hand (menu path and clicks, or the command to type)\", \"why\": \"one sentence\", "
              "\"check\": \"a READ-ONLY command the team runs afterwards to confirm it was done, or empty\", \"target\": \"host\" or \"lxc:<id>\", \"expect\": \"exit0\" or \"contains:<text>\" or \"absent:<text>\"}]}\n"
              "owner_steps ('Your part') lists ONLY what the team cannot do itself: settings in another system's web interface (OPNsense, the router, a switch), cables or hardware, "
              "typing a password or secret, or a host reboot (the owner does it in quiet hours 01:00-06:00 after checking that night's backups). Use [] when the team can do everything. "
              "'before' = the owner does it before the team starts; 'after' = after the team's steps. The ticket stays open until the team's check of every 'after' part passes.\n"
              "Rules: at most 10 steps; start with a read-only step that confirms the starting state; back up any file before editing it (cp file file.prxy-bak) and restore it in the rollback; "
              "apt with -y and DEBIAN_FRONTEND=noninteractive; never reboot or power off the host; never touch the host network, cluster, users or storage layout; never delete guests or volumes; "
              "no pipes from curl/wget into a shell; prefer small, reversible steps; restore points (container snapshots and a copy of /etc) are made automatically before step 1. "
              "Every command runs as `bash -c '<command>'`: quote and escape for that (e.g. find needs \\( \\) around grouped tests), and every command is syntax-checked before approval. "
              "Use only the facts given; if the facts are not enough to plan safely, make the first steps read-only checks and say so in 'why'.")

    def trail(self, mid, who, text, kind="note"):
        """One line of the planning trail shown on the card (who did what, and why)."""
        m = self.store.mission_get(mid)
        t = (m.get("trail") or [])[-80:] + [{"ts": time.time(), "who": who, "kind": kind, "text": scrub(str(text))[:900], "attempt": m.get("attempt") or 1}]
        self.store.mission_update(mid, trail=t)
        self.bus.publish("mission.trail", agent=who if who in ("claude", "openai", "gemini") else "commander", id=mid)

    def create(self, goal, origin="owner", ref=""):
        goal = re.sub(r"\s+", " ", str(goal or "")).strip()
        if len(goal) < 10:
            raise ValueError("describe the mission in a sentence")
        mid = self.store.mission_add(goal[:120], goal, origin, ref)
        self.store.audit("owner" if origin == "owner" else f"agent:{origin}", "mission.create", ticket(mid), goal[:150])
        self.bus.publish("mission.created", agent="commander", id=mid, title=goal[:120])
        self.tasks[mid] = asyncio.create_task(self._plan(mid))
        return mid

    async def _plan(self, mid, feedback=None):
        m = self.store.mission_get(mid)
        try:
            await self.plan(m, feedback)
        except Exception as e:
            self.store.mission_update(mid, status="plan_failed", review={"problems": [f"{type(e).__name__}: {str(e)[:200]}"], "concerns": [], "panel": []})
        m = self.store.mission_get(mid)
        rule = None
        if m["status"] == "ready" and self.autopilot and self.configured():
            try:
                rule = await self.autopilot.consider(mid)
            except Exception as e:
                self.trail(mid, "commander", f"Autopilot error: {type(e).__name__}: {str(e)[:150]}. It waits for you.", "problems")
            m = self.store.mission_get(mid)
        self.bus.publish("mission.planned", agent="claude", id=mid, ticket=ticket(mid), attempt=m["attempt"], title=m["title"], status=m["status"],
                         steps=len((m.get("plan") or {}).get("steps") or []), auto=rule)

    async def plan(self, m, feedback=None, retry=True, lead_round=True):
        if not self.adv or not self.adv.ready("plan"):
            self.store.mission_update(m["id"], status="plan_failed", review={"problems": ["no outside advisor is available to plan missions (Settings > Outside advisors)"], "concerns": [], "panel": []})
            return
        facts, smap = self.facts(m["goal"], m["ref"]), self.system_map(m["goal"])
        nf = facts.split("\n\nSettings")[0].count("\n- [")
        self.trail(m["id"], "commander", f"The crew gathered the facts: {nf} matching finding(s), the settings of the machines named, the container list, your notes" + (", and the matching parts of the build notes (system map)" if smap else "") + ".", "facts")
        self.trail(m["id"], "claude", ("Replanning to address: " + feedback) if feedback else "Writing the plan.", "thinking")
        user = f"Mission request: {m['goal']}\n\n{facts}\n\n{smap}"
        if feedback:
            user += f"\n\nThe previous plan was sent back. Fix this: {feedback}\nPrevious plan: {json.dumps((m.get('plan') or {}).get('steps', []))[:3000]}"
        r = await self.adv.ask("plan", self.SYSTEM, user, 2500)
        j = extract_json(r["text"])
        if not isinstance(j, dict):
            raise ValueError("the planner did not return a mission")
        steps = clean_steps(j.get("steps"))
        reasoning = [scrub(str(x))[:300] for x in (j.get("reasoning") or []) if isinstance(x, (str, int, float))][:6]
        self.trail(m["id"], r.get("advisor") or "claude", (f"Plan written: {len(steps)} steps, risk {j.get('risk', '?')}. " + (" | ".join(reasoning) if reasoning else str(j.get("why") or ""))), "plan")
        problems, concerns = local_review(steps, self.allowed_cts())
        osteps = clean_owner_steps(j.get("owner_steps"))
        problems += owner_review(osteps, self.allowed_cts())
        if osteps:
            concerns.append(f"{len(osteps)} part(s) of this mission are yours to do by hand (see 'Your part')")
        if problems:
            self.trail(m["id"], "commander", "The safety checks found: " + "; ".join(problems[:6]), "problems")
        plan = {"title": str(j.get("title") or m["title"])[:120], "goal": str(j.get("goal") or m["goal"])[:600], "why": str(j.get("why") or "")[:600],
                "risk": j.get("risk") if j.get("risk") in ("low", "medium", "high") else "medium", "downtime": str(j.get("downtime") or "none")[:200],
                "steps": steps, "owner_steps": osteps, "planner": {"advisor": r.get("advisor"), "model": r.get("model")},
                "leads": {"leader": r.get("advisor"), "co": [x for x in (self.adv.policy()["panel"] if hasattr(self.adv, "policy") else []) if x != r.get("advisor")]},
                "revised_for": feedback[:600] if feedback else None, "reasoning": reasoning}
        if problems and retry:
            self.store.mission_update(m["id"], plan=plan)
            return await self.plan(self.store.mission_get(m["id"]), "; ".join(problems[:6]), retry=False)
        prev_dr = (m.get("plan") or {}).get("dry_run")
        if prev_dr and prev_dr.get("attempt") == (m.get("attempt") or 1):
            plan["dry_run"] = prev_dr
        elif not problems and self._want_dry_run(steps):
            dr = await self.dry_run(m, steps)
            if dr:
                plan["dry_run"] = dr
                self.store.mission_update(m["id"], plan=plan)
                outs = "\n".join(f"step {x['n']} ({x['title']}): {'ok' if x['ok'] else 'FAILED'}\n{x['out']}" for x in dr["steps"])
                return await self.plan(self.store.mission_get(m["id"]), "DRY RUN: before anyone approves, the team ran the plan's read-only first steps for real. Here is what they returned. "
                                       "Fix the plan so it fits these facts (drop checks that are now known, correct paths, names and versions); keep what still applies:\n" + outs[:3500],
                                       retry=True, lead_round=lead_round)
        if not problems:
            self.trail(m["id"], "commander", "Safety checks passed (block-list, targets, every change has an undo, shell syntax). Sending it to the reviewers.", "checks")
        panel = await self.second_opinions(plan)
        for x in panel:
            self.trail(m["id"], x["advisor"], {"agree": "No objection.", "caution": "Go carefully.", "disagree": "I disagree."}[x["verdict"]] + (" " + " | ".join(x["concerns"]) if x["concerns"] else ""), "review")
        verdicts = [p["verdict"] for p in panel]
        co = [x for x in panel if x["advisor"] != plan["planner"]["advisor"] and x["verdict"] in ("caution", "disagree") and x["concerns"]]
        if co and lead_round and not problems:
            # the leader answers the co-leads once: fix what is valid, keep the rest
            notes = "; ".join(f"{x['advisor']}: {c}" for x in co for c in x["concerns"])
            self.trail(m["id"], plan["planner"]["advisor"] or "claude", "Revising once to answer my co-leads' concerns before this reaches the boss.", "thinking")
            self.store.mission_update(m["id"], plan=plan, review={"problems": [], "concerns": concerns, "panel": panel, "round": 1})
            m2 = self.store.mission_get(m["id"])
            return await self.plan(m2, "Your co-leads reviewed the plan. Fix every concern that is valid (back up before overwriting, make detection robust, check what you claim), "
                                       "keep what is already right, and do not make it longer than needed: " + notes, retry=True, lead_round=False)
        status = "needs_revision" if problems else "ready"
        self.trail(m["id"], "commander", "Ready for your approval." if status == "ready" else "Not ready: the problems above must be fixed (send it back with a note).", "done")
        self.store.mission_update(m["id"], title=plan["title"], plan=plan, version=(m.get("version") or 1) + (1 if feedback else 0), status=status,
                                  review={"problems": problems, "concerns": concerns, "panel": panel, "worst": max(verdicts, key=["agree", "caution", "disagree"].index) if verdicts else None})

    async def second_opinions(self, plan):
        if not self.adv:
            return []
        text = "\n".join(f"{s['n']}. [{s['target']}] {s['title']}\n   run: {s['run']}\n   check: {s['check']} (expect {s['expect']})\n   rollback: {s['rollback'] or '-'}" for s in plan["steps"])
        try:
            replies = await self.adv.panel("review", "You review a mission plan an AI team will carry out on a home-lab Proxmox host after the owner approves it. Reply with ONE JSON object: "
                                           "{\"verdict\": \"agree\" | \"caution\" | \"disagree\", \"concerns\": [up to 3 short strings: what could break, what is missing, a wrong command]}.",
                                           f"Goal: {plan['goal']}\nWhy: {plan['why']}\nRisk: {plan['risk']}\nSteps:\n{text}" + ("\nThe owner's part (done by hand):\n" + "\n".join(
                                               f"- {o['when']}: {o['what']} (team checks: {o['check'] or 'nothing'})" for o in plan.get("owner_steps") or []) if plan.get("owner_steps") else ""), 700)
        except FrontierError:
            return []
        out = []
        for r in replies:
            j = extract_json(r["text"]) or {}
            if j.get("verdict") in ("agree", "caution", "disagree"):
                out.append({"advisor": r.get("advisor"), "verdict": j["verdict"], "concerns": [scrub(str(c))[:220] for c in (j.get("concerns") or []) if isinstance(c, str)][:3]})
        return out

    def revise(self, mid, feedback):
        m = self.store.mission_get(mid)
        if not m or m["status"] not in ("ready", "needs_revision", "plan_failed", "rolled_back", "failed", "refused", "rollback_failed", "lost"):
            raise ValueError("only a mission waiting for your decision, or one that stopped, can be replanned")
        if m["status"] in ("rolled_back", "failed", "refused", "rollback_failed", "lost"):
            self.store.mission_update(mid, attempt=m["attempt"] + 1, run=None, result=None, hash=None, approved_at=None)
        fb = re.sub(r"\s+", " ", str(feedback or "")).strip()[:600]
        if len(fb) < 5:
            raise ValueError("say what to change")
        self.store.mission_update(mid, status="planning", feedback=fb)
        self.store.audit("owner", "mission.revise", ticket(mid), fb[:150])
        self.tasks[mid] = asyncio.create_task(self._plan(mid, fb))

    def reject(self, mid, why=""):
        m = self.store.mission_get(mid)
        if not m or m["status"] in ("running", "done", "approved", "waiting_for_you", "checking_your_part"):
            raise ValueError("this mission can't be rejected now")
        self.store.mission_update(mid, status="rejected", feedback=str(why)[:300])
        self.store.audit("owner", "mission.reject", ticket(mid), str(why)[:150])

    # ------------------------------------------------------------------ 3. approve -> 4. execute
    def _sig(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    async def _call(self, method, path, body=None, headers=None):
        if self.http:
            return await self.http(method, path, body, headers or {})
        async with aiohttp.ClientSession() as s:
            async with s.request(method, self.url + path, json=body, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                return r.status, await r.json(content_type=None)

    async def approve(self, mid, ack=False, before_done=False, auto=None):
        m = self.store.mission_get(mid)
        if not m or m["status"] != "ready":
            raise ValueError("only a mission that is ready can be approved")
        if not ack:
            raise ValueError("confirm that you read the steps and the rollback")
        osteps = (m.get("plan") or {}).get("owner_steps") or []
        before = [o for o in osteps if o["when"] == "before"]
        if before and not before_done:
            raise ValueError("first do your part that comes before the team starts, then tick it: " + before[0]["what"][:120])
        if owner_review(osteps, self.allowed_cts()):
            raise ValueError("your part no longer passes the checks: " + owner_review(osteps, self.allowed_cts())[0])
        if not self.configured():
            raise ValueError("the runner is not installed yet: run tools/install-runner.sh on the Proxmox host")
        steps = m["plan"]["steps"]
        problems, _ = local_review(steps, self.allowed_cts())
        if problems:
            raise ValueError("the plan no longer passes the checks: " + problems[0])
        rid = run_id(m)
        h = mission_hash(rid, m["plan"]["goal"], steps)
        mission = {"id": rid, "goal": m["plan"]["goal"], "steps": steps, "hash": h, "title": f"{ticket(mid)} {m['title']}",
                   "readonly": all(RULES.readonly_ok(s[k]) is None for s in steps for k in ("run", "check"))}
        now = time.time()
        self.store.mission_update(mid, status="approved", hash=h, approved_at=now,
                                  owner={"hash": owner_hash(osteps), "items": [{"i": o["i"], "when": o["when"], "done_at": now if o["when"] == "before" else None} for o in osteps],
                                         "checks": [], "rid": None} if osteps else None)
        if auto:
            self.store.mission_update(mid, auto_rule=auto)
        self.store.audit("autopilot" if auto else "owner", "mission.approve", ticket(mid), (f"standing permission '{auto}', " if auto else "") + f"attempt {m['attempt']}: {m['title'][:100]} ({len(steps)} steps, {h[:12]})")
        status, d = await self._call("POST", "/run", {"mission": mission, "sig": self._sig(canonical(mission))})
        if status != 202:
            err = d.get("error", f"HTTP {status}") + (": " + "; ".join(d.get("problems", [])[:3]) if d.get("problems") else "")
            self.store.mission_update(mid, status="refused", result={"error": err})
            self.store.audit("system", "mission.refused", ticket(mid), err[:200])
            self.bus.publish("mission.finished", agent="commander", id=mid, ticket=ticket(mid), title=m["title"], status="refused", error=err)
            raise ValueError("the runner refused it: " + err)
        self.store.mission_update(mid, status="running")
        self.bus.publish("mission.started", agent="commander", id=mid, ticket=ticket(mid), attempt=m["attempt"], title=m["title"], steps=len(steps), auto=auto)
        self.tasks[mid] = asyncio.create_task(self._follow(mid))
        return h

    async def abort(self, mid):
        rid = run_id(self.store.mission_get(mid))
        status, d = await self._call("POST", f"/abort/{rid}", {"sig": self._sig(f"abort:{rid}")})
        self.store.audit("owner", "mission.abort", ticket(mid), str(status))
        return status == 200

    async def _follow(self, mid, every=3, limit=4 * 3600, rid=None, on_done=None):
        t0, last = time.time(), None
        rid = rid or run_id(self.store.mission_get(mid))
        on_done = on_done or self._finish
        while time.time() - t0 < limit:
            await asyncio.sleep(every)
            try:
                status, run = await self._call("GET", f"/status/{rid}", None, {"X-Sig": self._sig(f"status:{rid}")})
            except Exception:
                continue
            if status != 200:
                continue
            if on_done == self._finish:
                self.store.mission_update(mid, run=run)
            done = [s["n"] for s in run.get("steps", []) if s.get("state") == "ok"]
            if done != last:
                last = done
                self.bus.publish("mission.progress", agent="commander", id=mid, done=len(done))
            if run.get("state") in ("done", "rolled_back", "rollback_failed", "failed") and run.get("finished"):
                return await on_done(mid, run)
        self.store.mission_update(mid, status="lost", result={"error": "no final state from the runner within 4 h"})

    # ------------------------------------------------------------------ 5. report
    async def _finish(self, mid, run):
        m = self.store.mission_get(mid)
        state = run["state"]
        result = {"state": state, "error": run.get("error"), "verify": None}
        if state == "done" and self.adv and self.adv.ready("verify"):
            outs = "\n".join(f"{s['n']}. {s.get('title', '')}: exit {s.get('rc')}, check exit {s.get('check_rc')}\n   output: {(s.get('out') or '')[-300:]}\n   check: {(s.get('check_out') or '')[-200:]}" for s in run.get("steps", []))
            try:
                r = await self.adv.ask("verify", "You check whether a mission carried out on a home-lab Proxmox host achieved its goal. Reply with ONE JSON object: "
                                       "{\"verdict\": \"ok\" | \"concern\", \"note\": \"one or two plain sentences\"}. Use only the outputs given.",
                                       f"Goal: {m['plan']['goal']}\nStep outputs:\n{outs}" + ("\nStill to come (not part of these outputs): the owner does by hand: " + "; ".join(
                                           o["what"][:120] for o in m["plan"].get("owner_steps") or [] if o["when"] == "after") if any(o["when"] == "after" for o in m["plan"].get("owner_steps") or []) else ""), 400)
                j = extract_json(r["text"]) or {}
                result["verify"] = {"advisor": r.get("advisor"), "verdict": j.get("verdict") if j.get("verdict") in ("ok", "concern") else "ok", "note": scrub(str(j.get("note") or ""))[:300]}
            except FrontierError:
                pass
        status = {"done": "done", "rolled_back": "rolled_back", "rollback_failed": "rollback_failed"}.get(state, "failed")
        after = [o for o in (m.get("plan") or {}).get("owner_steps") or [] if o["when"] == "after"]
        if status == "done" and after:
            status = "waiting_for_you"                  # the team's steps are done; the ticket stays open until the owner's part is done and checked
        self.store.mission_update(mid, status=status, result=result)
        self.store.audit("agent:commander", f"mission.{status}", ticket(mid), f"attempt {m['attempt']}: " + (run.get("error") or m["title"])[:200])
        learned = await self.learn(mid, run) if status in ("done", "waiting_for_you") else None
        if learned:
            result["learned"] = learned
            self.store.mission_update(mid, result=result)
        if status == "done":
            self._close_finding(m, learned)
            self.schedule_stick(m)
            keys = (m.get("plan") or {}).get("cleanup_keys")
            if keys:
                self.store.kv_set("missions:cleaned", sorted(set(self.store.kv_get("missions:cleaned") or []) | set(keys))[-2000:])
        bad = [s for s in run.get("steps", []) if s.get("state") not in ("ok", "running")]
        tail = (bad[-1].get("check_out") or bad[-1].get("out") or "")[-600:] if bad else ""
        hist = (m.get("history") or []) + [{"attempt": m["attempt"], "status": status, "error": run.get("error"), "failed_step": bad[-1]["n"] if bad else None,
                                            "output": tail, "steps_ok": sum(s.get("state") == "ok" for s in run.get("steps", [])), "finished": time.time(), "hash": m.get("hash"),
                                            "rollback": run.get("rollback") or [], "restore_points": run.get("restore_points") or []}]
        self.store.mission_update(mid, history=hist)
        if status == "waiting_for_you":
            self.bus.publish("mission.waiting_for_you", agent="commander", id=mid, ticket=ticket(mid), title=m["title"], parts=[o["what"] for o in after])
        else:
            self.bus.publish("mission.finished", agent="commander", id=mid, ticket=ticket(mid), attempt=m["attempt"], title=m["title"], status=status, error=run.get("error"), verify=result["verify"])
        if status in ("rolled_back", "failed", "rollback_failed") and m["attempt"] < self.MAX_ATTEMPTS and self.adv and self.adv.ready("plan"):
            # the leader studies what went wrong and REPLANS the same ticket; the reviewers check the new MOP again; it comes back to the owner for approval
            fb = (f"Attempt {m['attempt']} stopped: {run.get('error')}. " + (f"Step {bad[-1]['n']} ({bad[-1].get('title', '')}) output: {tail}. " if bad else "")
                  + "The completed steps were undone. Find the cause in this output and fix the plan; keep the steps that worked.")
            self.store.mission_update(mid, status="planning", attempt=m["attempt"] + 1, feedback=fb, run=None, hash=None, approved_at=None)
            self.store.audit("agent:claude", "mission.replan", ticket(mid), fb[:200])
            self.bus.publish("mission.replanning", agent="claude", id=mid, ticket=ticket(mid), attempt=m["attempt"] + 1, title=m["title"])
            self.tasks[f"re{mid}"] = asyncio.create_task(self._plan(mid, fb))
        return status

    def _close_finding(self, m, learned=None):
        if m["ref"].startswith("finding:") and not (learned and learned.get("next_id")):
            fid = m["ref"].split(":", 1)[1]
            if (self.store.get(fid) or {}).get("status") in ("new", "seen"):
                self.store.set_status(fid, "done")
                self.bus.publish("finding.status", agent=(self.store.get(fid) or {}).get("agent", "commander"), id=fid, status="done", quiet=True)

    # ------------------------------------------------------------------ 6. your part: the owner does it by hand, the team checks it, then the ticket closes
    def owner_pending(self, m):
        """-> the owner's 'after' parts not yet ticked, for a mission waiting for the owner."""
        if m.get("status") != "waiting_for_you":
            return []
        done = {x["i"] for x in (m.get("owner") or {}).get("items", []) if x.get("done_at")}
        return [o for o in (m.get("plan") or {}).get("owner_steps") or [] if o["when"] == "after" and o["i"] not in done]

    async def owner_done(self, mid, i):
        """The owner ticks 'I did it' on one part. When every part is ticked, the team runs its read-only checks of them."""
        m = self.store.mission_get(mid)
        if not m or m["status"] != "waiting_for_you":
            raise ValueError("this mission is not waiting for your part")
        osteps = (m.get("plan") or {}).get("owner_steps") or []
        own = m.get("owner") or {}
        if own.get("hash") != owner_hash(osteps):
            raise ValueError("your part changed after approval; send the mission back to Sage")
        items = own.get("items") or []
        it = next((x for x in items if x["i"] == int(i)), None)
        if not it or it["when"] != "after":
            raise ValueError("no such part")
        it["done_at"] = time.time()
        self.store.mission_update(mid, owner={**own, "items": items})
        o = next(x for x in osteps if x["i"] == int(i))
        self.store.audit("owner", "mission.owner_done", ticket(mid), o["what"][:150])
        self.trail(mid, "commander", f"You did your part {o['i']}: {o['what'][:200]}", "owner")
        if self.owner_pending(self.store.mission_get(mid)):
            return "waiting_for_you"
        return await self._check_owner(mid)

    async def _check_owner(self, mid):
        m = self.store.mission_get(mid)
        own = m.get("owner") or {}
        checks = [o for o in m["plan"].get("owner_steps") or [] if o["when"] == "after" and o["check"]]
        if not checks:
            return await self._owner_finished(mid, None)
        n = len(own.get("checks") or []) + 1
        if n > 45 or not self.configured():
            raise ValueError("the team can't run the check of your part (runner not installed, or too many tries)")
        rid = int(mid) * 100 + 50 + n                   # each check of your part is its own signed, run-once job, apart from the mission's attempts (id*100+1..3)
        steps = [{"n": k, "title": f"Check your part {o['i']}: {o['what'][:100]}", "target": o["target"], "run": o["check"], "check": o["check"], "expect": o["expect"],
                  "rollback": "", "timeout": 120} for k, o in enumerate(checks, 1)]
        goal = f"Confirm the owner's part of {ticket(mid)}: " + "; ".join(o["what"][:80] for o in checks)
        mission = {"id": rid, "goal": goal[:600], "steps": steps, "hash": mission_hash(rid, goal[:600], steps), "title": f"{ticket(mid)} check of your part", "readonly": True}
        self.store.mission_update(mid, status="checking_your_part", owner={**own, "rid": rid})
        self.trail(mid, "commander", f"Every part of yours is ticked. The team is checking it now ({plural_s(len(steps), 'read-only check')}).", "checks")
        status, d = await self._call("POST", "/run", {"mission": mission, "sig": self._sig(canonical(mission))})
        if status != 202:
            err = d.get("error", f"HTTP {status}")
            self.store.mission_update(mid, status="waiting_for_you")
            raise ValueError("the runner refused the check: " + err)
        self.bus.publish("mission.progress", agent="commander", id=mid, done=0)
        self.tasks[f"own{mid}"] = asyncio.create_task(self._follow(mid, rid=rid, on_done=self._owner_finished))
        return "checking_your_part"

    async def _owner_finished(self, mid, run):
        m = self.store.mission_get(mid)
        own = m.get("owner") or {}
        if run is not None:
            bad = [s for s in run.get("steps", []) if s.get("state") != "ok"]
            rec = {"rid": own.get("rid"), "ts": time.time(), "ok": not bad and run.get("state") == "done",
                   "steps": [{"n": s["n"], "title": s.get("title", ""), "state": s.get("state"), "out": (s.get("check_out") or s.get("out") or "")[-400:]} for s in run.get("steps", [])]}
            own["checks"] = (own.get("checks") or []) + [rec]
            if not rec["ok"]:
                # the part that did not pass goes back on the owner's list; the others stay ticked
                checks = [o for o in m["plan"].get("owner_steps") or [] if o["when"] == "after" and o["check"]]
                failed = {checks[s["n"] - 1]["i"] for s in bad if 0 < s["n"] <= len(checks)} or {o["i"] for o in checks}
                for x in own.get("items") or []:
                    if x["i"] in failed:
                        x["done_at"] = None
                self.store.mission_update(mid, status="waiting_for_you", owner=own)
                out = (bad[0].get("check_out") or bad[0].get("out") or "")[-300:] if bad else (run.get("error") or "")
                self.trail(mid, "commander", f"The check of your part did not pass yet: {out}", "problems")
                self.store.audit("agent:commander", "mission.owner_check_failed", ticket(mid), out[:200])
                self.bus.publish("mission.owner_check_failed", agent="commander", id=mid, ticket=ticket(mid), title=m["title"], output=out[:300],
                                 parts=[o["what"] for o in m["plan"]["owner_steps"] if o["i"] in failed])
                return "waiting_for_you"
        self.store.mission_update(mid, status="done", owner=own)
        self.store.audit("agent:commander", "mission.done", ticket(mid), "the owner's part is done" + (" and checked" if run is not None else ""))
        self.trail(mid, "commander", "Your part is done" + (" and the team's check of it passed" if run is not None else "") + ". Ticket closed.", "done")
        self._close_finding(m, (m.get("result") or {}).get("learned"))
        self.schedule_stick(m)
        self.bus.publish("mission.finished", agent="commander", id=mid, ticket=ticket(mid), attempt=m["attempt"], title=m["title"], status="done", error=None,
                         verify=(m.get("result") or {}).get("verify"), owner=True)
        return "done"

    def owner_close(self, mid, why=""):
        """The owner closes a mission without doing (all of) their part."""
        m = self.store.mission_get(mid)
        if not m or m["status"] != "waiting_for_you":
            raise ValueError("this mission is not waiting for your part")
        why = re.sub(r"\s+", " ", str(why or "")).strip()[:300]
        self.store.mission_update(mid, status="done", result={**(m.get("result") or {}), "owner_skipped": why or "closed by the owner without their part"})
        self.store.audit("owner", "mission.owner_close", ticket(mid), why[:150])
        self.trail(mid, "commander", "You closed it without your part" + (f": {why}" if why else "") + ".", "done")

    # ------------------------------------------------------------------ the owner's to-do list (things learn() found only the owner can do)
    def todo_add(self, what, why="", src=""):
        what = re.sub(r"\s+", " ", scrub(str(what or ""))).strip()[:300]
        if len(what) < 5:
            raise ValueError("say what to do")
        todos = self.store.kv_get("owner:todo") or []
        if any(t["what"].lower() == what.lower() and not t.get("closed") for t in todos):
            return None
        t = {"id": int(time.time() * 1000) + len(todos), "ts": time.time(), "what": what, "why": scrub(str(why or ""))[:300], "src": src[:40], "closed": None}
        self.store.kv_set("owner:todo", (todos + [t])[-100:])
        return t

    def todo_close(self, tid, how="done"):
        todos = self.store.kv_get("owner:todo") or []
        t = next((x for x in todos if x["id"] == int(tid)), None)
        if not t:
            raise ValueError("no such item")
        t["closed"], t["how"] = time.time(), "done" if how == "done" else "dismissed"
        self.store.kv_set("owner:todo", todos)
        self.store.audit("owner", f"todo.{t['how']}", str(tid), t["what"][:150])
        return t

    def todos(self):
        return [t for t in (self.store.kv_get("owner:todo") or []) if not t.get("closed")]

    # ------------------------------------------------------------------ dry run: the plan's read-only first steps run before anyone approves
    def _want_dry_run(self, steps):
        if not (self.ap_policy().get("dry_run") and self.configured()):
            return False
        ro = [RULES.readonly_ok(s["run"]) is None and RULES.readonly_ok(s["check"]) is None for s in steps]
        return any(ro) and not all(ro) and ro[0]          # only plans that change things and start by reading (an all-read plan IS the diagnosis)

    async def dry_run(self, m, steps, wait=300):
        lead = []
        for s in steps:
            if RULES.readonly_ok(s["run"]) is None and RULES.readonly_ok(s["check"]) is None:
                lead.append(s)
            else:
                break
        lead = lead[:6]
        k = (self.store.kv_get(f"missions:dry:{m['id']}") or 0) + 1
        self.store.kv_set(f"missions:dry:{m['id']}", k)
        rid = 10_000_000 + int(m["id"]) * 100 + min(k, 99)
        goal = f"Dry run (read-only) of {ticket(m['id'])}"
        rsteps = [{**s, "n": i, "rollback": ""} for i, s in enumerate(lead, 1)]
        mission = {"id": rid, "goal": goal, "steps": rsteps, "hash": mission_hash(rid, goal, rsteps), "title": goal, "readonly": True}
        self.trail(m["id"], "commander", f"Dry run: running the first {plural_s(len(rsteps), 'read-only step')} for real, so the plan rests on facts, not guesses.", "dry")
        try:
            status, d = await self._call("POST", "/run", {"mission": mission, "sig": self._sig(canonical(mission))})
        except Exception as e:
            self.trail(m["id"], "commander", f"Dry run skipped: the runner did not answer ({type(e).__name__}).", "note")
            return None
        if status != 202:
            self.trail(m["id"], "commander", f"Dry run skipped: {d.get('error', status)}.", "note")
            return None
        t0 = time.time()
        while time.time() - t0 < wait:
            await asyncio.sleep(2)
            try:
                st, run = await self._call("GET", f"/status/{rid}", None, {"X-Sig": self._sig(f"status:{rid}")})
            except Exception:
                continue
            if st == 200 and run.get("finished"):
                out = {"attempt": m.get("attempt") or 1, "ts": time.time(), "rid": rid,
                       "steps": [{"n": s["n"], "title": s.get("title", ""), "ok": s.get("state") == "ok", "out": scrub(((s.get("out") or "") + ("\n--- check ---\n" + s["check_out"] if s.get("check_out") else ""))[-700:])}
                                 for s in run.get("steps", [])]}
                self.trail(m["id"], "commander", "Dry run done: " + "; ".join(f"step {x['n']} {'ok' if x['ok'] else 'failed'}" for x in out["steps"]) + ". Sage now fixes the plan to these facts.", "dry")
                return out
        self.trail(m["id"], "commander", "Dry run took too long; planning without it.", "note")
        return None

    # ------------------------------------------------------------------ did it stick? a fixed finding is checked again later
    def schedule_stick(self, m):
        h = self.ap_policy().get("stick_check_h") or 0
        if not h or not m["ref"].startswith("finding:"):
            return
        f = self.store.get(m["ref"].split(":", 1)[1])
        if not f:
            return
        q = [x for x in (self.store.kv_get("missions:stick") or []) if x["mid"] != m["id"]]
        q.append({"mid": m["id"], "fid": f["id"], "agent": f["agent"], "fp": f["fingerprint"], "title": f["title"][:140], "due": time.time() + h * 3600, "done_at": time.time()})
        self.store.kv_set("missions:stick", q[-200:])

    async def check_sticks(self, now=None):
        now = now or time.time()
        q = self.store.kv_get("missions:stick") or []
        due = [x for x in q if x["due"] <= now]
        if not due:
            return []
        out = []
        for x in due[:3]:
            if x["agent"] in getattr(self.rt, "agents", {}):
                started = self.rt.trigger(x["agent"], "stick-check")
                t = self.rt.tasks.get(x["agent"])
                if t:
                    try:
                        await asyncio.wait_for(asyncio.shield(t), 600)
                    except Exception:
                        pass
                if not started and not t:
                    continue
            back = self.store.q("SELECT * FROM findings WHERE agent=? AND fingerprint=? AND status IN ('new','seen') AND last_seen > ? ORDER BY ts DESC LIMIT 1",
                                (x["agent"], x["fp"], x["done_at"]))
            m = self.store.mission_get(x["mid"])
            res = (m.get("result") or {}) if m else {}
            if back:
                nf = self.store._f(back[0])
                res["stick"] = {"ok": False, "ts": now, "finding": nf["id"]}
                if m:
                    self.store.mission_update(m["id"], result=res)
                    self.trail(m["id"], "commander", f"Checked again after {self.ap_policy().get('stick_check_h')} h: it came back (“{nf['title'][:120]}”). Opening a new mission.", "problems")
                nid = self.create(f"It came back: {nf['title']}. {ticket(x['mid'])} fixed it, but {x['agent']} reports it again. Find out why the fix did not hold and fix the cause. "
                                  f"{scrub(nf['summary'])[:250]}", origin="commander", ref=f"finding:{nf['id']}")
                self.store.audit("agent:commander", "mission.regressed", ticket(x["mid"]), f"came back; new mission {ticket(nid)}")
                self.bus.publish("mission.regressed", agent="commander", id=x["mid"], ticket=ticket(x["mid"]), title=x["title"], next=nid, next_ticket=ticket(nid))
                out.append((x["mid"], False))
            else:
                res["stick"] = {"ok": True, "ts": now}
                if m:
                    self.store.mission_update(m["id"], result=res)
                    self.trail(m["id"], "commander", f"Checked again after {self.ap_policy().get('stick_check_h')} h: still fixed.", "done")
                out.append((x["mid"], True))
            q = [y for y in q if y["mid"] != x["mid"]]
            self.store.kv_set("missions:stick", q)
        return out

    # ------------------------------------------------------------------ the team cleans up its own restore points
    def restore_points(self, now=None):
        """Restore points of finished missions older than the set days: [(key, target, rid)]. Never those of a mission whose undo failed or that is still open."""
        days = self.ap_policy().get("cleanup_after_days") or 0
        if not days:
            return []
        now = now or time.time()
        cleaned = set(self.store.kv_get("missions:cleaned") or [])
        out = []
        for m in self.store.mission_list(500):
            if m["status"] in ("rollback_failed", "lost", "running", "approved", "waiting_for_you", "checking_your_part"):
                continue
            for h in m.get("history") or []:
                if h.get("status") == "rollback_failed" or now - (h.get("finished") or now) < days * 86400:
                    continue
                rid = int(m["id"]) * 100 + int(h.get("attempt") or 1)
                for rp in h.get("restore_points") or []:
                    if not rp.get("ok"):
                        continue
                    t = rp.get("target") or ""
                    key = f"{t}:{rid}"
                    if key not in cleaned and (t == "host" or re.fullmatch(r"lxc:\d+", t)):
                        out.append((key, t, rid))
        return out

    def plan_cleanup(self, now=None):
        """One mission (no AI needed: the steps are fixed) that deletes old restore points; Autopilot runs it if 'cleanup' is allowed."""
        if any(m.get("auto_rule") == "cleanup" or m["title"].startswith("Clean up old restore points") for m in self.store.mission_list(30) if m["status"] in ("planning", "ready", "approved", "running")):
            return None
        pts = self.restore_points(now)[:MAX_STEPS]
        if not pts:
            return None
        steps = []
        for i, (key, t, rid) in enumerate(pts, 1):
            if t == "host":
                p = f"/root/backups/prxy-mission-{rid}-etc.tgz"
                steps.append({"n": i, "title": f"Delete the /etc copy made before run {rid}", "target": "host", "run": f"rm -f {p}", "check": f"test ! -e {p}", "expect": "exit0", "rollback": "", "timeout": 60})
            else:
                ct = t.split(":")[1]
                steps.append({"n": i, "title": f"Delete snapshot prxy-m{rid} of container {ct}", "target": "host",
                              "run": f"if pct listsnapshot {ct} | grep -qw prxy-m{rid}; then pct delsnapshot {ct} prxy-m{rid}; fi",
                              "check": f"! pct listsnapshot {ct} | grep -qw prxy-m{rid}", "expect": "exit0", "rollback": "", "timeout": 300})
        days = self.ap_policy().get("cleanup_after_days")
        goal = f"Delete {plural_s(len(steps), 'restore point')} the team made before its own missions, now older than {days} days (finished missions only)."
        mid = self.store.mission_add("Clean up old restore points", goal, "commander", "")
        problems, concerns = local_review(steps, self.allowed_cts())
        plan = {"title": "Clean up old restore points", "goal": goal, "why": "Snapshots and /etc copies from finished missions take space in the thin pool and the backup disk.",
                "risk": "low", "downtime": "none", "steps": steps, "owner_steps": [], "planner": {"advisor": "commander", "model": "fixed steps"}, "leads": {"leader": "commander", "co": []},
                "reasoning": ["Fixed steps written by plain code, not by a model.", "Only snapshots named prxy-m<run> and copies named prxy-mission-<run>-etc.tgz are touched.",
                              "Restore points of a mission whose undo failed are kept."], "cleanup_keys": [k for k, _, _ in pts]}
        self.store.mission_update(mid, plan=plan, status="needs_revision" if problems else "ready", review={"problems": problems, "concerns": concerns, "panel": []})
        self.trail(mid, "commander", f"Listed {plural_s(len(steps), 'old restore point')} to delete (fixed steps, no model).", "facts")
        self.store.audit("agent:commander", "mission.create", ticket(mid), goal[:150])
        self.bus.publish("mission.created", agent="commander", id=mid, title=plan["title"])
        self.tasks[f"cl{mid}"] = asyncio.create_task(self._after_direct(mid))
        return mid

    async def _after_direct(self, mid):
        m = self.store.mission_get(mid)
        rule = await self.autopilot.consider(mid) if (self.autopilot and m["status"] == "ready" and self.configured()) else None
        m = self.store.mission_get(mid)
        self.bus.publish("mission.planned", agent="commander", id=mid, ticket=ticket(mid), attempt=1, title=m["title"], status=m["status"], steps=len(m["plan"]["steps"]), auto=rule)

    MAX_ATTEMPTS = 3
    MAX_CHAIN = 4                  # at most this many follow-on missions in one chain (no endless loops)

    def knowledge(self, days=30):
        return [k for k in (self.store.kv_get("team:knowledge") or []) if time.time() - k["ts"] < days * 86400]

    async def learn(self, mid, run):
        """The leader reads what the mission found, files it in the team's shared knowledge, and plans the next mission when more work is needed."""
        m = self.store.mission_get(mid)
        if not self.adv or not self.adv.ready("plan"):
            return None
        outs = "\n".join(f"{s['n']}. {s.get('title', '')}\n   output: {(s.get('out') or '')[-700:]}\n   check: {(s.get('check_out') or '')[-200:]}" for s in run.get("steps", []))
        known = "\n".join(f"- {k['ticket']}: {k['text']}" for k in self.knowledge()[-15:]) or "none"
        chain = self.store.kv_get(f"missions:depth:{mid}") or 0                    # how many follow-ons led here
        try:
            r = await self.adv.ask("plan", "You lead an AI operations team. A mission just finished. Read the step outputs and reply with ONE JSON object: "
                                   "{\"learned\": [\"2 to 8 short facts the outputs prove (only what the outputs show)\"], \"conclusion\": \"one or two sentences: what this means\", "
                                   "\"next\": {\"needed\": true|false, \"goal\": \"the next mission in one or two sentences, building on what was learned\", \"why\": \"one sentence\"}, "
                                   "\"owner_todo\": [{\"what\": \"something ONLY the owner can do (another system's settings with the exact menu path, a password, hardware, a decision)\", \"why\": \"one sentence\"}]}. "
                                   "Say needed=false when the goal is reached or when the next step needs the owner (a decision, a password, access to another system). "
                                   "Do not propose anything the team already knows or already did.",
                                   f"Mission {ticket(mid)}: {m['plan']['goal']}\n\nStep outputs:\n{outs}\n\nWhat the team already knew:\n{known}", 1200)
        except FrontierError:
            return None
        j = extract_json(r["text"]) or {}
        facts = [scrub(str(x))[:300] for x in (j.get("learned") or []) if isinstance(x, (str, int, float))][:8]
        out = {"by": r.get("advisor"), "facts": facts, "conclusion": scrub(str(j.get("conclusion") or ""))[:500], "next_id": None, "next_why": None}
        if facts:
            kn = [k for k in (self.store.kv_get("team:knowledge") or []) if time.time() - k["ts"] < 60 * 86400][-180:]
            kn += [{"ts": time.time(), "ticket": ticket(mid), "text": f} for f in facts]
            self.store.kv_set("team:knowledge", kn)
            self.trail(mid, r.get("advisor") or "claude", "What we learned: " + " | ".join(facts) + (f" Conclusion: {out['conclusion']}" if out["conclusion"] else ""), "learned")
        out["owner_todo"] = []
        for t in (j.get("owner_todo") or [])[:3] if isinstance(j.get("owner_todo"), list) else []:
            if isinstance(t, dict) and isinstance(t.get("what"), str):
                try:
                    if self.todo_add(t["what"], t.get("why") or "", ticket(mid)):
                        out["owner_todo"].append(t["what"][:300])
                except ValueError:
                    pass
        if out["owner_todo"]:
            self.trail(mid, r.get("advisor") or "claude", "Added to your list (only you can do these): " + " | ".join(out["owner_todo"]), "owner")
        nx = j.get("next") if isinstance(j.get("next"), dict) else {}
        if nx.get("needed") is True and isinstance(nx.get("goal"), str) and len(nx["goal"].strip()) > 15 and chain < self.MAX_CHAIN:
            goal = f"{nx['goal'].strip()[:600]} (Follow-on of {ticket(mid)}; learned: {'; '.join(facts)[:500]})"
            nid = self.create(goal, origin="mission", ref=m["ref"] or f"mission:{mid}")
            self.store.kv_set(f"missions:depth:{nid}", chain + 1)
            out["next_id"], out["next_why"] = nid, scrub(str(nx.get("why") or ""))[:300]
            self.trail(mid, r.get("advisor") or "claude", f"Planning the next mission as {ticket(nid)}: {nx['goal'][:300]}", "next")
        self.bus.publish("mission.learned", agent=r.get("advisor") or "claude", id=mid, ticket=ticket(mid), facts=len(facts), next=out["next_id"], next_ticket=ticket(out["next_id"]) if out["next_id"] else None, conclusion=out["conclusion"])
        return out

    # ------------------------------------------------------------------ the crew acts: every proposed fix becomes a mission for High Command
    def auto_propose(self, max_per_day=3, now=None):
        """The Chief turns open findings that carry a proposed fix (and medium+ problems on machines) into mission plans, so the crew's reports become work.
        A finding gets one mission at a time; rejected missions are not re-proposed for 30 days. -> number started."""
        now = now or time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        k = self.store.kv_get("missions:auto:day") or {}
        if k.get("d") != day:
            k = {"d": day, "n": 0}
        if k["n"] >= max_per_day or not self.adv or not self.adv.ready("plan"):
            return 0
        open_refs = {}
        for m in self.store.mission_list(200):
            if m["ref"].startswith("finding:"):
                open_refs.setdefault(m["ref"], []).append(m)
        sev = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        cands = [f for f in self.store.pending_proposals() if f["agent"] not in ("commander",)]
        cands.sort(key=lambda f: (sev.get(f["severity"], 9), -f["count"]))
        started = 0
        for f in cands:
            if k["n"] >= max_per_day:
                break
            prev = open_refs.get(f"finding:{f['id']}", [])
            if any(m["status"] not in ("rejected", "failed", "rolled_back", "refused", "plan_failed") for m in prev):
                continue
            if any(m["status"] == "rejected" and now - m["updated"] < 30 * 86400 for m in prev):
                continue
            steps = " / ".join((f.get("proposed_action") or {}).get("steps", []))[:500]
            self.create(f"Fix: {f['title']}. {f['summary'][:300]} Suggested by the crew: {steps}", origin="commander", ref=f"finding:{f['id']}")
            k["n"] += 1; started += 1
        self.store.kv_set("missions:auto:day", k)
        return started

    async def run(self, every=600):
        await asyncio.sleep(180)
        n = 0
        while True:
            try:
                if n % 6 == 0:
                    self.auto_propose()
                await self.check_sticks()
                day = time.strftime("%Y-%m-%d")
                if self.store.kv_get("missions:cleanup:day") != day and self.configured():
                    self.store.kv_set("missions:cleanup:day", day)
                    self.plan_cleanup()
            except Exception as e:
                print(f"missions: {type(e).__name__}: {e}", flush=True)
            n += 1
            await asyncio.sleep(every)

    def start(self):
        self.recover()
        self.tasks["auto"] = asyncio.create_task(self.run())

    def recover(self):
        """After a restart: keep following missions that were running."""
        for m in self.store.mission_list(20):
            if m["status"] in ("running", "approved"):
                self.tasks[m["id"]] = asyncio.create_task(self._follow(m["id"]))
            elif m["status"] == "checking_your_part" and (m.get("owner") or {}).get("rid"):
                self.tasks[f"own{m['id']}"] = asyncio.create_task(self._follow(m["id"], rid=m["owner"]["rid"], on_done=self._owner_finished))
            elif m["status"] == "planning":
                self.tasks[m["id"]] = asyncio.create_task(self._plan(m["id"], m.get("feedback") or None))

    async def runner_health(self):
        if not self.configured():
            return {"installed": False}
        try:
            status, d = await self._call("GET", "/health")
            return {"installed": True, "reachable": status == 200, **(d if status == 200 else {})}
        except Exception as e:
            return {"installed": True, "reachable": False, "error": str(e)[:120]}
