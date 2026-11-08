"""DNS Watcher: what the lab can actually see of DNS is thin. NextDNS keeps the per-domain logs in its own cloud and nothing resolver-side reaches Loki,
so this agent watches what IS exported: the NextDNS query counters (volume spikes, a collapse to zero, a jump in the blocked share) and whether the
local resolvers answer. It says plainly that per-domain analysis (DGA names, tunnelling, one device querying many new domains) needs resolver logs in Loki."""
import socket, struct, time, asyncio
from ..runner import Agent
from ..sources import Prom, SourceError

RESOLVERS = {"OPNsense resolver": "10.20.40.1"}


def dns_query(server, name="example.com", timeout=2.0, port=53):
    """Minimal UDP A query. Returns (ok, rcode, ms)."""
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    t = time.time()
    try:
        s.sendto(q, (server, port)); d, _ = s.recvfrom(512)
        return True, d[3] & 0x0F, round((time.time() - t) * 1000)
    except OSError:
        return False, None, None
    finally:
        s.close()


def analyse(total_series, blocked_series, spike_factor=5):
    """Series are lists of floats (oldest first, last = now). Returns [(fp, severity, title, summary, excerpt)]."""
    out = []
    if len(total_series) < 12:
        return out
    hist = sorted(total_series[:-1])
    med = hist[len(hist) // 2]
    now = total_series[-1]
    if med >= 100 and now > spike_factor * med and now > 500:
        out.append(("dns:volume-spike", "medium", "DNS query volume is far above normal", f"The NextDNS counter is {now:.0f}, over five times its usual level ({med:.0f}). A device may be looping or beaconing.", f"now {now:.0f} vs median {med:.0f}"))
    if med >= 100 and all(v == 0 for v in total_series[-6:]):
        out.append(("dns:volume-zero", "medium", "No DNS queries have reached NextDNS for a while", "The counter has been zero for the last stretch although it is normally busy. DNS may be broken, bypassed, or the exporter stalled.", f"last values all zero; median {med:.0f}"))
    if len(blocked_series) == len(total_series):
        ratios = [b / t for b, t in zip(blocked_series, total_series) if t >= 100]
        if len(ratios) >= 12 and total_series[-1] >= 500:
            h = sorted(ratios[:-1]); mr = h[len(h) // 2]
            if ratios[-1] - mr > 0.25:
                out.append(("dns:blocked-share-jump", "low", "An unusual share of DNS queries is being blocked", f"{ratios[-1] * 100:.0f}% of recent queries were blocked, against about {mr * 100:.0f}% normally. Could be an ad-heavy app, or malware trying blocked domains.", f"blocked share {ratios[-1] * 100:.0f}% vs median {mr * 100:.0f}%"))
    return out


class DnsWatcher(Agent):
    name = "dns_watcher"
    group = "security"
    title = "DNS Watcher"
    autonomy = "R"
    interval_s = 900
    auto_resolve = True
    PARAMS = {"spike_factor": dict(default=5, min=1.5, max=50, label="times the usual DNS query volume that counts as a spike", quieter="up", step=1, rules=["dns:volume-spike"])}

    async def run(self, ctx):
        prom = Prom()
        try:
            ctx.working("reading", "NextDNS counters, 24 h")
            tot = await prom.range("nextdns_total_queries", hours=24, step=300)
            blk = await prom.range("nextdns_blocked_queries", hours=24, step=300)
        except SourceError as e:
            ctx.finding("source:prometheus", "medium", "DNS Watcher cannot reach Prometheus", str(e), [{"type": "error", "ref": "prometheus", "excerpt": str(e)[:300]}], confidence=1.0)
            ctx.no_data("Prometheus unreachable")
            return
        if not tot or not tot[0]["values"] or time.time() - tot[0]["values"][-1][0] > 1800:
            ctx.finding("dns:exporter-stale", "medium", "The NextDNS exporter has stopped reporting", "No fresh NextDNS counters for over 30 minutes, so DNS statistics are blind (the Pi-hole series are also stale).",
                        [{"type": "metric", "ref": "nextdns_total_queries", "excerpt": "no sample in the last 30 minutes"}], confidence=0.85)
        else:
            ts, bs = [v for _, v in tot[0]["values"]], [v for _, v in blk[0]["values"]] if blk else []
            ctx.checked(len(ts))
            hist = sorted(ts[:-1]); med = hist[len(hist) // 2] if len(hist) > 11 else 0
            if med >= 100 and ts[-1] > 2.5 * med:                 # weak signal for the Correlator: network-wide, not tied to one machine
                ctx.signal("net:all", "dns-volume", min(0.6, 0.2 + 0.1 * ts[-1] / med), f"DNS query volume is {ts[-1] / med:.1f} times its usual level")
            for fp, sev, title, summ, ex in analyse(ts, bs, ctx.param("spike_factor")):
                ctx.finding(fp, sev, title, summ, [{"type": "metric", "ref": "nextdns_*_queries (24 h)", "excerpt": ex}], confidence=0.6)
        loop = asyncio.get_running_loop()
        for name, ip in RESOLVERS.items():
            ctx.working("scanning", f"test query to {name}")
            ok, rc, ms = await loop.run_in_executor(None, dns_query, ip)
            if not ok or rc != 0:
                ctx.finding(f"dns:resolver-down:{ip}", "high", f"{name} ({ip}) is not answering DNS", "A test lookup of example.com got no valid answer. Every device that uses it has lost name resolution.",
                            [{"type": "probe", "ref": f"udp/53 {ip}", "excerpt": "timeout" if not ok else f"rcode {rc}"}], confidence=0.9)
            elif ms > 1500:
                ctx.finding(f"dns:resolver-slow:{ip}", "low", f"{name} ({ip}) is slow: {ms} ms", "A test lookup took longer than 1.5 seconds.", [{"type": "probe", "ref": f"udp/53 {ip}", "excerpt": f"{ms} ms"}], confidence=0.7)
        ctx.finding("dns:visibility-gap", "low", "DNS Watcher cannot see which domains are queried",
                    "Per-domain checks (algorithm-generated names, DNS tunnelling, one device asking for many new domains) need resolver query logs in Loki. NextDNS keeps them in its cloud and nothing from OPNsense/Unbound is shipped here. Reject this finding to silence it, or ship the resolver's query log to Loki to enable those checks.",
                    [{"type": "config", "ref": "design", "excerpt": "Loki jobs: netflow, unifi_siem only"}], confidence=0.8)
        ctx.note("counters and resolver checked")
