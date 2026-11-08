"""The executor: carries out a few named, reversible actions after the owner's approval. This is the only place in the system that writes to Proxmox.

Nothing here is free-form. An action is one of the entries in actionspec.py with typed parameters; there is no shell, no SSH, no 'run this command'. Every run:
  1. PREVIEW   reads the current state (read-only token) and shows exactly which API calls would be made and what would change;
  2. PRECHECKS the executor is on, the action is allowed, the action token exists, the daily limit, no other action running, the state really allows it, and
               (for anything that reduces backup coverage) no backup is running and the last backup is healthy. Any failure blocks it, with the reason;
  3. APPLY     only after the owner pressed 'run it for me' on the Approvals page with the rollback plan in front of them;
  4. VERIFY    reads the state again and compares it with what was intended (and, if switched on, the outside model gives a second look);
  5. ROLLBACK  when the check fails the previous setting is restored automatically (if auto_rollback is on); the owner can also roll back by hand later.
Every step is in the audit log, a maintenance window is opened around the guest, and Discord is told. The write token (PVE_ACT_TOKEN_*) is created by the owner
with only the privileges the allowed actions need; without it nothing can run."""
import asyncio, json, re, time

from . import actionspec as spec
from .sources import PVE, PVEAct, SourceError
from .redact import scrub

IGNORE_GUESTS = {"qemu/100", "qemu/102"}                      # stopped on purpose (the lab's handoff notes)


class ActionError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------- helpers shared by the handlers
def flag_of(value):
    """The effective Backup flag of an LXC mount point string (volume,opt=...,backup=1): 1 when on, else 0 (off is the default for mount points)."""
    for o in (value or "").split(",")[1:]:
        if o.strip() == "backup=1":
            return 1
    return 0


def with_flag(value, backup):
    parts = (value or "").split(",")
    opts = [o for o in parts[1:] if not o.startswith("backup=")]
    opts.append(f"backup={backup}")
    return ",".join([parts[0]] + opts)


def ids(raw):
    return sorted({int(x) for x in re.findall(r"\d+", raw or "")})


