"""Sentinel: reviews what the network can show it. Two feeds exist in Loki today: NetFlow (goflow2, from OPNsense) and UniFi CEF events.
(OPNsense firewall/Suricata syslog is NOT in Loki right now; IDS Doctor tracks that gap.) Deterministic rules only; no baselines of
identifying data are kept: it looks at the last 15 minutes of flows and the last hour of UniFi events, and evidence expires after 24 h."""
import os, collections, hashlib, ipaddress, json, time
from ..runner import Agent
from ..sources import Loki, SourceError

WINDOW_MIN, SLICE_S, SLICE_LIMIT = 15, 60, 5000          # one-minute slices stay under Loki's 5000-line cap
SENSITIVE_INBOUND = {22, 23, 445, 3389, 5900, 8006, 3306, 5432, 6379, 27017, 1883, 8123, 8443, 9090, 9100, 3100, 11434}
SUSPICIOUS_OUT = {23, 2323, 6667, 6697, 4444, 1337, 31337, 9001, 9030}
SCAN_PAIRS, SCAN_MEDIAN_BYTES = 200, 150
BIG_TRANSFER_MB = 300
NETS = {"20": "Home", "30": "Guest", "40": "Internal", "50": "IoT"}
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# infrastructure that legitimately talks to everything (firewall interfaces, Proxmox host): never reported as a scanner
INFRA = {"10.20.20.1", "10.20.30.1", "10.20.40.1", "10.20.50.1", "10.20.40.224", "10.20.40.139"}   # .139 = this system, whose Exposure Auditor scans on purpose
RESPONSE_PORTS = {53, 80, 123, 443, 853, 3478, 4500, 5353, 8443, 41641}      # a flow FROM these ports is almost always a reply


