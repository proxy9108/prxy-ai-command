"""Capacity Planner: when will each disk fill? Least-squares trend over up to 7 days of Prometheus history for guest disks, Proxmox storages
and the host filesystems. Only a clear upward trend with a decent fit is reported; flat or noisy disks stay quiet."""
import time
from ..runner import Agent, bump
from ..sources import Prom, SourceError

SPECS = [  # (name, used query, size query, label->id)
    ("guest", 'pve_disk_usage_bytes{id=~"lxc/.*"}', 'pve_disk_size_bytes{id=~"lxc/.*"}', "id"),
    ("storage", 'pve_disk_usage_bytes{id=~"storage/.*"}', 'pve_disk_size_bytes{id=~"storage/.*"}', "id"),
]
HOST_MOUNTS = ("/", "/mnt/pve/backups")


def fit(values):
    """values: [(t_seconds, y)] -> (slope_per_day, r2, span_days) by least squares."""
    n = len(values)
    if n < 6:
        return 0.0, 0.0, 0.0
    xs = [(t - values[0][0]) / 86400 for t, _ in values]
    ys = [y for _, y in values]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    return slope, (1 - ss_res / ss_tot) if ss_tot else 0.0, xs[-1]


def project(values, size, horizon=90, min_r2=0.5, min_growth=0.0005):
    """Returns None when there is nothing worth reporting, else dict(days, slope_gb_day, r2, used)."""
    slope, r2, span = fit(values)
    used = values[-1][1]
    if span < 1.0 or slope <= 0 or r2 < min_r2 or size <= 0:
        return None
    days = (size - used) / slope
    if days > horizon or slope < size * min_growth:         # too far away, or growing under 0.05% of the disk a day: not worth a finding
        return None
    return {"days": days, "slope_gb_day": slope / 1e9, "r2": r2, "used": used, "span": span}


def accelerating(values, size, p):
    """Is the last 48 h growing clearly faster than the whole week? -> (days at the recent pace, ratio) or None. This is the 'it changed' signal behind a shrinking forecast."""
    recent = [v for v in values if v[0] >= values[-1][0] - 48 * 3600]
    slope, r2, span = fit(recent)
    if span < 1.0 or r2 < 0.6 or slope <= 0 or p["slope_gb_day"] <= 0:
        return None
    ratio = (slope / 1e9) / p["slope_gb_day"]
    return ((size - p["used"]) / slope, ratio) if ratio >= 1.5 else None


def severity(days):
    return "high" if days <= 7 else "medium" if days <= 30 else "low"


