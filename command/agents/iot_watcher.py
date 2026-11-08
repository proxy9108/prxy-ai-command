"""IoT Watcher: how do the devices on the IoT VLAN (10.20.50.0/24) behave, and does one suddenly change?
Every run summarises the last 10 minutes of NetFlow per device (bytes, number of distinct outside addresses). It keeps a rolling 24 HOURS of those
buckets (the privacy rule: nothing identifying is kept longer) and flags a device whose outside-peer count or traffic jumps far above its own
median, and a device that appears for the first time. It says 'learning' until it has about a day of history."""
import collections, ipaddress, time
from ..runner import Agent
from ..sources import Loki, SourceError
import json

IOT = ipaddress.ip_network("10.20.50.0/24")
WINDOW_S, KEEP_S = 600, 24 * 3600


def summarise(flows):
    """flows: (src, dst, sport, dport, proto, bytes, ...) -> {device_ip: {'bytes': n, 'peers': k}} for addresses on the IoT VLAN."""
    dev = collections.defaultdict(lambda: {"bytes": 0, "peers": set()})
    for f in flows:
        s, d, b = f[0], f[1], f[5]
        for me, other in ((s, d), (d, s)):
            try:
                if ipaddress.ip_address(me) in IOT:
                    dev[me]["bytes"] += b
                    if not ipaddress.ip_address(other).is_private:
                        dev[me]["peers"].add(other)
            except ValueError:
                continue
    return {ip: {"bytes": v["bytes"], "peers": len(v["peers"])} for ip, v in dev.items()}


