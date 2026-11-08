"""Archivist (the council's Memory keeper): looks after the findings history and closes the feedback loop.
  * noisy agents    most of an agent's findings were rejected -> says so
  * unreviewed      findings that have sat 'new' AND unchanged for two weeks (a standing condition nobody has looked at)
  * tuning          when the owner has rejected the same KIND of finding three times, the Archivist proposes a quieter threshold for the agent that raises it
                    ('Tune Sentinel: scan_pairs 200 -> 250?'). The change is applied only when the owner presses the button; it is a versioned config change
                    and can be rolled back from Settings. Nothing is ever tightened automatically.
The searchable history itself is served by the API (/api/memory/*)."""
import time
from ..config import PARAM_SPECS
from ..runner import Agent

TUNE_AFTER = 3


def nicer(v):
    return round(v, 4) if isinstance(v, float) else v


def tuning_proposals(rows, specs, current, tuned_at):
    """rows: findings (30 days). specs: {agent: {param: spec}}. current(agent, param) -> value. tuned_at(agent, param) -> ts of the last applied tuning.
    Returns [{'agent','param','from','to','rejected':[titles]}]. Pure function (tested)."""
    out = []
    for agent, ps in specs.items():
        used = set()
        for pname, sp in ps.items():
            if not sp.get("rules"):
                continue
            since = tuned_at(agent, pname)
            rej = [f for f in rows if f["agent"] == agent and f["status"] == "rejected" and (f.get("decided_at") or 0) > since and f["fingerprint"] not in used
                   and any(f["fingerprint"].startswith(r) for r in sp["rules"])]
            if len({f["fingerprint"] for f in rej}) < TUNE_AFTER:
                continue
            cur = current(agent, pname)
            new = cur + sp["step"] if sp["quieter"] == "up" else cur - sp["step"]
            new = nicer(max(sp["min"], min(sp["max"], new)))
            if new == cur:
                continue                                   # already at the limit of what the setting allows
            used |= {f["fingerprint"] for f in rej}
            out.append({"agent": agent, "param": pname, "from": cur, "to": new, "label": sp["label"], "rejected": [f["title"][:80] for f in rej[:3]], "n": len(rej)})
    return out


class Archivist(Agent):
    name = "archivist"
    group = "council"
    title = "The Archivist"
    autonomy = "R"
    at = ["03:15"]
    auto_resolve = True

    async def run(self, ctx):
        store, now = ctx.runner.store, time.time()
        ctx.working("reading", "30 days of findings")
        rows = [f for f in store.list_findings(None, None, None, 500) if now - f["ts"] < 30 * 86400]
        ctx.checked(len(rows))
        per = {}
        for f in rows:
            p = per.setdefault(f["agent"], {"total": 0, "rejected": 0})
            p["total"] += 1
            p["rejected"] += f["status"] == "rejected"
        noisy = 0
        for agent, p in per.items():
            if p["total"] >= 5 and p["rejected"] / p["total"] >= 0.5:
                noisy += 1
                ctx.finding(f"memory:noisy:{agent}", "info", f"{agent} is noisy: {p['rejected']} of its {p['total']} findings in 30 days were rejected",
                            "When most findings from an agent are rejected, its rules are probably too sensitive. Consider raising its thresholds or disabling the rule that keeps firing.",
                            [{"type": "metric", "ref": "findings history", "excerpt": f"{agent}: {p['rejected']}/{p['total']} rejected"}], confidence=0.7)
        stale = [f for f in rows if f["status"] == "new" and f.get("chronic") and f["severity"] != "info"]
        if stale:
            ctx.finding("memory:unreviewed", "info", f"{len(stale)} finding(s) have been open and unchanged for over two weeks without being looked at",
                        "Mark each one Seen, Rejected or Done so the briefing can focus on what is new.",
                        [{"type": "metric", "ref": "findings history", "excerpt": "; ".join(f['title'][:60] for f in stale[:5])}], confidence=0.9)
        cfg = ctx.cfg["agents"]
        props = tuning_proposals(rows, PARAM_SPECS, lambda a, p: cfg.get(a, {}).get("params", {}).get(p, PARAM_SPECS[a][p]["default"]), lambda a, p: store.kv_get(f"tune_at:{a}:{p}", 0))
        for t in props:
            title = rt_title(ctx, t["agent"])
            f, _ = ctx.finding(f"tune:{t['agent']}:{t['param']}", "info", f"Make {title} quieter? Change \"{t['param']}\" from {t['from']} to {t['to']}",
                               f"You rejected {t['n']} findings of this kind in the last 30 days ({'; '.join(t['rejected'])}). The setting is: {t['label']}. Press 'Apply this change' to try the quieter value; "
                               f"it is saved as a new config version and you can roll it back in Settings. If you reject this suggestion, it will not be raised again for 30 days.",
                               [{"type": "metric", "ref": "findings history", "excerpt": f"{t['n']} rejected: " + "; ".join(t["rejected"])}], confidence=0.7)
            if f:
                store.set_thread(f["id"], {"tune": {k: t[k] for k in ("agent", "param", "from", "to")}})
        ctx.note(f"{len(rows)} findings in the last 30 days across {len(per)} agents; {noisy} noisy agent(s), {len(stale)} unreviewed, {len(props)} tuning suggestion(s)")

    SKILLS = {"history": {"about": "what the findings history says about a topic: how often it came up, when, and what you decided", "args": {"query": "words or an address to look for"}}}

    async def skill_history(self, ctx, args):
        q = str(args.get("query", "")).strip()[:80]
        rows = ctx.runner.store.search_findings(q, 60) if len(q) >= 2 else []
        if not rows:
            return {"summary": f"Nothing in the history matches '{q}'.", "facts": [], "followups": [], "recommend": [], "data": {}}
        by = {}
        for f in rows:
            by.setdefault((f["agent"], f["fingerprint"]), []).append(f)
        facts = [f"{len(by)} distinct finding(s) in the history match '{q}'."]
        for (agent, fp), fs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:5]:
            st = {}
            for f in fs:
                st[f["status"]] = st.get(f["status"], 0) + 1
            facts.append(f"{agent}: {fs[0]['title'][:90]} (seen {len(fs)} time(s), first {time.strftime('%Y-%m-%d', time.localtime(min(f['ts'] for f in fs)))}; outcomes: " + ", ".join(f"{k} {v}" for k, v in st.items()) + ")")
        return {"summary": facts[0], "facts": facts, "followups": [], "recommend": [], "data": {}}



def rt_title(ctx, name):
    a = ctx.runner.agents.get(name)
    return a.title if a else name