class CapacityPlanner(Agent):
    name = "capacity_planner"
    group = "operations"
    title = "Capacity Planner"
    autonomy = "R"
    interval_s = 6 * 3600
    auto_resolve = True
    PARAMS = {
        "horizon_days": dict(default=90, min=7, max=365, label="report disks forecast to fill within this many days", quieter="down", step=15, rules=["capacity:"]),
        "min_r2": dict(default=0.5, min=0.2, max=0.95, label="how steady (0-1) the growth must be before it is trusted", quieter="up", step=0.05, rules=["capacity:"]),
    }

    async def collect(self, prom):
        names = {r["metric"]["id"]: r["metric"].get("name", r["metric"]["id"]) for r in await prom.query("pve_guest_info")}
        jobs = []
        for kind, used_q, size_q, lab in SPECS:
            sizes = {r["metric"][lab]: r["value"] for r in await prom.query(size_q)}
            jobs.append((kind, used_q, sizes, lab))
        host_sizes = {r["metric"]["mountpoint"]: r["value"] for r in await prom.query('node_filesystem_size_bytes{mountpoint=~"/|/mnt/pve/backups"}')}
        jobs.append(("host", 'node_filesystem_size_bytes{mountpoint=~"/|/mnt/pve/backups"} - node_filesystem_avail_bytes{mountpoint=~"/|/mnt/pve/backups"}', host_sizes, "mountpoint"))
        return names, jobs

    async def run(self, ctx):
        prom, found, checked = Prom(), 0, 0
        try:
            names, jobs = await self.collect(prom)
        except SourceError as e:
            ctx.finding("source:prometheus", "high", "Capacity Planner cannot reach Prometheus", f"No forecasts can be made right now: {e}", [{"type": "error", "ref": "prometheus", "excerpt": str(e)}], confidence=1.0)
            ctx.no_data("Prometheus unreachable")
            return
        horizon, min_r2 = ctx.param("horizon_days"), ctx.param("min_r2")
        for kind, used_q, sizes, lab in jobs:
            ctx.working("scanning", f"{kind} disks, 7-day trend")
            try:
                series = await prom.range(used_q, hours=24 * 7, step=3600)
            except SourceError as e:
                ctx.note(f"{kind}: {e}")
                continue
            for s in series:
                key = s["metric"].get(lab, "?")
                size = sizes.get(key, 0)
                checked += 1
                p = project(s["values"], size, horizon, min_r2)
                ctx.checked(1)
                if not p:
                    continue
                label = names.get(key, key.split("/")[-1] if kind == "storage" else key)
                found += 1
                p["days"] = max(p["days"], 0.0)
                sev, extra = severity(p["days"]), ""
                acc = accelerating(s["values"], size, p)
                if acc:                                              # the recent pace is what the forecast will follow: say so, and treat it as more urgent
                    sev, extra = bump(sev), f" The last 2 days grew {acc[1]:.1f} times faster than the week as a whole; at that recent pace it would be full in about {max(acc[0], 0):.0f} days."
                tr = ctx.track(f"days:{key}", round(p["days"], 1))
                if tr["v24"] is not None and tr["v24"] - p["days"] >= 3:
                    extra += f" The forecast a day ago was {tr['v24']:.0f} days; it moved by {tr['v24'] - p['days']:.0f} days."
                when = "is about to fill up" if p["days"] < 1 else f"will be full in about {p['days']:.0f} days"
                ctx.finding(f"capacity:{kind}:{key}", sev, f"{label} {when}",
                            f"It has been growing {p['slope_gb_day']:.2f} GB per day over the last {p['span']:.0f} days (trend fit {p['r2']:.2f}) and is {p['used'] * 100 / size:.0f}% full. At this pace: {when}." + extra,
                            [{"type": "metric", "ref": used_q, "excerpt": f"{key}: used {p['used'] / 1e9:.1f} GB of {size / 1e9:.1f} GB, +{p['slope_gb_day']:.2f} GB/day, R2={p['r2']:.2f}, {p['span']:.1f} days of data"}], confidence=min(0.9, p["r2"]))
        ctx.note(f"{checked} disks forecast, {found} will fill within 90 days")

    SKILLS = {"forecast": {"about": "forecast when a disk or storage fills, and whether its growth is speeding up", "args": {"target": "name or id, e.g. backups, local-lvm, ollama, lxc/131 (empty = the fastest-filling disks)"}}}

    async def skill_forecast(self, ctx, args):
        target = str(args.get("target", "")).strip().lower()
        prom = Prom()
        names, jobs = await self.collect(prom)
        rows = []
        for kind, used_q, sizes, lab in jobs:
            for s in await prom.range(used_q, hours=24 * 7, step=3600):
                key = s["metric"].get(lab, "?"); size = sizes.get(key, 0); vals = s["values"]
                label = names.get(key, key.split("/")[-1] if kind == "storage" else key)
                if (target and target not in f"{key} {label}".lower()) or len(vals) < 6 or size <= 0:
                    continue
                used = vals[-1][1]
                slope7, r2, span = fit(vals)
                slope2, r22, span2 = fit([v for v in vals if v[0] >= vals[-1][0] - 48 * 3600])
                d7 = (size - used) / slope7 if slope7 > 0 and r2 >= 0.3 else None
                d2 = (size - used) / slope2 if slope2 > 0 and r22 >= 0.5 else None
                rows.append(dict(kind=kind, key=key, label=label, used=used, size=size, s7=slope7 / 1e9, s2=slope2 / 1e9, d7=d7, d2=d2, r2=r2, hist=[vals[i][1] / 1e9 for i in range(len(vals) - 1, -1, -24)][::-1]))
        tail = lambda r: r["label"].rstrip("/").split("/")[-1].lower()
        hosts = {tail(r) for r in rows if r["kind"] == "host"}
        rows = [r for r in rows if not (r["kind"] == "storage" and tail(r) in hosts)]          # the Proxmox storage and its host filesystem are the same disk: keep the filesystem view
        rows.sort(key=lambda r: min(x for x in (r["d7"], r["d2"], 1e9) if x is not None))
        rows = rows[:4 if target else 5]
        if not rows:
            return {"summary": f"No disk matches '{target}' in the metrics.", "facts": [], "followups": [], "recommend": [], "data": {}}
        facts, follow, rec = [], [], []
        for r in rows:
            line = f"{r['label']} ({r['kind']}): {r['used'] * 100 / r['size']:.0f}% full, {r['used'] / 1e9:.1f} GB of {r['size'] / 1e9:.1f} GB."
            line += f" Week trend {r['s7']:+.2f} GB/day" + (f", full in about {r['d7']:.0f} days." if r["d7"] else ".")
            if r["d2"] and r["s7"] > 0 and r["s2"] / r["s7"] >= 1.5:
                line += f" Last 2 days {r['s2']:+.2f} GB/day ({r['s2'] / r['s7']:.1f} times faster), full in about {r['d2']:.0f} days at that pace."
            facts.append(line)
            facts.append("  used per day (GB, oldest first): " + ", ".join(f"{h:.1f}" for h in r["hist"]))
            soon = min(x for x in (r["d7"], r["d2"], 1e9) if x is not None)
            if soon < 30:
                if "backup" in r["label"].lower() or "backup" in r["key"].lower():
                    follow.append({"agent": "backup_warden", "skill": "report", "args": {}, "why": f"{r['label']} is filling: which guests' backups take the space and what grew?"})
                    follow.append({"agent": "config_drift", "skill": "describe", "args": {"item": "backup"}, "why": "what are the backup job's and the storage's retention settings?"})
                elif r["key"].startswith(("lxc/", "qemu/")):
                    follow.append({"agent": "config_drift", "skill": "describe", "args": {"item": r["key"]}, "why": f"what is mounted on {r['label']} and how big is its disk?"})
                rec.append({"title": f"Make room on {r['label']} before it fills", "risk": "medium", "steps": [
                    f"Find out what uses the space: in Proxmox open {r['label']} (storage: Content, sorted by size; guest: Resources) or run 'du -xh --max-depth=1' inside the guest",
                    "Remove what you no longer need, or lower how many backups are kept (Datacenter > Backup > edit the job > Retention)",
                    "Run this project's question again tomorrow to confirm the growth has slowed"],
                    "rollback": ["Deleted files and pruned backups cannot be restored: copy anything you might still need elsewhere first", "If you changed the retention settings, set them back in Datacenter > Backup"]})
        keys = [f for f in facts if not f.startswith("  ")][:2]
        return {"summary": facts[0], "facts": facts, "key": keys, "followups": follow, "recommend": rec[:1], "data": {"disks": [{k: r[k] for k in ("label", "used", "size", "d7", "d2")} for r in rows]}}