def judge(hist, now_bucket, ts, learning_hours=20, peers_factor=4, peers_min=15, bytes_factor=10, bytes_min_mb=50):
    """hist: {ip: [[ts, bytes, peers], ...]} (older buckets); now_bucket: {ip: {...}} -> [(fp, sev, title, summary, excerpt)]."""
    out = []
    span = (ts - min((b[0] for v in hist.values() for b in v), default=ts)) / 3600
    for ip, cur in now_bucket.items():
        past = hist.get(ip, [])
        if len(past) < 8:
            if span >= learning_hours and not past:
                out.append((f"iot:new:{ip}", "low", f"New device on the IoT VLAN: {ip}", "This address had no traffic in the previous day. A new gadget, or something that should not be there.", f"first seen now; {cur['peers']} outside peers, {cur['bytes'] // 1000} KB"))
            continue
        pm = sorted(b[2] for b in past)[len(past) // 2]
        bm = sorted(b[1] for b in past)[len(past) // 2]
        if cur["peers"] > max(peers_min, peers_factor * pm):
            out.append((f"iot:peers:{ip}", "medium", f"{ip} is talking to {cur['peers']} different outside addresses", f"Its usual is about {pm}. A device that suddenly contacts many new hosts may be compromised or scanning.", f"{cur['peers']} peers vs median {pm}"))
        if cur["bytes"] > max(bytes_min_mb * 1e6, bytes_factor * bm):
            out.append((f"iot:bytes:{ip}", "low", f"{ip} moved {cur['bytes'] / 1e6:.0f} MB in 10 minutes", f"Its usual is about {bm / 1e6:.1f} MB. Streaming or an update is common; a sudden jump can also be data leaving.", f"{cur['bytes'] / 1e6:.0f} MB vs median {bm / 1e6:.1f} MB"))
    return out


class IotWatcher(Agent):
    name = "iot_watcher"
    group = "security"
    title = "IoT Watcher"
    autonomy = "R"
    interval_s = 600
    auto_resolve = True
    PARAMS = {
        "peers_factor": dict(default=4, min=1.5, max=50, label="times a device's usual number of outside peers that counts as a jump", quieter="up", step=1, rules=["iot:peers:"]),
        "peers_min": dict(default=15, min=3, max=500, label="fewest outside peers before a jump is reported at all", quieter="up", step=5, rules=["iot:peers:"]),
        "bytes_factor": dict(default=10, min=2, max=200, label="times a device's usual traffic that counts as a jump", quieter="up", step=2, rules=["iot:bytes:"]),
        "bytes_min_mb": dict(default=50, min=5, max=5000, label="smallest 10-minute transfer (MB) worth reporting", quieter="up", step=25, rules=["iot:bytes:"]),
    }

    async def run(self, ctx):
        loki, end, flows = Loki(), time.time(), []
        for i in range(WINDOW_S // 60):
            e = end - i * 60
            try:
                for r in await loki.query('{job="netflow"}', limit=5000, start=e - 60, end=e, timeout=15):
                    try:
                        f = json.loads(r["line"]); flows.append((f["src_addr"], f["dst_addr"], 0, 0, f.get("proto"), int(f.get("bytes") or 0)))
                    except (ValueError, KeyError, TypeError):
                        pass
            except SourceError:
                continue
        if not flows:
            ctx.no_data("no flows readable this run")
            return
        ctx.working("scanning", f"{len(flows)} flows, IoT VLAN")
        cur = summarise(flows)
        hist = ctx.kv_get("hist", {})
        ctx.checked(len(cur))
        p = {k: ctx.param(k) for k in ("peers_factor", "peers_min", "bytes_factor", "bytes_min_mb")}
        found = judge(hist, cur, end, **p)
        for ip, v in cur.items():                                # weak signals for the Correlator: a device clearly above its own usual, but below the finding threshold
            past = hist.get(ip, [])
            if len(past) >= 8:
                pm = sorted(b[2] for b in past)[len(past) // 2]
                if v["peers"] > max(5, 2 * pm) and not any(f[0] == f"iot:peers:{ip}" for f in found):
                    ctx.signal(f"host:{ip}", "peers-up", 0.35, f"{ip} talks to {v['peers']} outside addresses, about twice its usual of {pm}")
        for fp, sev, title, summ, ex in found:
            ctx.finding(fp, sev, title, summ, [{"type": "netflow", "ref": "10.20.50.0/24, 10 min", "excerpt": ex}], confidence=0.6)
        for ip, v in cur.items():                                # append this bucket, drop anything older than 24 h
            hist.setdefault(ip, []).append([end, v["bytes"], v["peers"]])
        hist = {ip: [b for b in bs if end - b[0] < KEEP_S] for ip, bs in hist.items()}
        ctx.kv_set("hist", {ip: bs for ip, bs in hist.items() if bs})
        learning = (end - min((b[0] for v in hist.values() for b in v), default=end)) / 3600 < 20
        ctx.note(f"{len(cur)} IoT device(s) seen" + (" (learning: needs about a day of history before it judges)" if learning else "") + f"; {len(found)} to look at")

    SKILLS = {"device": {"about": "how one IoT device normally behaves over the last 24 hours (traffic, outside peers) and how it looks now", "args": {"host": "its address, e.g. 10.20.50.20"}}}

    async def skill_device(self, ctx, args):
        host = str(args.get("host", "")).strip()
        past = ctx.kv_get("hist", {}).get(host, [])
        if len(past) < 3:
            return {"summary": f"{host}: not enough history yet ({len(past)} samples of 10 minutes).", "facts": [], "followups": [], "recommend": [], "data": {}}
        med = lambda xs: sorted(xs)[len(xs) // 2]
        cur = past[-1]
        facts = [f"{host}: {len(past)} samples over the last 24 hours (10 minutes each).",
                 f"Traffic per 10 minutes: usual {med([b[1] for b in past]) / 1e6:.1f} MB, highest {max(b[1] for b in past) / 1e6:.1f} MB, latest {cur[1] / 1e6:.1f} MB.",
                 f"Outside peers per 10 minutes: usual {med([b[2] for b in past])}, highest {max(b[2] for b in past)}, latest {cur[2]}."]
        return {"summary": facts[1], "facts": facts, "followups": [], "recommend": [], "data": {}}
