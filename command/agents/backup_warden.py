"""Backup Warden: did last night's backup run, did it cover every guest, did any archive shrink suspiciously?
Reads the Proxmox API read-only (PVEAuditor token). A read-only token cannot list the backup files themselves (Proxmox hides them without
backup rights) and cannot test-restore, so this agent reads the vzdump TASK LOGS instead: per guest 'Finished' or 'ERROR', archive sizes
and the warnings Proxmox prints. 'Healthy' here means: the job ran on schedule, every guest finished OK, sizes did not collapse."""
import re, time
from ..runner import Agent
from ..sources import PVE, SourceError

IGNORE = {100, 102}                 # stopped on purpose, excluded from the backup job (HOMELAB_HANDOFF)
UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def hours(s):
    return f"{s / 3600:.0f} h"


def parse_log(lines):
    """-> (vms {vmid: {'ok': bool, 'size': bytes|None, 'err': str}}, warnings [str]) from a vzdump task log (list of text lines)."""
    vms, cur, warns = {}, None, []
    for t in lines:
        m = re.match(r"INFO: Starting Backup of VM (\d+)", t)
        if m:
            cur = int(m.group(1)); vms[cur] = {"ok": False, "size": None, "err": ""}; continue
        m = re.match(r"INFO: archive file size: ([\d.]+)\s*([KMGT]?B)", t)
        if m and cur is not None:
            vms[cur]["size"] = int(float(m.group(1)) * UNITS[m.group(2)]); continue
        m = re.match(r"INFO: Finished Backup of VM (\d+)", t)
        if m:
            vms.setdefault(int(m.group(1)), {"ok": False, "size": None, "err": ""})["ok"] = True; continue
        m = re.match(r"ERROR: Backup of VM (\d+) failed - (.*)", t)
        if m:
            vms.setdefault(int(m.group(1)), {"ok": False, "size": None, "err": ""})["err"] = m.group(2)[:200]; continue
        if "thin pool" in t.lower() and "WARNING" in t:
            warns.append(t.strip()[:200])
    return vms, warns


def merge_manual(job, manual):
    """A manual single-guest backup made AFTER the nightly job replaces that guest's result (a failure redone by hand is fixed).
    It never stands in for the whole job: before 1.4 a manual `vzdump 131` was taken as 'the last run' and every other guest looked skipped.
    job: parsed job run; manual: parsed single-guest runs, any order. Returns a new run dict."""
    out = {**job, "vms": dict(job["vms"]), "redone": []}
    for m in sorted(manual, key=lambda r: r["end"]):
        if m["end"] <= job["end"]:
            continue
        for vmid, r in m["vms"].items():
            if r["ok"] and vmid in out["vms"] and not out["vms"][vmid]["ok"]:
                out["redone"].append(vmid)
            if r["ok"] or vmid not in out["vms"]:
                out["vms"][vmid] = r
    if out["status"] != "OK" and out["vms"] and all(r["ok"] for r in out["vms"].values()) and out["redone"]:
        out["status"] = "OK"                       # every failure of the job has since been backed up by hand
    return out


