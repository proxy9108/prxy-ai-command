"""Which host / guest / VLAN / storage does a finding or signal talk about? One place decides, so the Correlator (who is involved), the
maintenance windows (what to stay quiet about) and the Office agree. Pure functions plus a small alias map (guest id <-> IP address).
Outside addresses are never entities: correlation is about OUR machines, and outside addresses live only as long as their evidence (24 h)."""
import os, ipaddress, re, time

IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
GUEST = re.compile(r"\b(lxc|qemu)/(\d+)\b")
STORAGE = re.compile(r"(?:storage[:/](?:[\w.-]+/)*|/mnt/pve/)([\w.-]+)")
VLANS = {"20": "Home", "30": "Guest", "40": "Internal", "50": "IoT"}
_cache = {"at": 0.0, "map": {}}


def private_ip(s):
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback


def vlan_of(ip):
    p = ip.split(".")
    return p[2] if len(p) == 4 and p[:2] == os.getenv("LAN_PREFIX", "10.20").split(".") else None


def alias_map(store):
    """{'lxc/131': '10.20.40.131', ...}. Read from Config Drift's baseline (real net0 settings); the convention 'LXC n = 10.20.40.n' fills the gaps."""
    if time.time() - _cache["at"] < 300 and _cache["map"]:
        return _cache["map"]
    m = {}
    try:
        base = store.kv_get("config_drift:baseline") or {}
        for gid, cfg in (base.get("items") or {}).items():
            for k in ("net0", "net1"):
                ip = re.search(r"ip=((?:\d{1,3}\.){3}\d{1,3})", str(cfg.get(k, "")))
                if ip and gid not in m:
                    m[gid] = ip.group(1)
    except Exception:
        pass
    _cache.update(at=time.time(), map=m)
    return m


def guest_to_ip(gid, aliases):
    if gid in aliases:
        return aliases[gid]
    kind, _, n = gid.partition("/")
    if kind == "lxc" and n.isdigit() and 100 <= int(n) <= 199:
        return f"10.20.40.{n}"
    return None


def ip_to_guest(ip, aliases):
    for gid, a in aliases.items():
        if a == ip:
            return gid
    return None


def entities_of(*texts, aliases=None):
    """-> {'hosts': {ip}, 'guests': {gid}, 'vlans': {'50'}, 'storages': {name}, 'keys': {every matching key, for scope tests}}"""
    aliases = aliases or {}
    blob = " ".join(t for t in texts if t)
    hosts, guests, storages = set(), set(), set()
    for m in IP.finditer(blob):
        if private_ip(m.group(0)):
            hosts.add(m.group(0))
    for m in GUEST.finditer(blob):
        gid = f"{m.group(1)}/{m.group(2)}"
        guests.add(gid)
        ip = guest_to_ip(gid, aliases)
        if ip:
            hosts.add(ip)
    for ip in list(hosts):
        g = ip_to_guest(ip, aliases)
        if g:
            guests.add(g)
    for m in STORAGE.finditer(blob):
        storages.add(m.group(1))
    vlans = {v for v in (vlan_of(h) for h in hosts) if v}
    keys = {f"host:{h}" for h in hosts} | {f"guest:{g}" for g in guests} | {f"vlan:{v}" for v in vlans} | {f"storage:{s}" for s in storages}
    return {"hosts": hosts, "guests": guests, "vlans": vlans, "storages": storages, "keys": keys}


def norm_scope(item):
    """Owner-typed scope ('10.20.40.131', 'lxc/131', '131', 'vlan 50', 'storage:backups', '*') -> list of keys; any one matching is enough."""
    s = str(item).strip().lower()
    if s in ("*", "all", "everything"):
        return ["*"]
    if IP.fullmatch(s):
        return [f"host:{s}"]
    m = re.fullmatch(r"(?:ct|lxc|vm|qemu)?[ /:-]?(\d{3})", s)
    if m:
        n = m.group(1)
        return [f"guest:lxc/{n}", f"guest:qemu/{n}", f"host:10.20.40.{n}"]
    m = re.fullmatch(r"vlan[ :]?(\d+)", s)
    if m:
        return [f"vlan:{m.group(1)}"]
    m = re.fullmatch(r"(?:storage[:/ ])?([\w.-]+)", s)
    return [f"storage:{m.group(1)}"] if m else []


def canonical(ent):
    """Entities the Correlator joins on: our own machines, as host:<ip> (a guest id is turned into its address when that is known)."""
    return sorted(f"host:{h}" for h in ent["hosts"])
