"""Exposure Auditor: which network services are reachable on our own machines, and is anything new or risky?
A polite TCP connect scan (bounded concurrency, short timeouts, a fixed list of ~60 well-known ports) of hosts that belong to us: the Proxmox
guests' addresses, the host, the gateways and the UniFi/UPS devices. Private addresses only. The first scan records the baseline of open ports;
later scans report NEW open ports and always report risky services. Mark a finding 'done' to accept that host:port as expected.
It scans weekly (and when asked), never at every restart. Scanned from this VLAN only: it shows what the internal network can reach."""
import asyncio, ipaddress, re, time
from ..runner import Agent
from ..sources import PVE, SourceError

PORTS = [21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 389, 443, 445, 465, 514, 587, 631, 873, 993, 995, 1433, 1521, 1883, 2049, 2375, 2376, 3000, 3100, 3306, 3389, 4000, 5000, 5353, 5432, 5601,
         5900, 5984, 6379, 7575, 8000, 8006, 8080, 8081, 8086, 8088, 8123, 8300, 8310, 8320, 8443, 8883, 8888, 9000, 9090, 9093, 9100, 9200, 9221, 11211, 11434, 27017]
EXTRA_HOSTS = ["10.20.40.1", "10.20.40.105", "10.20.40.106", "10.20.40.224", "10.20.20.1", "10.20.30.1", "10.20.50.1"]
RISKY = {23: ("high", "Telnet sends passwords in clear text"), 21: ("medium", "FTP is unencrypted"), 3389: ("high", "Remote Desktop exposed"), 445: ("medium", "SMB file sharing"), 139: ("medium", "NetBIOS file sharing"),
         5900: ("medium", "VNC remote screen"), 2375: ("critical", "Docker API without TLS gives root on the host to anyone who can connect"), 6379: ("medium", "Redis usually has no password"),
         27017: ("medium", "MongoDB reachable over the network"), 11211: ("medium", "memcached reachable over the network"), 9200: ("medium", "Elasticsearch reachable over the network"),
         3306: ("low", "MySQL reachable over the network"), 5432: ("low", "PostgreSQL reachable over the network"), 1883: ("low", "MQTT broker (check it requires a login)"), 111: ("low", "rpcbind")}


def classify(open_ports, baseline, accepted):
    """open_ports {host: sorted [ports]}; baseline {host: [ports]} (None = first scan); accepted {'host:port', ...} -> [(fp, sev, title, summary, excerpt)]."""
    out = []
    for host, ports in sorted(open_ports.items()):
        for p in ports:
            key = f"{host}:{p}"
            if key in accepted:
                continue
            risk = RISKY.get(p)
            new = baseline is not None and p not in baseline.get(host, [])
            if risk:
                out.append((f"exposure:{key}", risk[0], f"{host}:{p} is open: {risk[1]}", f"Port {p} on {host} accepts connections from this network. {risk[1]}. If it is intended, mark this finding done to accept it.", f"{key} open" + (" (new since the baseline)" if new else "")))
            elif new:
                out.append((f"exposure:{key}", "medium", f"New open port {key}", f"{host} did not have port {p} open in the baseline scan. A new listening service can be a new app or something unwanted. Mark done to accept it.", f"{key} open, not in the baseline"))
    return out


def private(ip):
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


async def scan_host(ip, ports, sem, timeout=0.7):
    async def probe(p):
        async with sem:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(ip, p), timeout)
                w.close()
                try: await w.wait_closed()
                except Exception: pass
                return p
            except Exception:
                return None
    return sorted(x for x in await asyncio.gather(*(probe(p) for p in ports)) if x)


