"""Patch Scout: which parts of the Proxmox stack have newer versions, and does the host need a reboot?
Reads the Proxmox API (installed versions, running kernel, configured repositories) and compares the installed versions with the UPSTREAM package
index of the repository the host actually uses, so it does not depend on when the host last ran 'apt update'.
It cannot see Debian packages outside the Proxmox stack, guest OS packages or Docker image versions (a read-only token cannot list them, and
there is no SSH channel by design); one standing, rejectable finding says so. It only proposes: the owner runs the steps."""
import gzip, re
import aiohttp
from ..runner import Agent
from ..sources import PVE, SourceError

CRITICAL = re.compile(r"^(proxmox-kernel|pve-kernel|proxmox-ve$|pve-manager|qemu-server|pve-qemu-kvm|pve-container|lxc-pve|pve-firmware|libpve-|pve-cluster|pve-ha|proxmox-backup-client|openssh|libssl|openssl)")


def _order(c):
    return -1 if c == "~" else (ord(c) if c.isalpha() else ord(c) + 256) if c else 0


def _cmp_part(a, b):
    while a or b:
        na = re.match(r"\D*", a).group(); nb = re.match(r"\D*", b).group()
        for x, y in zip(na.ljust(max(len(na), len(nb))), nb.ljust(max(len(na), len(nb)))):
            ox, oy = _order(x.strip() or ""), _order(y.strip() or "")
            if ox != oy:
                return -1 if ox < oy else 1
        a, b = a[len(na):], b[len(nb):]
        da = re.match(r"\d*", a).group(); db = re.match(r"\d*", b).group()
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
        a, b = a[len(da):], b[len(db):]
    return 0


def vercmp(a, b):
    """Debian version comparison: -1 if a < b, 0 equal, 1 if a > b."""
    def split(v):
        e, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
        up, _, rev = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return int(e or 0), up, rev
    ea, ua, ra = split(a); eb, ub, rb = split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


def parse_packages(text):
    """Packages index -> {name: newest version}."""
    best = {}
    for block in text.split("\n\n"):
        m = re.search(r"^Package: (\S+)", block, re.M); v = re.search(r"^Version: (\S+)", block, re.M)
        if m and v and (m.group(1) not in best or vercmp(v.group(1), best[m.group(1)]) > 0):
            best[m.group(1)] = v.group(1)
    return best


def pending(installed, upstream):
    """installed {name: version}, upstream {name: version} -> [(name, installed, upstream)] where upstream is newer."""
    return sorted((n, v, upstream[n]) for n, v in installed.items() if n in upstream and vercmp(upstream[n], v) > 0)


def newer_kernel_series(installed, upstream):
    """Kernel meta-packages named proxmox-kernel-X.Y: -> (installed_series, upstream_series) when upstream has a newer series, else None.
    The API's package list only covers part of the stack, so a whole new kernel series would otherwise go unnoticed."""
    ser = lambda names: max(((int(m.group(1)), int(m.group(2))) for n in names for m in [re.match(r"^proxmox-kernel-(\d+)\.(\d+)$", n)] if m), default=None)
    i, u = ser(installed), ser(upstream)
    return (i, u) if i and u and u > i else None


def plan(items, kernel=False):
    names = " ".join(n for n, _, _ in items[:12])
    steps = ["Check the list first:  apt update && apt list --upgradable", f"Upgrade:  apt full-upgrade   (includes: {names}{' ...' if len(items) > 12 else ''})",
             "Run the backup job first or confirm last night's finished OK (Backup Warden shows it)"]
    if kernel:
        steps.append("Reboot the host at a quiet time: every guest stops for a few minutes and the Discord bot goes offline meanwhile")
    return {"kind": "manual", "risk": "medium" if kernel else "low", "steps": steps,
            "rollback": ["Kernel: pick the previous kernel in the boot menu (Proxmox keeps older kernels installed)",
                         "Single package: apt install <package>=<previous version> (versions are in this finding's evidence)",
                         "Guests: restore from last night's vzdump backup if one misbehaves"]}