def evaluate(guests, runs, now, shrink_ratio=0.3):
    """guests: [{'vmid','name','template'}]; runs: parsed vzdump runs newest first [{'upid','end','status','vms'}]. Pure function (tested with synthetic data)."""
    out, ev = [], lambda t, r, x: [{"type": t, "ref": r, "excerpt": x}]
    if not runs or now - runs[0]["end"] > 26 * 3600:
        age = hours(now - runs[0]["end"]) if runs else "ever"
        out.append(dict(fp="backup:no-recent-run", sev="high", title="No completed backup run in the last 26 hours",
                        summary=f"The nightly backup should finish every day. The last completed run was {age} ago.", evidence=ev("task", "vzdump tasks", f"last end: {age} ago")))
        return out
    last, prev = runs[0], (runs[1] if len(runs) > 1 else None)
    if last["status"] != "OK":
        out.append(dict(fp="backup:last-run-problem", sev="high", title=f"The last backup run did not finish cleanly: {last['status']}",
                        summary="Open the task log in Proxmox to see which guest failed.", evidence=ev("task", last["upid"], str(last["status"]))))
    for g in guests:
        vmid = int(g["vmid"])
        if vmid in IGNORE or g.get("template"):
            continue
        r = last["vms"].get(vmid)
        if r is None:
            new = prev is not None and vmid not in prev["vms"]        # absent from two runs in a row = probably a guest created since; absent only now = skipped
            out.append(dict(fp=f"backup:skipped:{vmid}", sev="low" if new else "high",
                            title=f"{g['name']} ({vmid}) has not been in a backup run yet" if new else f"{g['name']} ({vmid}) was not in the last backup run",
                            summary="It was created after the last run and will be included tonight (this clears itself)." if new else "The newest backup job did not touch this guest, so its newest backup is older than a day.",
                            evidence=ev("task", last["upid"], f"no entry for VM {vmid}"), conf=0.6 if new else 0.9))
        elif not r["ok"]:
            kind = g.get("kind") or "lxc"
            out.append(dict(fp=f"backup:failed:{vmid}", sev="high", title=f"Backup of {g['name']} ({vmid}) failed",
                            summary=f"The last run reported: {r['err'] or 'no success message'}.", evidence=ev("task", last["upid"], r["err"] or "no 'Finished Backup' line"),
                            action={"kind": "manual", "risk": "low",
                                    "steps": [f"Fix the cause first if it is known (for 'No space left on device': free space on the backup storage or skip replaceable data)",
                                              f"Back up {kind}/{vmid} now: Datacenter > Backup > Run now, or `vzdump {vmid} --storage backups --mode snapshot`",
                                              "Check that a new archive appeared and the log ends with 'Finished Backup'"],
                                    "rollback": ["Nothing to undo: it only adds an archive; the job's keep-last rule prunes old ones"],
                                    "execute": {"action": "guest_backup_now", "params": {"vmid": vmid, "kind": kind}}}))
        elif prev and r["size"] and prev["vms"].get(vmid, {}).get("size") and r["size"] < shrink_ratio * prev["vms"][vmid]["size"]:
            ps = prev["vms"][vmid]["size"]
            out.append(dict(fp=f"backup:shrunk:{vmid}", sev="medium", title=f"Backup of {g['name']} ({vmid}) shrank to {r['size'] * 100 // ps}% of the previous night's",
                            summary="A sudden drop can mean data was lost or the backup is incomplete.", evidence=ev("task", last["upid"], f"{r['size']} bytes vs {ps} bytes the night before"), conf=0.6))
    if last.get("warnings"):
        out.append(dict(fp="ops:thin-pool-protection", sev="medium", title="Proxmox warns the thin pool is not protected against filling up",
                        summary="The backup log says thin-pool auto-extension is off and the total size of all guest disks exceeds the pool. If the pool fills, every guest can stall at once. Keep it well below full and avoid adding more over-committed disk space.",
                        evidence=ev("log", last["upid"], " | ".join(sorted(set(last["warnings"]))[:2])), conf=0.8))
    return out


