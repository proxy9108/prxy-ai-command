"""IDS Doctor: is the intrusion-detection / firewall log pipeline really working?
The handoff notes that Suricata runs in Divert mode but inspected nothing and that firewall logs should reach Loki. Today Loki only has the
'netflow' and 'unifi_siem' jobs. This agent keeps that gap visible until it is fixed (the finding clears itself when logs appear), and once
Suricata logs exist it checks that its stats events show packets actually being inspected."""
import json
from ..runner import Agent
from ..sources import Loki, SourceError

EXPECTED = {"suricata": "Suricata alerts and stats", "opnsense": "OPNsense firewall (filterlog) and system logs"}
FIX_STEPS = ["OPNsense: System > Settings > Logging > Remote: add a destination for the Loki host (LXC 133, UDP 514) with the applications 'filter' and 'suricata' ticked",
             "OPNsense: Services > Intrusion Detection > Administration: enable it, tick 'Syslog alerts' and 'Syslog eve events'",
             "On LXC 133: add a promtail scrape (or rsyslog rule) that labels those lines job=opnsense / job=suricata, then check this page again"]


def suricata_inspected(lines):
    """lines: Loki entries of eve 'stats' events -> (packets_seen_total, events_parsed). Counters only ever grow, so 0 growth means nothing is inspected."""
    pk = []
    for l in lines:
        try:
            j = json.loads(l["line"][l["line"].index("{"):])
            pk.append((l["ts"], j["stats"]["decoder"]["pkts"]))
        except (ValueError, KeyError, TypeError):
            continue
    pk.sort()
    return (pk[-1][1] - pk[0][1], len(pk)) if len(pk) >= 2 else (None, len(pk))


class IdsDoctor(Agent):
    name = "ids_doctor"
    group = "security"
    title = "IDS Doctor"
    autonomy = "P"
    interval_s = 3600
    auto_resolve = True

    async def run(self, ctx):
        loki = Loki()
        try:
            ctx.working("reading", "which log jobs Loki has")
            jobs = set(await loki.label_values("job"))
        except SourceError as e:
            ctx.finding("source:loki", "medium", "IDS Doctor cannot read Loki", str(e), [{"type": "error", "ref": "loki", "excerpt": str(e)[:300]}], confidence=1.0)
            ctx.no_data("Loki unreachable")
            return
        missing = []
        for job, what in EXPECTED.items():
            ctx.working("scanning", f"{job} logs in the last hour")
            n = 0
            if job in jobs:
                try:
                    n = len(await loki.query(f'{{job="{job}"}}', minutes=60, limit=5))
                except SourceError:
                    n = 0
            if not n:
                missing.append(job)
        for job in missing:
            ctx.finding(f"ids:no-{job}-logs", "medium", f"No {job} logs are reaching Loki", f"{EXPECTED[job]} should arrive in Loki as job '{job}' but nothing was received in the last hour. Without it, nobody can say whether the firewall or the IDS is seeing and blocking anything.",
                        [{"type": "metric", "ref": "loki labels", "excerpt": f"jobs present: {', '.join(sorted(jobs)) or 'none'}; expected: {job}"}],
                        proposed_action={"kind": "manual", "risk": "low", "steps": FIX_STEPS, "rollback": ["Remove the remote logging destination in OPNsense and switch 'Syslog alerts' off again"]}, confidence=0.85)
        if "suricata" not in missing:
            ctx.working("reading", "Suricata stats")
            try:
                grew, n = suricata_inspected(await loki.query('{job="suricata"} |= "\\"event_type\\":\\"stats\\""', minutes=60, limit=200))
            except SourceError:
                grew, n = None, 0
            if grew == 0:
                ctx.finding("ids:suricata-inspects-nothing", "high", "Suricata is running but inspected no packets in the last hour", "Its own packet counter did not move, so no traffic is being analysed (the earlier Divert-mode problem).",
                            [{"type": "log", "ref": "suricata stats events", "excerpt": f"{n} stats events, packets counted: 0"}], confidence=0.9)
        ctx.note(f"jobs in Loki: {', '.join(sorted(jobs)) or 'none'}; missing: {', '.join(missing) or 'none'}")
