"""Alert Triage: there are no Prometheus alert rules, so this agent plays that role. Deterministic rules over Prometheus metrics,
sustained conditions only (no single-sample flapping), every finding carries the query and value as evidence. Findings clear on their own."""
import re, time
from ..runner import Agent, bump
from ..sources import Prom, SourceError

IGNORE_GUESTS = {"qemu/100", "qemu/102"}                 # stopped on purpose (HOMELAB_HANDOFF)
IGNORE_MOUNTS = {"/etc/pve", "/boot/efi", "/var/lib/lxcfs"}     # pseudo/always-full filesystems on a Proxmox host
SEV_ORDER = ["info", "low", "medium", "high", "critical"]


def pct(v):
    return f"{v * 100:.0f}%"


class AlertTriage(Agent):
    name = "alert_triage"
    group = "operations"
    title = "Alert Triage"
    autonomy = "R"
    interval_s = 300
    auto_resolve = True
    ctx = None

    def _p(self, name):
        return self.ctx.param(name) if self.ctx else self.PARAMS[name]["default"]

    PARAMS = {
        "guest_disk_warn": dict(default=0.85, min=0.5, max=0.99, label="guest disk usage (share) that raises a finding", quieter="up", step=0.03, rules=["guest-disk:"]),
        "storage_warn": dict(default=0.85, min=0.5, max=0.99, label="Proxmox storage usage (share) that raises a finding", quieter="up", step=0.03, rules=["storage:"]),
        "host_fs_free_warn": dict(default=0.15, min=0.01, max=0.5, label="free space (share) on a host filesystem below which a finding is raised", quieter="down", step=0.03, rules=["host-fs:"]),
        "guest_mem_warn": dict(default=0.95, min=0.5, max=0.999, label="guest memory usage (share, 15 min average) that raises a finding", quieter="up", step=0.02, rules=["guest-mem:"]),
        "jump_points": dict(default=0.08, min=0.02, max=0.5, label="rise (share of a disk in 24 h) that counts as a sudden jump and raises the severity", quieter="up", step=0.02, rules=[]),
    }

    async def run(self, ctx):
        prom = Prom()
        names = {}
        try:
            ctx.working("reading", "guest names and targets")
            for r in await prom.query("pve_guest_info"):
                names[r["metric"]["id"]] = r["metric"].get("name", r["metric"]["id"])
        except SourceError as e:
            ctx.finding("source:prometheus", "high", "Alert Triage cannot reach Prometheus", f"Nothing is being watched right now: {e}",
                        [{"type": "error", "ref": "prometheus", "excerpt": str(e)}], confidence=1.0)
            ctx.no_data("Prometheus unreachable")
            return
        checks = [self.targets_down, self.guests_down, self.guest_disk, self.guest_memory, self.storage, self.host_fs,
                  self.host_memory, self.host_temps, self.container_restarts, self.container_missing, self.not_backed_up, self.ups]
        problems = 0
        self.ctx = ctx
        ctx.checked(len(names))
        for chk in checks:
            ctx.working("scanning", chk.__name__.replace("_", " "))
            try:
                for f in await chk(prom, names):
                    ctx.finding(f["fp"], f["sev"], f["title"], f["summary"], f["evidence"], proposed_action=f.get("action"), confidence=f.get("conf", 0.9))
                    problems += 1
            except SourceError as e:
                ctx.note(f"{chk.__name__} could not run: {e}")
        ctx.note(f"{len(checks)} checks, {problems} condition(s) active")
        if ctx.created:
            try:
                ctx.working("thinking", "one-line overview")
                titles = "; ".join(f["title"] for f in ctx.runner.store.list_findings(self.name, "new", limit=ctx.created))
                r = await ctx.llm([{"role": "system", "content": "You write ONE short plain sentence summarising the alert titles. Do not add facts."},
                                   {"role": "user", "content": titles}], options={"num_predict": 50})
                ctx.note(f"{len(checks)} checks, {problems} active. {r['text']}")
            except Exception:
                pass

    @staticmethod
    def ev(promql, value, extra=""):
        return [{"type": "metric", "ref": promql, "excerpt": f"{value}{(' ' + extra) if extra else ''}"}]

    async def targets_down(self, prom, names):
        q = "min_over_time(up[3m]) == 0"
        return [dict(fp=f"target-down:{r['metric'].get('job')}:{r['metric'].get('instance')}", sev="high",
                     title=f"Metrics target '{r['metric'].get('job')}' is down",
                     summary=f"Prometheus has not been able to scrape {r['metric'].get('instance')} for at least 3 minutes, so everything that depends on it is blind.",
                     evidence=self.ev(q, "up=0", r["metric"].get("instance", ""))) for r in await prom.query(q)]

    async def guests_down(self, prom, names):
        q = "(pve_up == 0) and on(id) (pve_onboot_status == 1)"
        out = []
        for r in await prom.query(q):
            gid = r["metric"]["id"]
            if gid in IGNORE_GUESTS:
                continue
            typ, _, num = gid.partition("/")
            cmd = f"pct start {num}" if typ == "lxc" else f"qm start {num}"
            out.append(dict(fp=f"guest-down:{gid}", sev="high", title=f"{names.get(gid, gid)} ({gid}) is not running",
                            summary="This guest is set to start on boot but is currently stopped.", evidence=self.ev(q, "pve_up=0", gid),
                            action={"kind": "manual", "risk": "low",
                                    "steps": [f"Check why it stopped: Proxmox > {names.get(gid, gid)} > task history (or a host reboot / backup in progress)", f"Start it: in the Proxmox web UI, or on the host: {cmd}", "Check that the service on it answers"],
                                    "rollback": [f"Shut it down again from the Proxmox UI or: {'pct shutdown' if typ == 'lxc' else 'qm shutdown'} {num}"]}))
        return out

    async def day_ago(self, prom, q):
        """Value of the same query 24 h ago, by series: what turns 'is at 85%' into 'went from 60% to 85% overnight'."""
        try:
            return {tuple(sorted(r["metric"].items())): r["value"] for r in await prom.query(f"({q}) offset 1d")}
        except SourceError:
            return {}

    def trend(self, v, before, sev):
        """-> (severity, extra sentence). A sudden rise lifts the severity one level; a condition that has not moved in two weeks is said to be standing."""
        if before is None:
            return sev, ""
        jump = self._p("jump_points")
        if v - before >= jump:
            return bump(sev), f" It rose from {pct(before)} to {pct(v)} in the last 24 hours, which is a sudden change."
        return sev, f" It was {pct(before)} a day ago." if abs(v - before) >= 0.01 else " It has not moved in the last day."

    async def guest_disk(self, prom, names):
        q = 'pve_disk_usage_bytes{id=~"lxc/.*"} / pve_disk_size_bytes{id=~"lxc/.*"}'
        out, prev, warn = [], await self.day_ago(prom, q), self._p("guest_disk_warn")
        for r in await prom.query(q):
            v, gid = r["value"], r["metric"]["id"]
            if v >= warn:
                sev, extra = self.trend(v, prev.get(tuple(sorted(r["metric"].items()))), "high" if v >= 0.93 else "medium")
                out.append(dict(fp=f"guest-disk:{gid}", sev=sev, title=f"{names.get(gid, gid)} disk is {pct(v)} full",
                                summary=f"The root disk of {names.get(gid, gid)} ({gid}) is {pct(v)} used. Free space or grow it before it fills." + extra, evidence=self.ev(q, pct(v), gid)))
        return out

    async def guest_memory(self, prom, names):
        q = 'avg_over_time((pve_memory_usage_bytes{id=~"lxc/.*|qemu/.*"} / pve_memory_size_bytes{id=~"lxc/.*|qemu/.*"})[15m:1m])'
        out = []
        for r in await prom.query(q):
            v, gid = r["value"], r["metric"]["id"]
            if v >= self._p("guest_mem_warn"):
                out.append(dict(fp=f"guest-mem:{gid}", sev="medium", title=f"{names.get(gid, gid)} memory is {pct(v)} used",
                                summary=f"Average over 15 minutes. If it keeps climbing the guest may start killing processes.", evidence=self.ev(q, pct(v), gid), conf=0.7))
        return out

    async def storage(self, prom, names):
        q = 'pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"}'
        out, prev, warn = [], await self.day_ago(prom, q), self._p("storage_warn")
        for r in await prom.query(q):
            v, sid = r["value"], r["metric"]["id"]
            short = sid.split("/")[-1]
            if v >= warn:
                sev, extra = self.trend(v, prev.get(tuple(sorted(r["metric"].items()))), "high" if v >= 0.95 else "medium")
                out.append(dict(fp=f"storage:{short}", sev=sev, title=f"Storage '{short}' is {pct(v)} full",
                                summary=f"Proxmox storage {short} is {pct(v)} used. Thin pools can stall every guest when they fill, so plan space before it gets there." + extra, evidence=self.ev(q, pct(v), sid)))
        return out

    async def host_fs(self, prom, names):
        flt = 'fstype!~"tmpfs|overlay|squashfs|ramfs|fuse.*|devtmpfs"'
        q = f"node_filesystem_avail_bytes{{{flt}}} / node_filesystem_size_bytes{{{flt}}}"
        out = []
        for r in await prom.query(q):
            mp = r["metric"].get("mountpoint", "?")
            if mp in IGNORE_MOUNTS:
                continue
            free = r["value"]
            if free <= self._p("host_fs_free_warn"):
                out.append(dict(fp=f"host-fs:{mp}", sev="high" if free <= 0.08 else "medium", title=f"Proxmox host '{mp}' has {pct(free)} free",
                                summary=f"Only {pct(free)} of {mp} is free on the Proxmox host.", evidence=self.ev(q, f"free={pct(free)}", mp)))
        return out

    async def host_memory(self, prom, names):
        q = "avg_over_time((node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)[10m:1m])"
        return [dict(fp="host-mem", sev="medium", title=f"Proxmox host has only {pct(r['value'])} memory available",
                     summary="Averaged over 10 minutes. The host has no room to spare for guests.", evidence=self.ev(q, pct(r["value"]))) for r in await prom.query(q) if r["value"] <= 0.10]

    async def host_temps(self, prom, names):
        q = 'node_hwmon_temp_celsius{chip=~"nvme.*|platform_coretemp.*"}'
        out = []
        for r in await prom.query(q):
            lim = 70 if "nvme" in r["metric"].get("chip", "") else 85
            if r["value"] >= lim:
                out.append(dict(fp=f"temp:{r['metric'].get('chip')}:{r['metric'].get('sensor')}", sev="medium", title=f"{r['metric'].get('chip')} is at {r['value']:.0f} °C",
                                summary="Above the comfortable range for this part. Check airflow and load.", evidence=self.ev(q, f"{r['value']:.0f} C", r["metric"].get("chip", ""))))
        return out

    async def container_restarts(self, prom, names):
        q = 'changes(container_start_time_seconds{name!=""}[1h]) >= 3'
        return [dict(fp=f"restart-loop:{r['metric'].get('name')}", sev="medium", title=f"Container '{r['metric'].get('name')}' restarted {int(r['value'])} times in an hour",
                     summary="A container that keeps restarting is usually crashing on start. Check its logs.", evidence=self.ev(q, f"{int(r['value'])} restarts")) for r in await prom.query(q)]

    async def container_missing(self, prom, names):
        q = 'time() - container_last_seen{name!=""} > 900'
        return [dict(fp=f"container-missing:{r['metric'].get('name')}", sev="medium", title=f"Container '{r['metric'].get('name')}' has disappeared",
                     summary=f"cAdvisor has not seen it for {int(r['value'] // 60)} minutes: it is stopped or removed.", evidence=self.ev(q, f"{int(r['value'])} s since last seen")) for r in await prom.query(q)]

    async def not_backed_up(self, prom, names):
        q = "pve_not_backed_up_info"
        return [dict(fp=f"not-backed-up:{r['metric']['id']}", sev="medium", title=f"{names.get(r['metric']['id'], r['metric']['id'])} is not covered by any backup job",
                     summary="No backup job includes this guest.", evidence=self.ev(q, "not backed up", r["metric"]["id"]), conf=0.8) for r in await prom.query(q) if r["metric"]["id"] not in IGNORE_GUESTS]

    async def ups(self, prom, names):
        seen = await prom.query("time() - aferiy_last_seen_unixtime")
        if not seen or seen[0]["value"] > 6 * 3600:
            return []                             # known: the UPS only reports when its app wakes it, so old readings are not alarms
        out = []
        q = "aferiy_battery_percent"
        for r in await prom.query(q):
            if r["value"] < 30:
                out.append(dict(fp="ups-battery", sev="high" if r["value"] < 15 else "medium", title=f"UPS battery at {r['value']:.0f}%",
                                summary="The UPS battery is low and its reading is recent.", evidence=self.ev(q, f"{r['value']:.0f}%")))
        return out

    SKILLS = {"metric_now": {"about": "current usage of disks, storages and host filesystems, and how they moved in the last day", "args": {"target": "optional name or id to focus on"}}}

    async def skill_metric_now(self, ctx, args):
        target, prom, facts = str(args.get("target", "")).strip().lower(), Prom(), []
        names = {r["metric"]["id"]: r["metric"].get("name", r["metric"]["id"]) for r in await prom.query("pve_guest_info")}
        for label, q in (("guest disk", 'pve_disk_usage_bytes{id=~"lxc/.*"} / pve_disk_size_bytes{id=~"lxc/.*"}'), ("storage", 'pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"}')):
            prev = await self.day_ago(prom, q)
            for r in await prom.query(q):
                gid = r["metric"]["id"]; nm = names.get(gid, gid.split("/")[-1])
                if target and target not in f"{gid} {nm}".lower():
                    continue
                b = prev.get(tuple(sorted(r["metric"].items())))
                facts.append(f"{nm} ({label}): {pct(r['value'])} used now" + (f", {pct(b)} a day ago ({(r['value'] - b) * 100:+.1f} points)." if b is not None else "."))
        flt = 'fstype!~"tmpfs|overlay|squashfs|ramfs|fuse.*|devtmpfs"'
        for r in await prom.query(f"node_filesystem_avail_bytes{{{flt}}} / node_filesystem_size_bytes{{{flt}}}"):
            mp = r["metric"].get("mountpoint", "?")
            if mp in IGNORE_MOUNTS or (target and target not in mp.lower()):
                continue
            facts.append(f"host filesystem {mp}: {pct(1 - r['value'])} used.")
        if not facts:
            return {"summary": f"No metric matches '{target}'.", "facts": [], "followups": [], "recommend": [], "data": {}}
        facts.sort(key=lambda f: -int(re.search(r"(\d+)% used", f).group(1)) if re.search(r"(\d+)% used", f) else 0)
        return {"summary": facts[0], "facts": facts[:8], "followups": [], "recommend": [], "data": {}}