# ---------------------------------------------------------------------------------------------------- the three actions
class GuestStart:
    name, backup_related = "guest_start", False

    async def read(self, rd, p):
        base = f"nodes/{rd.node}/{p['kind']}/{p['vmid']}"
        st, cfg = await rd.get(base + "/status/current"), await rd.get(base + "/config")
        return {"status": st.get("status"), "name": st.get("name") or cfg.get("hostname") or cfg.get("name"), "lock": cfg.get("lock")}

    def problems(self, b, p):
        out = []
        if b["status"] != "stopped":
            out.append(f"it is {b['status']}, not stopped")
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        if f"{p['kind']}/{p['vmid']}" in IGNORE_GUESTS:
            out.append("this guest is stopped on purpose")
        return out

    def calls(self, rd, b, p):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/start", "body": {}, "wait": True}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/shutdown", "body": {"timeout": 60}, "wait": True}]

    async def verify(self, rd, b, p, undone=False):
        want = "stopped" if undone else "running"
        for _ in range(30):
            st = (await rd.get(f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/current")).get("status")
            if st == want:
                return True, f"it is {st}", {"status": st}
            await asyncio.sleep(1.5)
        return False, f"it is {st}, expected {want}", {"status": st}

    def changes(self, b, p):
        return [f"{p['kind']}/{p['vmid']} ({b.get('name')}): stopped -> running"]


class LxcMountBackupFlag:
    name, backup_related = "lxc_mount_backup_flag", True

    async def read(self, rd, p):
        cfg = await rd.get(f"nodes/{rd.node}/lxc/{p['vmid']}/config")
        return {"value": cfg.get(p["mp"]), "digest": cfg.get("digest"), "lock": cfg.get("lock"), "name": cfg.get("hostname")}

    def problems(self, b, p):
        out = []
        if not b["value"]:
            out.append(f"{p['mp']} does not exist on this container")
        elif flag_of(b["value"]) == p["backup"]:
            out.append(f"Backup is already {'on' if p['backup'] else 'off'} for {p['mp']}: nothing to change")
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        return out

    def calls(self, rd, b, p):
        return [{"method": "PUT", "path": f"nodes/{rd.node}/lxc/{p['vmid']}/config", "body": {p["mp"]: with_flag(b["value"], p["backup"]), "digest": b["digest"]}, "wait": False}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "PUT", "path": f"nodes/{rd.node}/lxc/{p['vmid']}/config", "body": {p["mp"]: b["value"], "digest": (after or {}).get("digest")}, "wait": False}]

    async def verify(self, rd, b, p, undone=False):
        cfg = await rd.get(f"nodes/{rd.node}/lxc/{p['vmid']}/config")
        val = cfg.get(p["mp"])
        want = flag_of(b["value"]) if undone else p["backup"]
        return flag_of(val) == want, f"{p['mp']} is now '{val}'", {"value": val, "digest": cfg.get("digest")}

    def changes(self, b, p):
        return [f"lxc/{p['vmid']} ({b.get('name')}) {p['mp']}: '{b['value']}' -> '{with_flag(b['value'], p['backup']) if b['value'] else '?'}'",
                "(a mount point change may only take effect at the next backup or restart)"]


class BackupJobExclude:
    name, backup_related = "backup_job_exclude", True

    async def read(self, rd, p):
        jobs = await rd.get("cluster/backup")
        jid = p.get("job") or (jobs[0]["id"] if len(jobs) == 1 else None)
        if not jid:
            return {"error": "there are several backup jobs: say which one (job id)"}
        j = await rd.get(f"cluster/backup/{jid}")
        return {"job": jid, "all": bool(j.get("all")), "exclude": ids(j.get("exclude")), "exclude_raw": j.get("exclude", ""), "digest": j.get("digest")}

    def problems(self, b, p):
        if b.get("error"):
            return [b["error"]]
        out = []
        if not b["all"]:
            out.append("this job lists its guests explicitly instead of 'all': take the guest out of the list by hand")
        if p["exclude"] and p["vmid"] in b["exclude"]:
            out.append(f"guest {p['vmid']} is already excluded")
        if not p["exclude"] and p["vmid"] not in b["exclude"]:
            out.append(f"guest {p['vmid']} is not excluded")
        return out

    def _body(self, new, digest):
        return {"exclude": ",".join(map(str, new)), "digest": digest} if new else {"delete": "exclude", "digest": digest}

    def _new(self, b, p):
        return sorted(set(b["exclude"]) | {p["vmid"]}) if p["exclude"] else sorted(set(b["exclude"]) - {p["vmid"]})

    def calls(self, rd, b, p):
        return [{"method": "PUT", "path": f"cluster/backup/{b['job']}", "body": self._body(self._new(b, p), b["digest"]), "wait": False}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "PUT", "path": f"cluster/backup/{b['job']}", "body": self._body(b["exclude"], (after or {}).get("digest")), "wait": False}]

    async def verify(self, rd, b, p, undone=False):
        j = await rd.get(f"cluster/backup/{b['job']}")
        got = ids(j.get("exclude"))
        want = b["exclude"] if undone else self._new(b, p)
        return got == want, f"the exclude list is now {got or 'empty'}", {"exclude": got, "digest": j.get("digest")}

    def changes(self, b, p):
        return [f"backup job {b.get('job')}: exclude {b.get('exclude') or 'nothing'} -> {self._new(b, p) or 'nothing'}"] if not b.get("error") else []


# ---------------------------------------------------------------------------------------------------- added in 1.4
class _Guest:
    """Shared reading for the guest actions."""
    async def read(self, rd, p):
        base = f"nodes/{rd.node}/{p['kind']}/{p['vmid']}"
        st, cfg = await rd.get(base + "/status/current"), await rd.get(base + "/config")
        return {"status": st.get("status"), "uptime": st.get("uptime", 0), "name": st.get("name") or cfg.get("hostname") or cfg.get("name"), "lock": cfg.get("lock")}

    def label(self, b, p):
        return f"{p['kind']}/{p['vmid']} ({b.get('name')})"


