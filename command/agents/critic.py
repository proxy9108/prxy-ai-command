"""The Critic (the council's Second Opinion): argues the other side before anything is acted on.
For every open finding that carries a proposed action (and every high/critical finding) it writes a review: a verdict (agree / caution / disagree),
concerns, harmless explanations, things to verify first. The verdict comes from plain rules and from what the OTHER agents currently see
(for example: never do risky work while the last backup is unhealthy). The model adds one short 'devil's advocate' paragraph that may be wrong
and is labelled so; it is dropped if it contains a number that is not in the facts."""
import re, time
from datetime import datetime
from zoneinfo import ZoneInfo
from ..runner import Agent

RISKY = re.compile(r"reboot|shut ?down|stop |restart|full-upgrade|restore|delete|remove", re.I)
HARMLESS = [  # (fingerprint regex, harmless explanation, things to verify first)
    (r"^guest-down:", "A host reboot, a backup window or a manual stop can leave a guest down briefly.", ["Check the guest's task history in Proxmox for who or what stopped it", "Check whether a backup or an update is running right now"]),
    (r"^capacity:guest:lxc/131", "Model downloads and deletions on the Ollama disk make step changes that look like steady growth.", ["Did you pull or delete models in the last week? (ollama list)", "Check the real free space, not just the trend"]),
    (r"^capacity:", "A trend over a short window can come from a one-off event such as a restore, an import or a log burst.", ["Look at the 7-day graph in Grafana before acting"]),
    (r"^scan:", "Background scanning from the internet is constant. With carrier-grade NAT, outside hosts cannot normally reach the lab.", ["Check whether the address is a known resolver or CDN", "Look at what the flows actually touched"]),
    (r"^patch:(kernel|reboot)", "A new kernel series needs a reboot and can break out-of-tree drivers. Ollama's GPU access depends on the host's NVIDIA driver.", ["Read the release notes of the new kernel series", "Confirm the NVIDIA driver supports it before rebooting", "Know how to boot the previous kernel from the boot menu"]),
    (r"thin-pool|^storage:local-lvm", "The pool has run near 90% for a while; growing a guest disk makes the over-commit worse without using space yet.", ["Check the real pool usage (lvs) and which guests grew recently"]),
]


def assess(f, ctx_info):
    """Pure function. ctx_info: {'open': [findings], 'agents': {name: {...}}, 'now': ts, 'local_hour': int, 'quiet': (start_h, end_h)}."""
    concerns, harmless, verify, blockers = [], [], [], []
    pa = f.get("proposed_action") or {}
    text = " ".join(pa.get("steps", []))
    now = ctx_info["now"]
    if pa and not f.get("evidence"):
        concerns.append("The evidence for this finding has expired under the 24 h retention rule: re-verify before acting.")
    if f.get("confidence", 1) < 0.6:
        concerns.append(f"The agent itself is only {int(f['confidence'] * 100)}% sure.")
    iv = (ctx_info["agents"].get(f["agent"]) or {}).get("interval_s") or 0
    if f.get("count", 1) == 1 and f["severity"] in ("high", "critical") and iv and iv <= 900 and now - f["ts"] < 900:
        concerns.append("Seen only once so far: wait for the next check to confirm it persists.")
    risky = bool(RISKY.search(text)) or pa.get("risk") in ("medium", "high")
    if risky:
        bw = [o for o in ctx_info["open"] if o["agent"] == "backup_warden" and o["severity"] in ("high", "critical") and o["fingerprint"] != f["fingerprint"]]
        bst = ctx_info["agents"].get("backup_warden") or {}
        if bw:
            blockers.append(f"Backup Warden reports an unhealthy backup ({bw[0]['title']}). Do not do risky work until the backups are good.")
        elif not bst.get("last_run") or now - bst["last_run"] > 12 * 3600:
            concerns.append("Backup Warden has not checked the backups recently: confirm last night's backup before doing risky work.")
        else:
            verify.append("Backups were verified healthy by Backup Warden at its last check.")
    if re.search(r"reboot|shut ?down", text, re.I):
        qs, qe = ctx_info["quiet"]
        inq = qs <= ctx_info["local_hour"] < qe if qs <= qe else (ctx_info["local_hour"] >= qs or ctx_info["local_hour"] < qe)
        concerns.append("This stops every guest for a few minutes, including the Discord bot and the Ollama GPU service." + ("" if inq else f" It is outside the quiet hours ({qs:02d}:00-{qe:02d}:00): wait for them."))
        if not inq:
            blockers.append("Wait for the quiet hours before rebooting or shutting things down.")
    for pat, h, v in HARMLESS:
        if re.search(pat, f["fingerprint"]) or re.search(pat, f["title"], re.I):
            harmless.append(h); verify += v
            break
    gm = re.search(r"(?:lxc|qemu)/\d+", f["fingerprint"])
    related = [o for o in ctx_info["open"] if gm and o["id"] != f["id"] and o["agent"] != f["agent"] and re.search(re.escape(gm.group(0)) + r"\b", o["fingerprint"])]
    if related:
        concerns.append(f"Other agents also report on the same guest: {related[0]['agent']}: {related[0]['title']}")
    verdict = "disagree" if blockers else ("caution" if concerns else "agree")
    return {"verdict": verdict, "blockers": blockers, "concerns": concerns, "harmless_explanations": harmless, "verify_first": list(dict.fromkeys(verify))[:6]}