def kind(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "ext"
    if a in CGNAT:
        return "wan"
    return "int" if a.is_private else "ext"


def vlan_of(ip):
    p = ip.split(".")
    return NETS.get(p[2]) if len(p) == 4 and p[:2] == os.getenv("LAN_PREFIX", "10.20").split(".") else None


def mb(n):
    return f"{n / 1e6:.0f} MB"


class Sentinel(Agent):
    name = "sentinel"
    group = "security"
    title = "Sentinel"
    autonomy = "R"
    interval_s = 600
    auto_resolve = True
    PARAMS = {
        "scan_pairs": dict(default=200, min=50, max=5000, label="distinct destination host/port pairs in 15 minutes that look like a scan", quieter="up", step=50, rules=["scan:"]),
        "big_transfer_mb": dict(default=300, min=50, max=20000, label="MB sent to one outside address in 15 minutes that count as a large transfer", quieter="up", step=100, rules=["big-out:"]),
    }

    async def fetch_flows(self, loki):
        end, flows, truncated, failed = time.time(), [], False, 0
        n_slices = WINDOW_MIN * 60 // SLICE_S
        for i in range(n_slices):
            e = end - i * SLICE_S
            rows = None
            for attempt in range(2):                  # Loki occasionally stalls on one slice; retry once, then skip it
                try:
                    rows = await loki.query('{job="netflow"}', limit=SLICE_LIMIT, start=e - SLICE_S, end=e, timeout=15)
                    break
                except SourceError as err:
                    last = err
            if rows is None:
                failed += 1
                continue
            truncated |= len(rows) >= SLICE_LIMIT
            for r in rows:
                try:
                    f = json.loads(r["line"])
                    flows.append((f["src_addr"], f["dst_addr"], int(f.get("src_port") or 0), int(f.get("dst_port") or 0), f.get("proto"), int(f.get("bytes") or 0), int(f.get("packets") or 0), int(f.get("tcp_flags") or 0)))
                except (ValueError, KeyError, TypeError):
                    continue
        if failed == n_slices:
            raise last
        self.failed_slices = failed
        return flows, truncated or failed > 2

    async def run(self, ctx):
        loki = Loki()
        ctx.working("reading", "NetFlow, last 15 minutes")
        try:
            flows, truncated = await self.fetch_flows(loki)
        except SourceError as e:
            ctx.finding("source:loki", "high", "Sentinel cannot read Loki", f"No network logs can be reviewed right now: {e}", [{"type": "error", "ref": "loki", "excerpt": str(e)}], confidence=1.0)
            ctx.no_data("Loki unreachable")
            return
        ctx.checked(len(flows))
        self.scan_pairs, self.big_mb = ctx.param("scan_pairs"), ctx.param("big_transfer_mb")
        if getattr(self, "failed_slices", 0) > 3:
            ctx.partial = True                           # a third of the window could not be read: do not take 'not seen again' as 'cleared'
        for f in self.scans(flows, self.scan_pairs, near=True):
            for host in (f["src"],):
                if kind(host) == "int":
                    ctx.signal(f"host:{host}", "fanout", f["strength"], f"{host} contacted {f['pairs']} different host/port pairs in {WINDOW_MIN} minutes (below the scan threshold)")
        notes = []
        if len(flows) < 100:
            ctx.finding("feed:netflow-silent", "medium", "NetFlow feed is (almost) silent",
                        f"Only {len(flows)} flow records arrived in the last {WINDOW_MIN} minutes (a normal home network produces thousands). The collector (goflow2, LXC 128) may have stopped or its disk may be full.",
                        [{"type": "metric", "ref": '{job="netflow"} last 15m', "excerpt": f"{len(flows)} records"}], confidence=0.9)
        else:
            ctx.working("scanning", f"{len(flows)} flows")
            for f in self.scans(flows, self.scan_pairs) + self.inbound(flows) + self.suspicious_out(flows) + self.big_transfers(flows, self.big_mb):
                ctx.finding(f["fp"], f["sev"], f["title"], f["summary"] + (" (flow sample was truncated, counts may be low)" if truncated else ""), f["evidence"], confidence=f.get("conf", 0.8))
        ctx.working("reading", "UniFi events, last hour")
        try:
            lines = await loki.query('{job="unifi_siem"}', minutes=60, limit=1000)
            for f in self.unifi(lines):
                ctx.finding(f["fp"], f["sev"], f["title"], f["summary"], f["evidence"], confidence=f.get("conf", 0.9))
            day = await loki.query('{job="unifi_siem"}', minutes=720, limit=1)
            if not day:
                ctx.finding("feed:unifi-silent", "low", "No UniFi events for 12 hours", "The UniFi SIEM feed normally produces some events every few hours; the syslog path (UniFi -> rsyslog on LXC 133 -> promtail) may be broken.",
                            [{"type": "metric", "ref": '{job="unifi_siem"} last 12h', "excerpt": "0 events"}], confidence=0.6)
        except SourceError as e:
            notes.append(f"UniFi feed unreadable: {e}")
        ctx.note(f"{len(flows)} flows reviewed{' (truncated)' if truncated else ''}" + ("; " + "; ".join(notes) if notes else ""))

    # ---- detections (pure functions over parsed flows; tested with synthetic data)
    @staticmethod
    def scans(flows, threshold=SCAN_PAIRS, near=False):
        """near=True returns the hosts that are between half the threshold and the threshold: weak signals for the Correlator, not findings."""
        per = collections.defaultdict(lambda: {"pairs": set(), "bytes": []})
        for s, d, sp, dp, pr, b, pk, fl in flows:
            if s in INFRA or ((sp < 1024 or sp in RESPONSE_PORTS) and dp >= 1024):
                continue                               # infrastructure, or a reply to something a device started
            x = per[s]; x["pairs"].add((d, dp)); x["bytes"].append(b)
        out = []
        for src, x in per.items():
            if near:
                if threshold / 2 <= len(x["pairs"]) < threshold and sorted(x["bytes"])[len(x["bytes"]) // 2] <= SCAN_MEDIAN_BYTES:
                    out.append(dict(src=src, pairs=len(x["pairs"]), strength=round(0.2 + 0.3 * len(x["pairs"]) / threshold, 2)))
                continue
            if len(x["pairs"]) < threshold:
                continue
            med = sorted(x["bytes"])[len(x["bytes"]) // 2]
            if med > SCAN_MEDIAN_BYTES:
                continue
            k = kind(src)
            if k == "wan":
                continue                               # the WAN address after NAT is every device at once: not attributable
            sev = "high" if k == "ext" else ("medium" if vlan_of(src) in ("IoT", "Guest") else "low")
            where = f"{src} ({vlan_of(src) or k})"
            out.append(dict(fp=f"scan:{src}", sev=sev, title=f"{where} contacted {len(x['pairs'])} different host/port pairs in {WINDOW_MIN} minutes",
                            summary=f"That is the shape of a scan: many destinations, tiny flows (median {med} bytes). {'An outside address probing the network.' if k == 'ext' else 'An internal device fanning out; check what it is running.'}",
                            evidence=[{"type": "netflow", "ref": f"src {src}", "excerpt": f"{len(x['pairs'])} distinct (dst,port) pairs, {len(x['bytes'])} flows, median {med} bytes"}], conf=0.75))
        return out

    @staticmethod
    def inbound(flows):
        hits = collections.defaultdict(lambda: {"srcs": set(), "n": 0})
        for s, d, sp, dp, pr, b, pk, fl in flows:
            if kind(s) == "ext" and kind(d) == "int" and pr == "TCP" and dp in SENSITIVE_INBOUND and fl == 2:
                h = hits[(d, dp)]; h["srcs"].add(s); h["n"] += 1
        return [dict(fp=f"inbound:{d}:{dp}", sev="high", title=f"Outside connection attempt to {d}:{dp}",
                     summary="An outside address sent a first-packet-only (SYN) connection to a sensitive service. With carrier-grade NAT this should not normally happen, so treat it as unexpected until explained.",
                     evidence=[{"type": "netflow", "ref": f"dst {d}:{dp}", "excerpt": f"{h['n']} attempts from {len(h['srcs'])} source(s): {', '.join(sorted(h['srcs'])[:5])}"}], conf=0.7) for (d, dp), h in hits.items()]

    @staticmethod
    def suspicious_out(flows):
        hits = collections.defaultdict(lambda: {"dsts": set(), "n": 0})
        for s, d, sp, dp, pr, b, pk, fl in flows:
            if kind(s) in ("int", "wan") and kind(d) == "ext" and dp in SUSPICIOUS_OUT:
                h = hits[(s, dp)]; h["dsts"].add(d); h["n"] += 1
        return [dict(fp=f"susp-out:{s}:{dp}", sev="medium", title=f"{s} connected out on unusual port {dp}",
                     summary=f"Port {dp} is associated with telnet, IRC, Tor or backdoor tooling. It may be harmless, but nothing on the lab should need it.",
                     evidence=[{"type": "netflow", "ref": f"src {s} -> port {dp}", "excerpt": f"{h['n']} flows to {len(h['dsts'])} address(es): {', '.join(sorted(h['dsts'])[:5])}"}], conf=0.6) for (s, dp), h in hits.items()]

    @staticmethod
    def big_transfers(flows, threshold_mb=BIG_TRANSFER_MB):
        tot = collections.Counter()
        for s, d, sp, dp, pr, b, pk, fl in flows:
            if kind(s) in ("int", "wan") and kind(d) == "ext":
                tot[(s, d)] += b
        return [dict(fp=f"big-out:{s}:{d}", sev="medium" if v > 1e9 else "low", title=f"{mb(v)} sent from {s} to {d} in {WINDOW_MIN} minutes",
                     summary="A large outbound transfer to one outside address. Often a backup or an upload, but it is also what data theft looks like.",
                     evidence=[{"type": "netflow", "ref": f"{s} -> {d}", "excerpt": f"{mb(v)} in {WINDOW_MIN} min"}], conf=0.5) for (s, d), v in tot.items() if v >= threshold_mb * 1e6]

    @staticmethod
    def unifi(lines):
        out, joins = [], 0
        for r in lines:
            parts = r["line"].split("CEF:0|", 1)[-1].split("|")
            if len(parts) < 7:
                continue
            name, sev = parts[4], parts[5]
            low = name.lower()
            ev = [{"type": "log", "ref": "unifi_siem", "excerpt": r["line"][:400]}]
            fp = f"unifi:{name}:{hashlib.sha1(r['line'].encode()).hexdigest()[:8]}"
            if any(w in low for w in ("failed", "denied", "unauthorized", "blocked", "intrusion", "threat", "rogue", "ips", "attack")):
                out.append(dict(fp=fp, sev="high", title=f"UniFi: {name}", summary="UniFi reported a security-relevant event.", evidence=ev))
            elif "admin" in low and ("login" in low or "logged" in low):
                out.append(dict(fp=fp, sev="info", title=f"UniFi: {name}", summary="An administrator signed in to the UniFi controller.", evidence=ev))
            elif "config modified" in low:
                out.append(dict(fp=fp, sev="low", title="UniFi configuration was modified", summary="Someone changed the UniFi network configuration. If that was not you, investigate.", evidence=ev))
            elif any(low.startswith(p) for p in ("device", "switch", "ap ", "gateway", "wireless uplink")) and any(w in low for w in ("disconnected", "lost", "offline", "restarted")):
                out.append(dict(fp=fp, sev="medium", title=f"UniFi: {name}", summary="A network device dropped or restarted.", evidence=ev))
            elif "client connected" in low:
                joins += 1
        if joins > 30:
            out.append(dict(fp="unifi:join-spike", sev="low", title=f"{joins} client connections in the last hour", summary="Unusually many devices joined the network. Check for a new or misbehaving device.",
                            evidence=[{"type": "metric", "ref": "unifi_siem Client Connected 1h", "excerpt": f"{joins} events"}], conf=0.5))
        return out

    SKILLS = {"network_activity": {"about": "what one machine has been talking to in the last 15 minutes: volume, busiest peers, ports, fan-out", "args": {"host": "an internal address, e.g. 10.20.50.20"}}}

    async def skill_network_activity(self, ctx, args):
        host = str(args.get("host", "")).strip()
        if kind(host) != "int":
            return {"summary": "Only internal addresses can be looked at.", "facts": [], "followups": [], "recommend": [], "data": {}}
        flows, truncated = await self.fetch_flows(Loki())
        mine = [f for f in flows if f[0] == host or f[1] == host]
        if not mine:
            return {"summary": f"No flows involving {host} in the last {WINDOW_MIN} minutes.", "facts": [f"{len(flows)} flows were read in total."], "followups": [], "recommend": [], "data": {}}
        out_b = sum(f[5] for f in mine if f[0] == host); in_b = sum(f[5] for f in mine if f[1] == host)
        peers = collections.Counter(); ports = collections.Counter()
        for f in mine:
            other, port = (f[1], f[3]) if f[0] == host else (f[0], f[2])
            peers[other] += f[5]; ports[port] += 1
        pairs = len({(f[1], f[3]) for f in mine if f[0] == host})
        outside = len({p for p in peers if kind(p) == "ext"})
        facts = [f"{host} ({vlan_of(host) or 'unknown network'}): {len(mine)} flows in {WINDOW_MIN} minutes, {mb(out_b)} sent, {mb(in_b)} received.",
                 f"It contacted {pairs} different host/port pairs and {outside} different outside addresses.",
                 "Busiest peers: " + ", ".join(f"{p} ({mb(b)})" for p, b in peers.most_common(4)) + ".",
                 "Most used ports: " + ", ".join(f"{p} ({n} flows)" for p, n in ports.most_common(4)) + "."]
        odd = sorted(p for p in ports if p in SUSPICIOUS_OUT)
        if odd:
            facts.append("Unusual ports in use: " + ", ".join(map(str, odd)) + ".")
        follow = []
        if pairs >= 100 or odd:
            follow.append({"agent": "exposure_auditor", "skill": "scan", "args": {"hosts": [host]}, "why": f"{host} fans out widely: what does it have open itself?"})
        if vlan_of(host) == "IoT":
            follow.append({"agent": "iot_watcher", "skill": "device", "args": {"host": host}, "why": f"how does {host} normally behave?"})
        return {"summary": facts[0], "facts": facts, "followups": follow, "recommend": [], "data": {"flows": len(mine), "pairs": pairs, "outside": outside}}
