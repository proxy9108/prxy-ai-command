"""Config Drift: has the real setup changed since the last accepted baseline? It snapshots guest configs, storages and the backup job from the
Proxmox API (read-only) and diffs against the stored baseline. First run records the baseline. To accept a change, mark its finding 'done'
(the next run adopts the new state); to keep the change as a problem, leave it open. Infrastructure settings only, nothing about people."""
from ..redact import scrub
import time
from ..runner import Agent
from ..sources import PVE, SourceError
from ..entities import entities_of, alias_map

VOLATILE = {"digest", "lock", "parent", "snaptime", "description", "lxc", "template"}
SECURITY_KEYS = {"net0", "net1", "unprivileged", "features", "mp0", "mp1", "mp2", "firewall", "lxc"}


LISTY = {"content", "exclude", "vmid", "features"}          # comma lists whose order Proxmox does not keep stable


def normalize(cfg):
    return {k: (",".join(sorted(str(v).split(","))) if k in LISTY else str(v)) for k, v in cfg.items() if k not in VOLATILE}


def canon(items):
    return {gid: normalize(c) for gid, c in items.items()}


def diff(base, cur):
    """base/cur: {id: {key: value}} -> [(id, kind, detail, severity)] kind in added|removed|changed."""
    out = []
    for gid in sorted(set(base) | set(cur)):
        if gid not in base:
            out.append((gid, "added", f"new: {cur[gid].get('hostname', cur[gid].get('name', gid))}", "low"))
        elif gid not in cur:
            out.append((gid, "removed", f"gone: {base[gid].get('hostname', base[gid].get('name', gid))}", "medium"))
        else:
            for k in sorted(set(base[gid]) | set(cur[gid])):
                a, b = base[gid].get(k), cur[gid].get(k)
                if a != b:
                    sev = "medium" if (k in SECURITY_KEYS or k == "onboot") else "low"
                    out.append((gid, "changed", f"{k}: {a if a is not None else '(unset)'} -> {b if b is not None else '(unset)'}", sev))
    return out