class ExposureAuditor(Agent):
    name = "exposure_auditor"
    group = "security"
    title = "Exposure Auditor"
    autonomy = "R"
    interval_s = 7 * 86400
    auto_resolve = True

    async def hosts(self):
        hosts = set(EXTRA_HOSTS)
        pve = PVE()
        try:
            for r in await pve.get("cluster/resources", {"type": "vm"}):
                if r.get("type") == "lxc" and r.get("status") == "running":
                    m = re.search(r"ip=(\d+\.\d+\.\d+\.\d+)", (await pve.get(f"nodes/{pve.node}/lxc/{r['vmid']}/config")).get("net0", ""))
                    if m:
                        hosts.add(m.group(1))
        except SourceError:
            pass                                    # fall back to the fixed list
        return sorted(h for h in hosts if private(h))

    async def run(self, ctx):
        last = ctx.kv_get("last_scan", 0)
        targeted = [h for h in (ctx.args.get("hosts") or []) if private(h)][:5]      # the Commander can ask for one machine to be looked at now
        if not targeted and ctx.trigger != "manual" and time.time() - last < 6 * 86400:
            ctx.skip(f"last scan {int((time.time() - last) / 86400)} day(s) ago; next one is due weekly")          # not looking is not the same as 'all clear'
            return
        hosts = targeted or await self.hosts()
        if targeted:
            ctx.partial = True
        ctx.working("scanning", f"{len(hosts)} hosts x {len(PORTS)} ports")
        sem = asyncio.Semaphore(60)
        res = await asyncio.gather(*(scan_host(h, PORTS, sem) for h in hosts))
        open_ports = {h: p for h, p in zip(hosts, res) if p}
        base, accepted = ctx.kv_get("baseline"), set(ctx.kv_get("accepted", []))
        for f in ctx.runner.store.list_findings(self.name, "done", limit=300):          # done = accepted by the owner
            if f["fingerprint"].startswith("exposure:") and f["fingerprint"][9:] not in accepted:
                accepted.add(f["fingerprint"][9:]); ctx.runner.store.set_status(f["id"], "resolved"); ctx.runner.store.audit("agent:exposure_auditor", "accepted", f["fingerprint"][9:], f["title"][:100])
        ctx.kv_set("accepted", sorted(accepted))
        found = classify(open_ports, base["open"] if base else None, accepted)
        ctx.checked(len(hosts))
        for fp, sev, title, summ, ex in found:
            ctx.finding(fp, sev, title, summ, [{"type": "probe", "ref": "tcp connect scan", "excerpt": ex}], confidence=0.9)
            if "baseline" in ex and base is not None:
                ctx.signal(f"host:{fp.split(':')[1]}", "new-port", 0.5, f"{fp.split(':', 1)[1]} is open and was not in the baseline scan")
        if targeted:
            ctx.note(f"looked at {len(hosts)} machine(s) on request: {sum(len(p) for p in open_ports.values())} open ports, {len(found)} to look at")
            return
        if base is None:
            ctx.kv_set("baseline", {"taken": time.time(), "open": open_ports})
            ctx.finding("exposure:baseline", "info", f"Exposure baseline recorded: {sum(len(p) for p in open_ports.values())} open ports on {len(open_ports)} hosts", "Future scans will report ports that are new. Risky services are reported every time until accepted.",
                        [{"type": "probe", "ref": "tcp connect scan", "excerpt": "; ".join(f"{h}: {','.join(map(str, p))}" for h, p in list(open_ports.items())[:8])}], confidence=1.0)
        ctx.kv_set("last_scan", time.time())
        ctx.note(f"{len(hosts)} hosts scanned, {sum(len(p) for p in open_ports.values())} open ports, {len(found)} to look at")

    SKILLS = {"scan": {"about": "scan one or a few internal machines for open ports now and compare with the baseline", "args": {"hosts": "list of internal addresses"}, "active": True}}

    async def skill_scan(self, ctx, args):
        hosts = [h for h in (args.get("hosts") or []) if private(h)][:5]
        if not hosts:
            return {"summary": "No internal address to scan.", "facts": [], "followups": [], "recommend": [], "data": {}}
        sem = asyncio.Semaphore(60)
        res = await asyncio.gather(*(scan_host(h, PORTS, sem) for h in hosts))
        open_ports = {h: p for h, p in zip(hosts, res) if p}
        base = ctx.kv_get("baseline")
        found = classify(open_ports, base["open"] if base else None, set(ctx.kv_get("accepted", [])))
        facts = [f"{h}: " + (", ".join(map(str, open_ports[h])) if h in open_ports else "no scanned port is open") for h in hosts]
        facts += [f"{sev}: {title}" for fp, sev, title, summ, ex in found]
        rec = []
        if found:
            rec.append({"title": "Look at the services this machine exposes", "risk": "low", "steps": [f"Check on the machine itself which program listens on the flagged ports (ss -ltnp)", "If a service does not need to be reachable from the network, bind it to localhost or add a firewall rule", "If it is intended, mark the exposure finding 'done' so it is accepted"],
                        "rollback": ["Undo the bind or firewall change (restore the previous config file or rule)"]})
        return {"summary": facts[0], "facts": facts, "followups": [], "recommend": rec, "data": {"open": open_ports}}