class BackupWarden(Agent):
    name = "backup_warden"
    group = "operations"
    title = "Backup Warden"
    autonomy = "R"
    interval_s = 6 * 3600
    auto_resolve = True
    PARAMS = {"shrink_ratio": dict(default=0.3, min=0.05, max=0.9, label="flag a backup that shrinks below this share of the night before", quieter="down", step=0.05, rules=["backup:shrunk"])}

    async def run(self, ctx):
        pve = PVE()
        try:
            ctx.working("reading", "Proxmox backup tasks")
            res = await pve.get("cluster/resources", {"type": "vm"})
            tasks = [t for t in await pve.get(f"nodes/{pve.node}/tasks", {"typefilter": "vzdump", "limit": 15}) if t.get("endtime")]
            tasks.sort(key=lambda t: -t["endtime"])
            jobs = [t for t in tasks if not t.get("id")][:3]                 # the nightly job (all guests) has no guest id; `vzdump 131` by hand has one
            manual = [t for t in tasks if t.get("id") and jobs and t["endtime"] > jobs[0]["endtime"]][:5]
            runs, extra = [], []
            for t in jobs + manual:
                ctx.working("reading", "task log " + time.strftime("%m-%d", time.localtime(t["starttime"])))
                log = await pve.get(f"nodes/{pve.node}/tasks/{t['upid']}/log", {"limit": 5000})
                vms, warns = parse_log([x["t"] for x in log])
                (runs if t in jobs else extra).append({"upid": t["upid"], "end": t["endtime"], "status": t.get("status"), "vms": vms, "warnings": warns})
            if runs and extra:
                runs[0] = merge_manual(runs[0], extra)
            status = await pve.get(f"nodes/{pve.node}/storage/backups/status")
        except SourceError as e:
            ctx.finding("source:pve", "high", "Backup Warden cannot read Proxmox", f"Backups are not being checked right now: {e}", [{"type": "error", "ref": "proxmox api", "excerpt": str(e)}], confidence=1.0)
            ctx.no_data("Proxmox API unreachable")
            return
        guests = [{"vmid": r["vmid"], "name": r.get("name", r["vmid"]), "template": r.get("template"), "kind": r.get("type") if r.get("type") in ("lxc", "qemu") else None} for r in res]
        ctx.working("scanning", f"{len(guests)} guests across {len(runs)} runs")
        ctx.checked(len(guests))
        found = evaluate(guests, runs, time.time(), ctx.param("shrink_ratio"))
        for f in found:
            ctx.finding(f["fp"], f["sev"], f["title"], f["summary"], f["evidence"], proposed_action=f.get("action"), confidence=f.get("conf", 0.9))
        ctx.finding("backup:no-offdisk-copy", "low", "Backups are only on this server",
                    f"The backup storage is a {status.get('type', '?')} on the Proxmox host ({status.get('used', 0) / 2**30:.0f} of {status.get('total', 0) / 2**30:.0f} GiB used). A disk or host failure would lose the guests AND their backups. An off-disk or off-site copy is still missing (reject this finding to silence it).",
                    [{"type": "config", "ref": "storage/backups", "excerpt": f"type={status.get('type')} used={status.get('used')} total={status.get('total')}"}], confidence=0.7)
        last = runs[0] if runs else None
        ctx.note(f"{len(guests)} guests; last run {hours(time.time() - last['end']) + ' ago, ' + str(last['status']) if last else 'none found'}; {len(found)} problem(s)")

    SKILLS = {"report": {"about": "read the last backup runs: size per guest, what grew, and what the backup storage holds", "args": {"guest": "optional guest name or id to focus on"}}}

    async def skill_report(self, ctx, args):
        pve, focus = PVE(), str(args.get("guest", "")).strip().lower()
        res = await pve.get("cluster/resources", {"type": "vm"})
        names = {int(r["vmid"]): r.get("name", str(r["vmid"])) for r in res}
        tasks = [t for t in await pve.get(f"nodes/{pve.node}/tasks", {"typefilter": "vzdump", "limit": 10}) if t.get("endtime")]
        tasks.sort(key=lambda t: -t["endtime"])
        runs = []
        for t in tasks[:6]:
            log = await pve.get(f"nodes/{pve.node}/tasks/{t['upid']}/log", {"limit": 5000})
            vms, _ = parse_log([x["t"] for x in log])
            runs.append({"end": t["endtime"], "status": t.get("status"), "vms": vms})
        st = await pve.get(f"nodes/{pve.node}/storage/backups/status")
        if not runs:
            return {"summary": "No backup runs found.", "facts": [], "followups": [], "recommend": [], "data": {}}
        gb = lambda n: f"{n / 2**30:.1f} GB"
        tot = [sum((v["size"] or 0) for v in r["vms"].values()) for r in runs]
        now = time.time()
        facts = [f"Backup storage: {st.get('type', '?')}, {gb(st.get('used', 0))} used of {gb(st.get('total', 0))} ({st.get('used', 0) * 100 // max(st.get('total', 1), 1)}%)."]
        facts.append(f"{len(runs)} recent runs found, newest ended {(now - runs[0]['end']) / 3600:.0f} h ago, oldest {(now - runs[-1]['end']) / 86400:.0f} days ago. Archived per run, newest first: " + ", ".join(gb(t) for t in tot) + ".")
        newest = {v: s for v, s in runs[0]["vms"].items() if s["size"]}
        big = sorted(newest.items(), key=lambda kv: -kv[1]["size"])
        sel = [kv for kv in big if not focus or focus in f"{names.get(kv[0], '')} {kv[0]}".lower()]
        facts.append("Largest archives in the newest run: " + ", ".join(f"{names.get(v, v)} ({v}) {gb(s['size'])} = {s['size'] * 100 // max(tot[0], 1)}%" for v, s in sel[:4]) + ".")
        growers = []
        for v, s in newest.items():
            old = [r["vms"][v]["size"] for r in runs[1:] if v in r["vms"] and r["vms"][v]["size"]]
            if old and s["size"] - old[-1] >= 0.5 * 2**30:
                growers.append((s["size"] - old[-1], v, old[-1], s["size"], len(old)))
        for d, v, a, b, n in sorted(growers, reverse=True)[:3]:
            facts.append(f"{names.get(v, v)} ({v}) grew from {gb(a)} to {gb(b)} over {n} run(s).")
        if not growers:
            facts.append("No guest's archive grew by 0.5 GB or more across these runs.")
        follow, rec = [], []
        if big and big[0][1]["size"] >= 0.25 * max(tot[0], 1):
            v = big[0][0]
            follow.append({"agent": "config_drift", "skill": "describe", "args": {"item": f"lxc/{v}"}, "why": f"{names.get(v, v)} is {big[0][1]['size'] * 100 // max(tot[0], 1)}% of each run: what is mounted on it?"})
            follow.append({"agent": "capacity_planner", "skill": "forecast", "args": {"target": f"lxc/{v}"}, "why": f"is the disk of {names.get(v, v)} still growing?"})
            rec.append({"title": f"Stop backing up replaceable data in {names.get(big[0][0], big[0][0])}", "risk": "medium", "steps": [
                f"In Proxmox open {names.get(v, v)} > Resources and note where the big, replaceable data lives (for example downloaded models or caches)",
                "If it sits on its own mount point: edit that mount point and untick Backup",
                "If it lives on the root disk: either move it to its own mount point first, or exclude the guest from the backup job (Datacenter > Backup > edit the job > Selection mode: Exclude selected VMs) and keep a copy of its configuration",
                "Check the size of the next night's backup in the storage's Content tab"],
                "rollback": ["Tick Backup again on the mount point, or remove the guest from the job's exclude list", "Data left out of the backups cannot be restored from them: copy anything irreplaceable elsewhere first"]})
            nm = names.get(v, v)
            rec.append({"title": f"Exclude {nm} ({v}) from the nightly backup job", "risk": "medium", "execute": {"action": "backup_job_exclude", "params": {"vmid": int(v), "exclude": True}}, "steps": [
                "In Proxmox open Datacenter > Backup and edit the nightly job",
                f"Set the selection to 'Include all VMs' and add {nm} ({v}) to the excluded guests (the job already excludes 100 and 102)",
                "Keep a copy of the guest's configuration (the guest's Options/Resources) somewhere else, and check the next night's backup size"],
                "rollback": [f"Remove {v} from the job's excluded guests again (Datacenter > Backup > edit)", "Anything that changed on the guest while it was excluded has no backup from those nights"]})
        key = [facts[1], facts[2]] + [f for f in facts if " grew from " in f][:1]
        return {"summary": facts[1], "facts": facts, "key": key, "followups": follow, "recommend": rec, "data": {"totals_gb": [round(t / 2**30, 1) for t in tot]}}