class ConfigDrift(Agent):
    name = "config_drift"
    group = "operations"
    title = "Config Drift"
    autonomy = "R"
    interval_s = 3 * 3600
    auto_resolve = True

    async def snapshot(self, pve):
        snap = {}
        res = await pve.get("cluster/resources", {"type": "vm"})
        for r in res:
            kind = r.get("type")
            if kind not in ("lxc", "qemu"):
                continue
            c = await pve.get(f"nodes/{pve.node}/{kind}/{r['vmid']}/config")
            snap[f"{kind}/{r['vmid']}"] = {**normalize(c), **({"name": c.get("name")} if kind == "qemu" else {})}
        for s in await pve.get("storage"):
            snap[f"storage/{s['storage']}"] = {k: str(v) for k, v in s.items() if k in ("type", "path", "content", "vgname", "thinpool", "shared", "disable")}
        for j in await pve.get("cluster/backup"):
            snap[f"backupjob/{j.get('id', '?')[:8]}"] = {k: str(v) for k, v in j.items() if k in ("schedule", "storage", "exclude", "all", "mode", "compress", "enabled", "prune-backups", "vmid")}
        return snap

    async def run(self, ctx):
        pve = PVE()
        try:
            ctx.working("reading", "guest, storage and backup-job configuration")
            cur = await self.snapshot(pve)
        except SourceError as e:
            ctx.finding("source:pve", "medium", "Config Drift cannot read Proxmox", str(e), [{"type": "error", "ref": "proxmox api", "excerpt": str(e)[:300]}], confidence=1.0)
            ctx.no_data("Proxmox API unreachable")
            return
        base = ctx.kv_get("baseline")
        if base is None:
            ctx.kv_set("baseline", {"taken": time.time(), "items": cur})
            ctx.finding("drift:baseline-recorded", "info", f"Baseline recorded: {len(cur)} configuration items", "Config Drift will tell you when any of these change. If a change is intended, mark its finding 'done' and it becomes the new baseline.",
                        [{"type": "config", "ref": "proxmox api", "excerpt": f"{len(cur)} items snapshotted"}], confidence=1.0)
            ctx.note(f"baseline recorded ({len(cur)} items)")
            return
        items = canon(base["items"])
        cur = canon(cur)
        ctx.working("scanning", "comparing with the baseline")
        # a drift finding the owner marked 'done' means "accepted": adopt the current state of that item, then retire the finding
        changed = False
        for f in ctx.runner.store.list_findings(self.name, "done", limit=200):
            if not f["fingerprint"].startswith("drift:"):
                continue
            gid = f["fingerprint"].split(":", 1)[1].split("|")[0]
            if gid in cur: items[gid] = cur[gid]
            else: items.pop(gid, None)
            changed = True
            ctx.runner.store.set_status(f["id"], "resolved")
            ctx.runner.store.audit(f"agent:{self.name}", "baseline.accepted", gid, f["title"][:100])
        found = diff(items, cur)
        ctx.checked(len(cur))
        for gid, kind, detail, sev in found:
            title = {"added": f"New in Proxmox: {gid}", "removed": f"Missing from Proxmox: {gid}", "changed": f"{gid} configuration changed"}[kind]
            ctx.finding(f"drift:{gid}|{kind}|{detail[:40]}", sev, title, f"{detail}. Compared with the baseline recorded {time.strftime('%Y-%m-%d', time.localtime(base['taken']))}. If this was intended, mark the finding 'done' to accept it as the new baseline.",
                        [{"type": "config", "ref": gid, "excerpt": detail}], confidence=0.9)
            for host in entities_of(gid, aliases=alias_map(ctx.runner.store))["hosts"]:       # a recorded change on a machine explains other agents' reactions to it for a while
                ctx.signal(f"host:{host}", "change", 0.3, f"{gid}: {detail[:100]}")
        if changed:
            ctx.kv_set("baseline", {"taken": base["taken"], "items": items})
        ctx.note(f"{len(cur)} items compared with the baseline: {len(found)} difference(s)")

    SKILLS = {"describe": {"about": "show the configuration of a guest, a storage or the backup job (disks, mounts, retention, schedule)", "args": {"item": "guest id/name (lxc/131, ollama), a storage name, or 'backup'"}},
              "changes": {"about": "what changed in a guest's configuration compared with the baseline: each setting before -> after, when it was first seen, and whether the owner accepted it",
                          "args": {"item": "guest id/name (qemu/102, lxc/131, ollama)"}}}

    async def skill_changes(self, ctx, args):
        """Before -> after for every recorded change of one item (from Config Drift's own findings, last 60 days). MAC addresses are masked."""
        text = str(args.get("item", "")).strip().lower()
        store = ctx.runner.store
        base = (store.kv_get("config_drift:baseline") or {}).get("items", {})
        gids = [g for g, c in base.items() if text and (text == g.lower() or text in g.lower() or text == str(c.get("hostname") or c.get("name") or "").lower())]
        rows = store.q("SELECT * FROM findings WHERE agent='config_drift' AND fingerprint LIKE 'drift:%' AND last_seen > ? ORDER BY ts", (time.time() - 60 * 86400,))
        fs = [store._f(r) for r in rows]
        fs = [f for f in fs if f["fingerprint"].split(":", 1)[1].split("|")[0] in gids] if gids else [f for f in fs if text and text in f["fingerprint"].lower()]
        ctx.checked(len(fs))
        state = {"new": "not accepted yet", "seen": "not accepted yet", "done": "accepted by the owner", "resolved": "accepted / cleared", "rejected": "dismissed", "expired": "expired"}
        facts = []
        for f in fs:
            detail = scrub(f["summary"].split(". Compared with")[0])
            facts.append(f"{f['fingerprint'].split(':', 1)[1].split('|')[0]}: {detail} (first seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(f['ts']))}, {state.get(f['status'], f['status'])})")
        if not facts:
            return {"summary": f"No recorded configuration changes for '{text}' in the last 60 days.", "facts": ["nothing recorded"], "followups": [], "recommend": [], "data": {}}
        when = time.strftime("%Y-%m-%d", time.localtime(min(f["ts"] for f in fs)))
        return {"summary": f"{len(facts)} recorded change(s) for {', '.join(gids) or text}, first seen {when}.", "facts": facts[:20], "followups": [], "recommend": [], "data": {"count": len(facts)}, "key": facts[:6]}

    async def skill_describe(self, ctx, args):
        text = str(args.get("item", "")).strip().lower()
        base = ctx.runner.store.kv_get("config_drift:baseline") or {}
        items, facts, follow, rec = base.get("items", {}), [], [], []
        show = {"hostname", "name", "rootfs", "mp0", "mp1", "mp2", "memory", "cores", "onboot", "net0", "unprivileged", "scsi0", "virtio0", "sata0", "boot"}
        hits = [(g, c) for g, c in items.items() if text and (text in g.lower() or any(text in str(v).lower() for k, v in c.items() if k in ("hostname", "name")))]
        def tidy(k, v):
            v = str(v)
            if k.startswith("net"):                                   # keep what matters (address, bridge, vlan tag), not the hardware address or gateway
                return ", ".join(p for p in v.split(",") if p.split("=")[0] in ("ip", "bridge", "tag", "firewall"))
            return v[:140]
        for g, c in hits[:4]:
            facts.append(f"{g}: " + "; ".join(f"{k}={tidy(k, v)}" for k, v in sorted(c.items()) if k in show))
        if "backup" in text or any(g.startswith("storage/") for g, _ in hits):
            pve = PVE()
            for s in await pve.get("storage"):
                if "backup" in str(s.get("content", "")) and (not text or text == "backup" or text in s["storage"].lower()):
                    facts.append(f"storage {s['storage']}: type {s.get('type')}, content {s.get('content')}, path {s.get('path', '-')}, retention (prune-backups): {s.get('prune-backups', 'not set on the storage')}")
            for j in await pve.get("cluster/backup"):
                facts.append(f"backup job {str(j.get('id', '?'))[:10]}: schedule {j.get('schedule', j.get('starttime', '?'))}, storage {j.get('storage')}, mode {j.get('mode')}, compress {j.get('compress')}, retention {j.get('prune-backups', 'not set on the job (the storage setting applies)')}, "
                             + ("all guests" if j.get("all") else f"guests {j.get('vmid')}") + (f", excluding {j.get('exclude')}" if j.get("exclude") else ""))
        if not facts:
            return {"summary": f"Nothing in the configuration matches '{text}'.", "facts": [], "followups": [], "recommend": [], "data": {}}
        has_backup = any(f.startswith(("storage ", "backup job")) for f in facts)
        retention_set = any(f.startswith(("storage ", "backup job")) and "not set" not in f.split("retention", 1)[-1][:40] for f in facts)
        if has_backup and (not retention_set or any("keep-all=1" in f for f in facts)):
            rec.append({"title": "Set an explicit retention for the backups", "risk": "medium", "steps": [
                "In Proxmox open Datacenter > Backup, edit the job and set Retention (for example keep-daily 7, keep-weekly 4) or set it on the storage under Datacenter > Storage > backups > Backup Retention",
                "The next prune removes older backups beyond the rule: check first that you do not need them"],
                "rollback": ["Set Retention back to what it was (or 'Keep all backups') before the next prune runs", "Pruned backups cannot be restored: copy any you need elsewhere first"]})
        age = (time.time() - base.get("taken", time.time())) / 86400
        facts.append(f"(guest settings are from the baseline taken {age:.0f} days ago; storage and backup-job settings were read just now)")
        key = [f for f in facts if f.startswith(("storage ", "backup job"))][:2] or facts[:1]
        return {"summary": facts[0][:200], "facts": facts, "key": key, "followups": follow, "recommend": rec, "data": {}}
