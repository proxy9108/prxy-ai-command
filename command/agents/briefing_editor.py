"""Briefing Editor: the editor for the TV 'Prxy Briefing' and the personal morning brief.
1) It condenses the Commander's lab briefing into a COUNTS-ONLY status for the TV (no names, no addresses) and serves it at /feed/lab.json to the
   TV feed's address only. 2) It checks the Day Briefing against its own data: any number in the greeting that is not in the facts is flagged.
It cannot edit the TV pipeline on LXC 137 itself (no SSH channel by design); one standing finding lists the manual wiring steps."""
import re, time
from ..runner import Agent


def tv_status(doc, live):
    """doc: the Commander's briefing document (or None) -> counts-only status for the TV."""
    if not doc:
        return {"state": "unknown", "line": "Lab: no briefing yet.", "open": 0, "attention": 0, "high": 0, "agents_live": live}
    top = doc.get("top", [])
    high = sum(1 for g in top if g["severity"] in ("high", "critical"))
    att = len(top)
    state = "alert" if high else "attention" if att else "quiet"
    if state == "quiet":
        line = "Lab: all quiet."
    elif not high:
        line = f"Lab: {att} item{'s' if att != 1 else ''} need{'s' if att == 1 else ''} attention."
    else:
        line = f"Lab: {high} high-priority item{'s' if high != 1 else ''}" + (f" and {att - high} other{'s' if att - high != 1 else ''}" if att - high else "") + " need attention."
    return {"state": state, "line": line, "open": doc["counts"]["open"], "attention": att, "high": high, "agents_live": live}


def fidelity(brief):
    """Numbers in the greeting must appear in the brief's own facts."""
    nums = lambda t: set(re.findall(r"\d+(?:\.\d+)?", t))
    return sorted(nums(brief.get("headline", "")) - nums(brief.get("facts", "")))


class BriefingEditor(Agent):
    name = "briefing_editor"
    group = "housekeeping"
    title = "Briefing Editor"
    autonomy = "R"
    interval_s = 1800
    auto_resolve = True

    async def run(self, ctx):
        st = ctx.runner.store
        ctx.working("reading", "the Commander's briefing")
        lab = st.get_briefing()
        live = sum(1 for a in ctx.runner.describe() if a["enabled"] and a["name"] != "demo")
        status = tv_status(lab["doc"] if lab else None, live)
        status["generated"] = time.time()
        ctx.kv_set("tv_lab", status)
        ctx.working("typing", status["line"])
        brief = st.get_briefing(trigger="personal")
        if brief:
            bad = fidelity(brief["doc"])
            if bad:
                ctx.finding("editor:greeting-numbers", "medium", "The morning greeting contains numbers that are not in its facts", f"Numbers not found in the data: {', '.join(bad)}. The greeting may be inventing details.",
                            [{"type": "check", "ref": f"brief #{brief['id']}", "excerpt": f"greeting: {brief['doc']['headline'][:200]}"}], confidence=0.8)
        ctx.finding("editor:tv-wiring", "info", "The TV lab status is ready; the TV feed does not read it yet",
                    "A counts-only status is published at /feed/lab.json (answers only the TV feed's address). The TV feed on LXC 137 still shows its own Prometheus counts. To use this one instead, the feed must fetch it; that is a change on LXC 137 that only you can approve.",
                    [{"type": "config", "ref": "/feed/lab.json", "excerpt": json_short(status)}],
                    proposed_action={"kind": "manual", "risk": "low", "steps": ["On LXC 137 edit /opt/tv-feed/feed.py: add a fetch of http://10.20.40.139:8320/feed/lab.json (cache it for 5 minutes) and merge its 'state' and 'line' into the lab widget payload", "Restart tv-feed (systemctl restart tv-feed) and check the lab widget on the TV"],
                                     "rollback": ["Restore feed.py from its backup copy and restart tv-feed"]}, confidence=0.9)
        ctx.note(f"TV status: {status['line']}")


def json_short(d):
    return ", ".join(f"{k}={v}" for k, v in d.items() if k != "generated")