async def outside_review(fr, f, rv):
    """The outside advisors' view of one proposal: agree / caution / disagree plus concerns. With a review panel every advisor answers on its own;
    the combined verdict is the strictest one. None when nobody is available or nothing readable came back."""
    from ..frontier import FrontierError, extract_json
    pa = f["proposed_action"]
    sys_user = ("You review one proposed manual change for a home-lab Proxmox host. Reply with ONE JSON object: {\"verdict\": \"agree\" | \"caution\" | \"disagree\", \"concerns\": [up to 3 short strings]}. Judge only whether the steps are safe and make sense for the finding; use only the facts given.",
                         f"Finding: {f['title']}. {f['summary'][:400]}\nProposed steps: {' / '.join(pa.get('steps', []))}\nRollback plan: {' / '.join(pa.get('rollback', []))}\nLocal checks said: {rv['verdict']} {' '.join(rv['concerns'] + rv['blockers'])[:300]}", 500)
    try:
        if hasattr(fr, "panel") and fr.policy()["jobs"].get("review") == "panel":
            replies = await fr.panel("review", *sys_user)
        else:
            replies = [await fr.ask("review", *sys_user)]
    except FrontierError:
        return None
    order, panel = ["agree", "caution", "disagree"], []
    for r in replies:
        j = extract_json(r["text"])
        if isinstance(j, dict) and j.get("verdict") in order:
            panel.append({"advisor": r.get("advisor", "claude"), "backend": r["backend"], "model": r["model"], "verdict": j["verdict"],
                          "concerns": [str(c)[:220] for c in (j.get("concerns") or []) if isinstance(c, str)][:3]})
    if not panel:
        return None
    worst = max(panel, key=lambda x: order.index(x["verdict"]))
    return {**worst, "panel": panel, "agree_count": sum(x["verdict"] == "agree" for x in panel)}


def adjust(conf, verdict):
    return round(conf * {"agree": 1.0, "caution": 0.9, "disagree": 0.7}[verdict], 2)


class Critic(Agent):
    name = "critic"
    group = "council"
    title = "The Critic"
    autonomy = "R"
    interval_s = 1800

    async def run(self, ctx):
        rt, store, cfg = ctx.runner, ctx.runner.store, ctx.cfg
        todo = store.needs_review()
        if not todo:
            ctx.note("nothing to review")
            return
        tz = ZoneInfo(cfg["tz"])
        qs, qe = (int(x.split(":")[0]) for x in cfg["gateway"]["quiet_hours"].split("-"))
        info = {"open": store.list_findings(None, "new", None, 300) + store.list_findings(None, "seen", None, 300),
                "agents": {a["name"]: a for a in rt.describe()}, "now": time.time(), "local_hour": datetime.now(tz).hour, "quiet": (qs, qe)}
        disputes, outside_calls = 0, 0
        for f in todo[:10]:
            ctx.working("thinking", f["title"][:40])
            rv = assess(f, info)
            facts = f"Finding: {f['title']}. Summary: {f['summary']} Proposed steps: {'; '.join((f.get('proposed_action') or {}).get('steps', [])) or 'none'}. Checks already made: {'; '.join(rv['concerns'] + rv['blockers']) or 'none'}."
            try:
                r = await ctx.llm([{"role": "system", "content": "You are a skeptical reviewer. In at most two short sentences, give the strongest honest reason this proposed action might be unnecessary or risky. Use only the facts given; add no numbers or names that are not in them."},
                                   {"role": "user", "content": facts}], options={"num_predict": 90})
                txt = r["text"].strip().replace("\n", " ")
                if set(re.findall(r"\d+(?:\.\d+)?", txt)) <= set(re.findall(r"\d+(?:\.\d+)?", facts)) and 15 < len(txt) < 400:
                    rv["devils_advocate"] = txt
            except Exception:
                pass
            rv.update(reviewed=time.time(), count_at_review=f["count"], by="rules + model (the model's paragraph can be wrong)")
            fr = getattr(rt, "frontier", None)
            if f.get("proposed_action") and fr and fr.available("review") and outside_calls < 3:        # a proposal is about to be acted on: worth a second look from the outside model
                outside_calls += 1
                o = await outside_review(fr, f, rv)
                for x in (o or {}).get("panel", []):
                    rt.bus.publish("advisor.review", agent=x["advisor"], id=f["id"], title=f["title"], verdict=x["verdict"], concern=(x["concerns"] or [""])[0])
                if o:
                    rv["outside"] = o
                    order = ["agree", "caution", "disagree"]
                    if order.index(o["verdict"]) > order.index(rv["verdict"]):
                        rv["verdict"] = o["verdict"]
                        rv["concerns"] = rv["concerns"] + [f"Outside review: {c}" for c in o["concerns"][:2]]
            store.set_critic(f["id"], rv, adjust(f["confidence"], rv["verdict"]))
            store.audit("agent:critic", "review", f["id"], f"{rv['verdict']}: {f['title'][:100]}")
            rt.bus.publish("council.review", agent=self.name, target=f["agent"], id=f["id"], verdict=rv["verdict"])
            if rv["verdict"] == "disagree":
                disputes += 1
        ctx.note(f"reviewed {min(len(todo), 10)} finding(s), {disputes} disputed")