class PatchScout(Agent):
    name = "patch_scout"
    group = "operations"
    title = "Patch Scout"
    autonomy = "P"
    interval_s = 6 * 3600
    auto_resolve = True

    async def upstream_index(self, repos):
        for f in repos.get("files", []):
            for r in f.get("repositories", []):
                if not r.get("Enabled"):
                    continue
                uris, suites, comps = r.get("URIs", []), r.get("Suites", []), r.get("Components", [])
                for u in uris:
                    if "download.proxmox.com" in u and "/pve" in u:
                        url = f"{u.rstrip('/')}/dists/{suites[0]}/{comps[0]}/binary-amd64/Packages.gz"
                        try:
                            async with aiohttp.ClientSession() as s:
                                async with s.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                                    if resp.status != 200:
                                        raise SourceError(f"{url} answered HTTP {resp.status}")
                                    return parse_packages(gzip.decompress(await resp.read()).decode("utf-8", "replace")), f"{suites[0]}/{comps[0]}"
                        except SourceError:
                            raise
                        except Exception as e:
                            raise SourceError(f"{url}: {type(e).__name__}: {e}")
        raise SourceError("no enabled Proxmox repository found in the host's apt configuration")

    async def run(self, ctx):
        pve = PVE()
        try:
            ctx.working("reading", "installed Proxmox packages and kernel")
            vers = await pve.get(f"nodes/{pve.node}/apt/versions")
            status = await pve.get(f"nodes/{pve.node}/status")
            repos = await pve.get(f"nodes/{pve.node}/apt/repositories")
            ctx.working("reading", "upstream package index")
            upstream, repo = await self.upstream_index(repos)
        except SourceError as e:
            ctx.finding("source:patch", "medium", "Patch Scout could not check for updates", f"{e}", [{"type": "error", "ref": "patch scout", "excerpt": str(e)[:300]}], confidence=1.0)
            ctx.no_data("could not check: " + str(e)[:120])
            return
        installed = {v["Package"]: v["OldVersion"] for v in vers if v.get("OldVersion")}
        items = pending(installed, upstream)
        kern = [i for i in items if re.match(r"(proxmox|pve)-kernel", i[0])]
        core = [i for i in items if CRITICAL.match(i[0]) and i not in kern]
        rest = [i for i in items if i not in kern and i not in core]
        fmt = lambda xs: "; ".join(f"{n} {a} -> {b}" for n, a, b in xs[:15])
        ev = lambda xs: [{"type": "package", "ref": f"{repo} index", "excerpt": f"{n}: installed {a}, available {b}"} for n, a, b in xs[:8]]
        ctx.working("scanning", f"{len(items)} newer package(s)")
        if kern:
            ctx.finding("patch:kernel", "medium", f"{len(kern)} Proxmox kernel package(s) have a newer version", f"Kernel updates carry security fixes and need a host reboot to take effect. {fmt(kern)}", ev(kern), proposed_action=plan(items, True), confidence=0.9)
        if core:
            ctx.finding("patch:core", "medium", f"{len(core)} core Proxmox package(s) have updates", f"These run your guests and the web interface. {fmt(core)}", ev(core), proposed_action=plan(items, False), confidence=0.9)
        if rest:
            ctx.finding("patch:other", "low", f"{len(rest)} other Proxmox package(s) have updates", fmt(rest), ev(rest), proposed_action=plan(items, False), confidence=0.9)
        ns = newer_kernel_series(installed, upstream)
        if ns:
            newkern = f"proxmox-kernel-{ns[1][0]}.{ns[1][1]}"
            ctx.finding("patch:kernel-series", "medium", f"A newer Proxmox kernel series {ns[1][0]}.{ns[1][1]} is available (installed {ns[0][0]}.{ns[0][1]})",
                        f"The repository now offers {newkern} {upstream.get(newkern, '')}. Newer kernel series bring security fixes and hardware support but need a reboot, and a new series deserves a look at the release notes first.",
                        [{"type": "package", "ref": f"{repo} index", "excerpt": f"{newkern}: {upstream.get(newkern)}; installed newest series {ns[0][0]}.{ns[0][1]}"}],
                        proposed_action={"kind": "manual", "risk": "medium", "steps": ["Read the Proxmox release notes for the new kernel series", "Confirm last night's backup finished OK", f"apt install {newkern} (or apt full-upgrade)", "Reboot at a quiet time: all guests and the Discord bot stop for a few minutes"],
                                         "rollback": ["Pick the previous kernel in the boot menu; Proxmox keeps older kernels installed", "proxmox-boot-tool kernel pin <previous version> makes it permanent"]}, confidence=0.8)
        running = re.search(r"(\d+\.\d+\.\d+-\d+)-pve", status.get("kversion", ""))
        newest = sorted({re.sub(r"^proxmox-kernel-", "", n).split("-pve")[0] for n, _ in installed.items() if re.match(r"proxmox-kernel-\d+\.\d+\.\d+-\d+-pve", n)}, key=lambda v: [int(x) for x in re.split(r"[.-]", v)])
        if running and newest and [int(x) for x in re.split(r"[.-]", newest[-1])] > [int(x) for x in re.split(r"[.-]", running.group(1))]:
            ctx.finding("patch:reboot", "medium", f"The host is running kernel {running.group(1)} but {newest[-1]} is installed",
                        "A newer kernel is already installed; a reboot activates it. Until then the host runs without those fixes.", [{"type": "metric", "ref": "nodes/status kversion", "excerpt": f"running {running.group(1)}, newest installed {newest[-1]}"}],
                        proposed_action={"kind": "manual", "risk": "medium", "steps": ["Confirm last night's backup finished OK", "Reboot the host at a quiet time (all guests stop for a few minutes)"], "rollback": ["Boot the previous kernel from the boot menu"]}, confidence=0.9)
        ctx.finding("patch:visibility-gap", "low", "Patch Scout cannot see guest or Debian package updates",
                    "It checks the Proxmox stack only. It cannot list pending Debian security updates on the host outside that stack, package updates inside the 21+ containers, or outdated Docker images, because a read-only Proxmox token cannot see them and apps are not allowed an SSH channel into guests. To close the gap you would approve a small read-only 'pending updates' feed (for example a textfile exporter on each guest). Reject this finding to silence it.",
                    [{"type": "config", "ref": "design", "excerpt": "no SSH action channel (owner decision); PVEAuditor token has no Sys.Modify"}], confidence=0.8)
        ctx.note(f"{len(installed)} Proxmox packages checked against {repo}: {len(items)} newer; kernel {running.group(1) if running else '?'}")