class GuestBackupNow(_Guest):
    name, backup_related = "guest_backup_now", False
    STORAGE = "backups"

    async def read(self, rd, p):
        b = await super().read(rd, p)
        arch = [x for x in (await rd.get(f"nodes/{rd.node}/storage/{self.STORAGE}/content", {"content": "backup", "vmid": p["vmid"]}) or [])]
        arch.sort(key=lambda x: x.get("ctime", 0))
        stor = await rd.get(f"nodes/{rd.node}/storage/{self.STORAGE}/status")
        active = await rd.get(f"nodes/{rd.node}/tasks", {"source": "active"})
        b.update(last_ctime=arch[-1]["ctime"] if arch else 0, last_size=arch[-1].get("size", 0) if arch else 0, archives=len(arch),
                 free=stor.get("avail", 0), running=len([t for t in (active or []) if "vzdump" in str(t.get("type", ""))]))
        return b

    def problems(self, b, p):
        out = []
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        if b.get("running"):
            out.append("a backup is already running")
        if b.get("last_size") and b["free"] < 1.3 * b["last_size"]:
            out.append(f"not enough free space on '{self.STORAGE}' ({b['free'] / 2**30:.1f} GiB free, last archive {b['last_size'] / 2**30:.1f} GiB)")
        return out

    def calls(self, rd, b, p):
        return [{"method": "POST", "path": f"nodes/{rd.node}/vzdump", "body": {"vmid": str(p["vmid"]), "storage": self.STORAGE, "mode": "snapshot", "compress": "zstd",
                                                                              "notes-template": "{{guestname}} (Prxy-AI Command)"}, "wait": True, "timeout": 3600}]

    def undo_calls(self, rd, b, p, after):
        return []                                    # an extra archive harms nothing; the job's keep-last rule prunes it

    async def verify(self, rd, b, p, undone=False):
        if undone:
            return True, "nothing to undo", {}
        arch = await rd.get(f"nodes/{rd.node}/storage/{self.STORAGE}/content", {"content": "backup", "vmid": p["vmid"]}) or []
        new = [x for x in arch if x.get("ctime", 0) > b["last_ctime"]]
        if not new:
            return False, "no new archive appeared", {"archives": len(arch)}
        n = max(new, key=lambda x: x["ctime"])
        return True, f"new archive of {n.get('size', 0) / 2**30:.2f} GiB", {"archives": len(arch), "new_size": n.get("size", 0), "volid": n.get("volid")}

    def changes(self, b, p):
        return [f"{self.label(b, p)}: a new backup archive on '{self.STORAGE}'" + (f" (last one was {b['last_size'] / 2**30:.1f} GiB)" if b.get("last_size") else "")]


class GuestSnapshot(_Guest):
    name, backup_related = "guest_snapshot", False

    async def read(self, rd, p):
        b = await super().read(rd, p)
        b["snapshots"] = [x["name"] for x in (await rd.get(f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/snapshot") or []) if x.get("name") != "current"]
        return b

    def problems(self, b, p):
        out = []
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        if p["name"] in b.get("snapshots", []):
            out.append(f"a snapshot called {p['name']} already exists")
        return out

    def calls(self, rd, b, p):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/snapshot", "body": {"snapname": p["name"], "description": "restore point taken by Prxy-AI Command"}, "wait": True, "timeout": 900}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "DELETE", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/snapshot/{p['name']}", "body": {}, "wait": True, "timeout": 900}]

    async def verify(self, rd, b, p, undone=False):
        names = [x["name"] for x in (await rd.get(f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/snapshot") or [])]
        ok = (p["name"] not in names) if undone else (p["name"] in names)
        return ok, f"snapshot {p['name']} {'is' if p['name'] in names else 'is not'} there", {"snapshots": [n for n in names if n != "current"]}

    def changes(self, b, p):
        return [f"{self.label(b, p)}: new snapshot '{p['name']}' (existing: {', '.join(b.get('snapshots') or []) or 'none'})"]


class GuestReboot(_Guest):
    name, backup_related = "guest_reboot", False

    def problems(self, b, p):
        out = []
        if b["status"] != "running":
            out.append(f"it is {b['status']}, not running")
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        return out

    def calls(self, rd, b, p):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/reboot", "body": {}, "wait": True, "timeout": 300}]

    def undo_calls(self, rd, b, p, after):
        return []

    async def verify(self, rd, b, p, undone=False):
        if undone:
            return True, "nothing to undo", {}
        for _ in range(60):
            st = await rd.get(f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/current")
            if st.get("status") == "running" and st.get("uptime", 0) < max(b.get("uptime", 0), 1):
                return True, f"running again (up {st.get('uptime', 0)} s)", {"status": "running", "uptime": st.get("uptime", 0)}
            await asyncio.sleep(2)
        return False, f"it is {st.get('status')} and its uptime did not restart", {"status": st.get("status"), "uptime": st.get("uptime", 0)}

    def changes(self, b, p):
        return [f"{self.label(b, p)}: restarts (down for about a minute)"]


class GuestShutdown(GuestStart):
    name, backup_related = "guest_shutdown", False

    async def read(self, rd, p):
        return await _Guest.read(self, rd, p)

    def problems(self, b, p):
        out = []
        if b["status"] != "running":
            out.append(f"it is {b['status']}, not running")
        if b.get("lock"):
            out.append(f"it is locked ({b['lock']})")
        return out

    def calls(self, rd, b, p):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/shutdown", "body": {"timeout": 120}, "wait": True, "timeout": 300}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "POST", "path": f"nodes/{rd.node}/{p['kind']}/{p['vmid']}/status/start", "body": {}, "wait": True}]

    async def verify(self, rd, b, p, undone=False):
        return await GuestStart.verify(self, rd, b, p, undone=not undone)       # shutting down = the reverse of starting

    def changes(self, b, p):
        return [f"{p['kind']}/{p['vmid']} ({b.get('name')}): running -> stopped"]


class BackupJobExcludePath(BackupJobExclude):
    name, backup_related = "backup_job_exclude_path", True

    def reduces(self, p):
        return p["exclude"]

    async def read(self, rd, p):
        jobs = await rd.get("cluster/backup")
        jid = p.get("job") or (jobs[0]["id"] if len(jobs) == 1 else None)
        if not jid:
            return {"error": "there are several backup jobs: say which one (job id)"}
        j = await rd.get(f"cluster/backup/{jid}")
        raw = j.get("exclude-path") or []
        paths = raw if isinstance(raw, list) else [x for x in str(raw).split(",") if x]
        return {"job": jid, "paths": paths, "digest": j.get("digest")}

    def problems(self, b, p):
        if b.get("error"):
            return [b["error"]]
        if p["exclude"] and p["path"] in b["paths"]:
            return [f"{p['path']} is already skipped"]
        if not p["exclude"] and p["path"] not in b["paths"]:
            return [f"{p['path']} is not skipped"]
        return []

    def _new(self, b, p):
        return (b["paths"] + [p["path"]]) if p["exclude"] else [x for x in b["paths"] if x != p["path"]]

    def _pbody(self, paths, digest):
        body = {"exclude-path": paths} if paths else {"delete": "exclude-path"}
        if digest:
            body["digest"] = digest
        return body

    def calls(self, rd, b, p):
        return [{"method": "PUT", "path": f"cluster/backup/{b['job']}", "body": self._pbody(self._new(b, p), b.get("digest")), "wait": False}]

    def undo_calls(self, rd, b, p, after):
        return [{"method": "PUT", "path": f"cluster/backup/{b['job']}", "body": self._pbody(b["paths"], (after or {}).get("digest")), "wait": False}]

    async def verify(self, rd, b, p, undone=False):
        j = await rd.get(f"cluster/backup/{b['job']}")
        raw = j.get("exclude-path") or []
        got = raw if isinstance(raw, list) else [x for x in str(raw).split(",") if x]
        want = b["paths"] if undone else self._new(b, p)
        return sorted(got) == sorted(want), f"skipped folders: {', '.join(got) or 'none'}", {"paths": got, "digest": j.get("digest")}

    def changes(self, b, p):
        return [f"backup job {b.get('job')}: skipped folders {b.get('paths') or 'none'} -> {self._new(b, p) or 'none'}"] if not b.get("error") else []


HANDLERS = {h.name: h for h in (GuestStart(), LxcMountBackupFlag(), BackupJobExclude(), GuestBackupNow(), GuestSnapshot(), GuestReboot(), GuestShutdown(), BackupJobExcludePath())}


class Actions:
    def __init__(self, runner, notifier=None, frontier=None):
        self.rt, self.store, self.bus, self.cfg = runner, runner.store, runner.bus, runner.cfg
        self.notifier, self.frontier = notifier, frontier
        self.tasks = {}
        self.lock = asyncio.Lock()

    def policy(self):
        return self.cfg.get()["actions"]

    def status(self):
        p = self.policy()
        day = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return {"enabled": p["enabled"], "token_configured": PVEAct().configured(), "allowed": p["allowed"], "max_per_day": p["max_per_day"], "today": self.store.actions_since(day), "auto_rollback": p["auto_rollback"],
                "catalogue": [{"name": n, **{k: v for k, v in s.items()}} for n, s in spec.ACTIONS.items()]}

    # ------------------------------------------------------------------ preview and prechecks
    async def preview(self, action, params):
        p = spec.validate_params(action, params)
        h, pol = HANDLERS[action], self.policy()
        rd, wr = PVE(), PVEAct()
        checks = [{"name": "the executor is switched on", "ok": pol["enabled"], "detail": "" if pol["enabled"] else "Settings > Actions"},
                  {"name": "this action is allowed", "ok": action in pol["allowed"], "detail": "" if action in pol["allowed"] else "tick it under Settings > Actions"},
                  {"name": "the action token exists", "ok": wr.configured(), "detail": "" if wr.configured() else "create it with tools/create-action-token.sh and restart"}]
        day = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        used = self.store.actions_since(day)
        checks.append({"name": "under today's limit", "ok": used < pol["max_per_day"], "detail": f"{used} of {pol['max_per_day']} used today"})
        busy = [a for a in self.store.action_list(10) if a["status"] in ("running", "rolling_back")]
        checks.append({"name": "no other action is running", "ok": not busy, "detail": ""})
        before, calls = None, []
        try:
            before = await h.read(rd, p)
            prob = h.problems(before, p)
            checks.append({"name": "the current state allows it", "ok": not prob, "detail": "; ".join(prob)})
            if not prob:
                calls = h.calls(rd, before, p)
            reduces = h.reduces(p) if hasattr(h, "reduces") else (h.backup_related and (action != "lxc_mount_backup_flag" or p["backup"] == 0))
            if reduces:
                active = await rd.get(f"nodes/{rd.node}/tasks", {"source": "active"})
                running = [t for t in (active or []) if "vzdump" in str(t.get("type", ""))]
                checks.append({"name": "no backup is running right now", "ok": not running, "detail": f"{len(running)} backup task(s) active" if running else ""})
                bad = [f for f in self.store.open_findings(300) if f["agent"] == "backup_warden" and (f["fingerprint"].startswith(("backup:no-recent-run", "backup:last-run")) or f["severity"] in ("high", "critical"))]
                checks.append({"name": "the last backup is healthy (so reducing coverage is safe)", "ok": not bad, "detail": bad[0]["title"] if bad else ""})
        except SourceError as e:
            checks.append({"name": "the current state can be read", "ok": False, "detail": str(e)[:200]})
        shown = [{"method": c["method"], "path": c["path"], "body": {k: v for k, v in c["body"].items() if k != "digest"}} for c in calls]
        s = spec.ACTIONS[action]
        return {"action": action, "params": p, "title": s["title"], "what": spec.describe(action, p), "risk": s["risk"], "reversible": s["reversible"], "privileges": s["privileges"],
                "before": before, "changes": h.changes(before, p) if before is not None and not (before.get("error")) else [], "calls": shown, "prechecks": checks, "ok": all(c["ok"] for c in checks)}

    # ------------------------------------------------------------------ start, execute, roll back
    async def start(self, finding_id, action, params, project=None, note=""):
        pv = await self.preview(action, params)
        aid = self.store.action_create(action, pv["params"], finding_id, project, pv["prechecks"])
        self.store.audit("owner", "action.approved", f"action#{aid}", f"{pv['what']} (finding {finding_id}) {note[:80]}")
        if not pv["ok"]:
            why = "; ".join(f"{c['name']}: {c['detail'] or 'no'}" for c in pv["prechecks"] if not c["ok"])
            self.store.action_update(aid, status="blocked", note=why[:400], finished=time.time())
            self.store.audit("system", "action.blocked", f"action#{aid}", why[:300])
            return aid, "blocked"
        self.tasks[aid] = asyncio.create_task(self._execute(aid, pv))
        return aid, "running"

    async def _calls(self, wr, calls):
        out = []
        for c in calls:
            res = await wr.call(c["method"], c["path"], c["body"] or None)
            if c.get("wait") and isinstance(res, str) and res.startswith("UPID"):
                await wr.wait_task(res, c.get("timeout", 90))
            out.append({"method": c["method"], "path": c["path"], "ok": True})
        return out

    async def _execute(self, aid, pv):
        async with self.lock:
            a = self.store.action_get(aid)
            h, p = HANDLERS[a["action"]], a["params"]
            rd, wr = PVE(), PVEAct()
            self.store.action_update(aid, status="running", started=time.time(), before=pv["before"])
            self.bus.publish("action.started", agent="commander", id=aid, what=pv["what"])
            scope = [f"{p['kind']}/{p['vmid']}" if "kind" in p else f"lxc/{p['vmid']}"] if "vmid" in p else ["storage/backups"]
            wid = self.store.maint_add(time.time(), time.time() + 20 * 60, scope, [], "", f"the team is carrying out: {pv['what']}")
            applied = False
            try:
                before = pv["before"]
                calls = h.calls(rd, before, p)
                done = await self._calls(wr, calls)
                applied = True
                self.store.action_update(aid, calls=done)
                self.store.audit("agent:commander", "action.applied", f"action#{aid}", pv["what"])
                ok, detail, after = await h.verify(rd, before, p)
                self.store.action_update(aid, after=after, verify={"ok": ok, "detail": detail})
                if ok:
                    await self._finish_ok(aid, a, pv, detail, before, after)
                else:
                    self.store.audit("system", "action.verify_failed", f"action#{aid}", detail[:200])
                    self.store.action_update(aid, status="verify_failed", note=detail[:300])
                    if self.policy()["auto_rollback"]:
                        await self._rollback(aid, "the check afterwards failed: " + detail, rd, wr)
                    else:
                        await self._notify(aid)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:250]}"
                self.store.audit("system", "action.failed", f"action#{aid}", msg)
                self.store.action_update(aid, status="failed", note=msg, finished=time.time())
                if applied and self.policy()["auto_rollback"]:
                    try:
                        await self._rollback(aid, "an error after the change: " + msg, rd, wr)
                    except Exception:
                        pass
                await self._notify(aid)
            # the maintenance window is left to run out by itself: it also covers the minutes after the change

    async def _finish_ok(self, aid, a, pv, detail, before, after):
        st = self.store
        st.action_update(aid, status="done", finished=time.time(), note=detail[:300])
        st.audit("agent:commander", "action.verified", f"action#{aid}", detail[:200])
        if a["finding_id"]:
            st.set_status(a["finding_id"], "done")
            for ap in st.approvals_for_finding(a["finding_id"]):
                if ap["status"] == "approved":
                    st.approval_update(ap["id"], status="done", executed_at=time.time(), result={"ok": True, "executed_by": "the team", "action_id": aid})
            self.bus.publish("finding.status", agent="commander", id=a["finding_id"], status="done")
        if self.frontier and self.frontier.available("verify"):
            try:
                from .frontier import FrontierError, extract_json
                r = await self.frontier.ask("verify", "You check whether a small configuration change on a home-lab Proxmox host did what was intended. Reply with ONE JSON object: {\"verdict\": \"ok\" or \"concern\", \"note\": \"one or two plain sentences\"}. Use only the facts given.",
                                            f"Intended: {pv['what']}.\nState before: {json.dumps(before)[:700]}\nState after: {json.dumps(after)[:700]}\nCalls made: {json.dumps(pv['calls'])[:500]}\nOur own check said: {detail}", 300)
                j = extract_json(r["text"]) or {}
                st.action_update(aid, outside={"backend": r["backend"], "verdict": j.get("verdict") if j.get("verdict") in ("ok", "concern") else "ok", "note": scrub(str(j.get("note", "")))[:300]})
            except Exception:
                pass
        await self._notify(aid)
        self.bus.publish("action.done", agent="commander", id=aid)

    async def rollback(self, aid, why="the owner asked for it"):
        a = self.store.action_get(aid)
        if not a or a["status"] not in ("done", "verify_failed", "failed"):
            raise ActionError("only an action that was carried out can be rolled back")
        async with self.lock:
            await self._rollback(aid, why, PVE(), PVEAct())

    async def _rollback(self, aid, why, rd, wr):
        a = self.store.action_get(aid)
        h, p = HANDLERS[a["action"]], a["params"]
        self.store.action_update(aid, status="rolling_back")
        self.store.audit("agent:commander", "action.rollback_started", f"action#{aid}", why[:200])
        try:
            await self._calls(wr, h.undo_calls(rd, a["before"], p, a["after"] or {}))
            ok, detail, _ = await h.verify(rd, a["before"], p, undone=True)
            self.store.action_update(aid, status="rolled_back" if ok else "rollback_failed", finished=time.time(), note=(why + " | " + detail)[:400])
            self.store.audit("agent:commander" if ok else "system", "action.rolled_back" if ok else "action.rollback_failed", f"action#{aid}", detail[:200])
        except Exception as e:
            self.store.action_update(aid, status="rollback_failed", finished=time.time(), note=f"{why} | rollback error: {type(e).__name__}: {str(e)[:200]}")
            self.store.audit("system", "action.rollback_failed", f"action#{aid}", str(e)[:200])
        await self._notify(aid)

    async def _notify(self, aid):
        if self.notifier:
            try:
                await self.notifier.action_event(self.store.action_get(aid))
            except Exception:
                pass
