#!/usr/bin/env python3
"""prxy-runner: the AI team's hands on the Proxmox host. It carries out MISSIONS the owner approved in Prxy-AI Command, and nothing else.

Trust rules (all enforced here, on the host, whatever the dashboard says):
  * only requests from the Prxy-AI Command container (RUNNER_ALLOW, default 10.20.40.139)
  * only missions signed with the shared secret (HMAC-SHA256 over the exact mission text the owner approved); a mission runs once (journal)
  * every command is checked against a block-list (disk wiping, deleting guests/storage, host network/cluster config, credentials, piping downloads into a shell,
    rebooting the host, stopping Prxy-AI Command itself); a blocked step refuses the whole mission before anything runs
  * targets: the host, or LXC containers in RUNNER_TARGETS (never VMs, never anything not listed)
  * step 0 is automatic: a snapshot of every container the mission touches, and a copy of /etc when the host is touched
  * each step: run -> check (exit code / expected text) -> next. A failed run or check stops the mission and runs the completed steps' rollbacks in reverse
  * one mission at a time, a time limit per step, an abort endpoint, and a kill switch: create /etc/prxy-runner.disabled (or stop the service)
Stdlib only. Config: /etc/prxy-runner.env (RUNNER_SECRET, RUNNER_BIND, RUNNER_PORT, RUNNER_ALLOW, RUNNER_TARGETS). Journal: /var/lib/prxy-runner/."""
import hashlib, hmac, json, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("RUNNER_SECRET", "").encode()
BIND, PORT = os.environ.get("RUNNER_BIND", "10.20.40.224"), int(os.environ.get("RUNNER_PORT", "8330"))
ALLOW = set(filter(None, os.environ.get("RUNNER_ALLOW", "10.20.40.139").split(",")))
TARGETS = {int(x) for x in re.findall(r"\d+", os.environ.get("RUNNER_TARGETS", ""))}
STATE_DIR = os.environ.get("RUNNER_STATE", "/var/lib/prxy-runner")
KILL = os.environ.get("RUNNER_KILL", "/etc/prxy-runner.disabled")
SELF_CT = int(os.environ.get("RUNNER_SELF_CT", "139"))
PCT = os.environ.get("RUNNER_PCT", "pct")
OUT_MAX = 4000

BLOCK = [
    (r"\bmkfs(\.\w+)?\b|\bwipefs\b|\bsgdisk\b|\bfdisk\b|\bparted\b|\bblkdiscard\b", "formats or repartitions disks"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "writes raw to a device"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*(/|/\*|/etc|/etc/pve\S*|/var/lib/vz\S*|/boot\S*|/root|/home|/usr|/var|/mnt/pve\S*)(\s|$)", "deletes a system or storage directory"),
    (r"\b(pct|qm)\s+(destroy|rollback|set\b.*--delete)|\bpvesm\s+(remove|free)|\blv(remove|reduce)\b|\bvg(remove|reduce)\b|\bzpool\s+destroy|\bzfs\s+destroy", "deletes guests, storage or volumes"),
    (r"/etc/network/interfaces|/etc/pve/corosync|\bifdown\b|\bifreload\b|\bip\s+(link|addr|route)\s+(set|del|flush)|\bnft\s+flush|\biptables\s+-F", "changes the host's network"),
    (r"\bpveum\b|\bpasswd\b|\bchpasswd\b|\busermod\b.*-p|authorized_keys|/etc/shadow|/etc/sudoers", "changes users, passwords or credentials"),
    (r"(curl|wget)\b[^|;]*\|\s*(ba|z|da)?sh\b", "runs a downloaded script"),
    (r"(^|[;&|]\s*)(reboot|poweroff|halt|shutdown)\b|\bsystemctl\s+(reboot|poweroff|halt|kexec)", "reboots or powers off a machine"),
    (r"prxy-runner|prxy-command\.service|systemctl\s+(stop|disable|mask)\s+prxy-command", "tampers with the mission system itself"),
    (r"\bhistory\s+-c\b|>\s*/var/log/|\bjournalctl\b.*--vacuum|\bshred\b", "hides or destroys logs"),
    (r":\(\)\s*\{", "fork bomb"),
]

# ---- read-only: an ALLOW-list (not a block-list). A command is read-only only if every part of it is a known reading command used in a reading way.
# Used for: runs that skip restore points (they change nothing), and the dashboard's Autopilot (read-only diagnosis may run without a click).
READ_CMDS = {"cat", "head", "tail", "grep", "egrep", "zgrep", "zcat", "ls", "stat", "find", "wc", "sort", "uniq", "cut", "tr", "awk", "sed", "df", "du", "free",
             "uptime", "uname", "hostname", "date", "id", "whoami", "ps", "ss", "netstat", "ip", "lsblk", "findmnt", "systemctl", "journalctl", "pct", "qm", "pvesh",
             "pvesm", "pveversion", "lvs", "vgs", "pvs", "zpool", "zfs", "apt", "apt-cache", "apt-get", "dpkg", "dpkg-query", "docker", "curl", "nc", "timeout",
             "echo", "printf", "true", "test", "[", "getent", "dig", "nslookup", "host", "ping", "which", "command", "type", "file", "md5sum", "sha256sum",
             "nproc", "lscpu", "sensors", "nvidia-smi", "smartctl", "lsof", "last", "who", "column", "xargs", "jq", "basename", "dirname", "readlink", "realpath",
             "promtail", "loki", "logcli", "ollama", "mount", "sleep", "seq", "tac", "diff", "cmp", "od", "strings", "nl", "fold", "expr", "dmesg", "tree"}
READ_SUB = {   # command -> its first argument must be one of these (or, for apt-get/curl/sed/find, special rules below)
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "list-unit-files", "show", "cat", "--no-pager", "-a", "--failed"},
    "pct": {"list", "config", "status", "listsnapshot", "df", "pending"}, "qm": {"list", "config", "status", "listsnapshot", "pending"},
    "pvesh": {"get", "ls"}, "pvesm": {"status", "list"}, "zpool": {"status", "list", "get", "iostat"}, "zfs": {"list", "get"},
    "apt": {"list", "policy", "show", "search"}, "apt-cache": {"policy", "show", "search", "depends", "rdepends", "madison"},
    "dpkg": {"-l", "-L", "-s", "-S", "--list", "--status", "--listfiles", "--search", "--print-architecture"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "version", "info", "top", "port"}, "ollama": {"list", "ps", "show"},
    "ip": {"a", "addr", "address", "link", "route", "r", "neigh", "-br", "-4", "-6", "-j", "-s"}, "logcli": {"query", "labels", "series"},
}
SECRET_RX = re.compile(r"shadow|gshadow|/etc/pve/priv|prxy-runner\.env|/etc/prxy-command|\.env\b|\benv\b|printenv|secret|token|passw|\.key\b|\.pem\b|id_rsa|id_ed25519|\.ssh/|credential", re.I)


def _segments(cmd):
    """Split a shell line into simple commands (on ; && || | & newlines, and inside $( ) )."""
    cmd = re.sub(r"[0-9]?>&[0-9]|&>>?\s*/dev/null|[0-9]?>>?\s*/dev/null", " ", cmd)      # harmless redirects are not command separators
    inner = re.findall(r"\$\(([^()]*)\)", cmd)
    flat = re.sub(r"\$\(([^()]*)\)", " X ", cmd)
    parts = re.split(r"\|\||&&|[;|&\n]", flat)
    for i in inner:
        parts += _segments(i)
    return [p.strip() for p in parts if p.strip()]


def readonly_ok(cmd):
    """-> None if the command only reads, else the reason it does not count as read-only."""
    cmd = (cmd or "").strip()
    if not cmd:
        return None
    if blocked(cmd):
        return "blocked"
    if "`" in cmd or re.search(r"<\(|>\(", cmd):
        return "uses backticks or process substitution"
    if SECRET_RX.search(cmd):
        return "touches secrets or environment variables"
    if re.search(r">>?\|?\s*(?!/dev/null\b|&[0-9]\b)\S", re.sub(r"'[^']*'|\"[^\"]*\"", "''", cmd)):
        return "writes to a file (redirect)"
    for seg in _segments(cmd):
        w = seg.split()
        while w and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", w[0]):
            w = w[1:]                                    # VAR=x prefixes
        while w and w[0] in ("timeout", "xargs", "command", "nice", "!"):
            w = w[1:]
            while w and (w[0].startswith("-") or re.fullmatch(r"\d+[smh]?", w[0])):
                w = w[1:]
        if not w or w[0] in ("X", "{", "}", "(", ")", "then", "do", "done", "fi", "else", "if", "for", "while", "in"):
            continue
        c = w[0].rsplit("/", 1)[-1]
        if c not in READ_CMDS:
            return f"'{c}' is not on the read-only list"
        args = w[1:]
        if c in READ_SUB and args and not any(a in READ_SUB[c] for a in args[:2]) and c != "ip":
            return f"'{c} {args[0]}' may change something"
        if c == "ip" and re.search(r"\b(add|del|delete|set|flush|change|replace|append)\b", seg):
            return "'ip' changes something"
        if c == "apt-get" and not re.search(r"(^|\s)(-s|--simulate|--dry-run|check|-qq\s+check)(\s|$)", seg):
            return "'apt-get' without --simulate"
        if c == "sed" and (re.search(r"(^|\s)-i|--in-place", seg) or re.search(r"[;/ ]w\s|/w$|\bw /", seg)):
            return "'sed' writes a file"
        if c == "find" and re.search(r"-(delete|exec|execdir|ok|okdir|fprint\w*|fls)\b", seg):
            return "'find' would change or run something"
        if c == "awk" and re.search(r"system\s*\(|print[f]?\s*>|\|\s*\"|getline", seg):
            return "'awk' runs commands or writes"
        if c == "curl" and re.search(r"(^|\s)(-X\s*(?!GET)\w+|--request\s+(?!GET)\w+|-d|--data\S*|-F|--form|-T|--upload-file|-o|--output|-O|--remote-name)(\s|$)", seg):
            return "'curl' sends data or writes a file"
        if c == "xargs":
            return "xargs runs other commands"
        if c == "journalctl" and re.search(r"--(vacuum|rotate|flush|sync|relinquish)", seg):
            return "'journalctl' changes the journal"
        if c == "docker" and args and args[0] == "stats" and "--no-stream" not in seg:
            return "'docker stats' never ends without --no-stream"
        if c == "mount" and args:
            return "'mount' with arguments"
        if c in ("smartctl",) and re.search(r"-(s|t|X|-set|-test)\b", seg):
            return "'smartctl' changes the disk's settings"
        if c == "nvidia-smi" and re.search(r"(^|\s)-(pm|pl|r|e|c|ac|rac|acp|lgc|rgc)\b|--(persistence|power-limit|reset|gpu-reset|ecc)", seg):
            return "'nvidia-smi' changes the GPU's settings"
        if c == "dmesg" and re.search(r"-(c|C|D|E|n)\b|--(clear|console)", seg):
            return "'dmesg' changes the kernel log"
    return None


def mission_readonly(m):
    return all(readonly_ok(s.get(k, "")) is None for s in m.get("steps") or [] for k in ("run", "check"))


LOCK = threading.Lock()
RUNS = {}


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def canonical(m):
    return json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def blocked(cmd):
    for rx, why in BLOCK:
        if re.search(rx, cmd or "", re.I):
            return why
    return None


def target_ok(t):
    if t == "host":
        return True
    m = re.fullmatch(r"lxc:(\d+)", t or "")
    return bool(m) and int(m.group(1)) in TARGETS


def validate(m):
    """-> list of problems (empty = runnable)."""
    probs = []
    steps = m.get("steps") or []
    if not 1 <= len(steps) <= 25:
        probs.append("a mission needs 1 to 25 steps")
    for s in steps:
        if not target_ok(s.get("target")):
            probs.append(f"step {s.get('n')}: target {s.get('target')} is not allowed (host or a listed container only)")
        for k in ("run", "check", "rollback"):
            why = blocked(s.get(k, ""))
            if why:
                probs.append(f"step {s.get('n')} {k}: refused ({why})")
        if s.get("target") == f"lxc:{SELF_CT}" and re.search(r"\bsystemctl\s+(stop|restart)\b", s.get("run", "")):
            probs.append(f"step {s.get('n')}: will not stop or restart services in Prxy-AI Command's own container")
        if not str(s.get("run", "")).strip():
            probs.append(f"step {s.get('n')}: nothing to run")
    return probs


def sh(target, cmd, timeout):
    if target == "host":
        args = ["bash", "-c", cmd]
    else:
        args = [PCT, "exec", target.split(":")[1], "--", "bash", "-c", cmd]
    t0 = time.time()
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
        return p.returncode, out[-OUT_MAX:], round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s", round(time.time() - t0, 1)
    except OSError as e:
        return 127, str(e), 0.0


def check_ok(step, rc, out):
    exp = step.get("expect") or "exit0"
    if exp.startswith("contains:"):
        return rc == 0 and exp[9:] in out
    if exp.startswith("absent:"):
        return rc == 0 and exp[7:] not in out
    return rc == 0


def save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"mission-{st['id']}.json"), "w") as f:
        json.dump(st, f, indent=1)


def run_mission(m):
    mid = m["id"]
    st = RUNS[mid]
    st["state"] = "running"
    done = []
    try:
        # step 0: restore points (not for a run that only reads: there is nothing to restore, and snapshots would fill the thin pool)
        st["readonly"] = bool(m.get("readonly")) and mission_readonly(m)
        for t in ([] if st["readonly"] else sorted({s["target"] for s in m["steps"]})):
            if os.path.exists(KILL) or st.get("abort"):
                raise RuntimeError("stopped (kill switch or abort)")
            if t == "host":
                path = f"/root/backups/prxy-mission-{mid}-etc.tgz"
                rc, out, sec = sh("host", f"mkdir -p /root/backups && tar czf {path} /etc 2>/dev/null; test -s {path} && echo saved {path}", 300)
            else:
                ct = t.split(":")[1]
                rc, out, sec = sh("host", f"{PCT} snapshot {ct} prxy-m{mid} --description 'before Prxy-AI Command mission {mid}'", 600)
            st["restore_points"].append({"target": t, "ok": rc == 0, "out": out[-300:]})
            save(st)
            if rc != 0:
                raise RuntimeError(f"could not make a restore point for {t}: {out[-200:]}")
        for s in m["steps"]:
            if os.path.exists(KILL) or st.get("abort"):
                raise RuntimeError("stopped (kill switch or abort)")
            rec = {"n": s["n"], "title": s.get("title", ""), "state": "running", "started": time.time()}
            st["steps"].append(rec); save(st)
            rc, out, sec = sh(s["target"], s["run"], int(s.get("timeout") or 300))
            rec.update(rc=rc, out=out, seconds=sec)
            if rc != 0:
                rec["state"] = "failed"; done.append(s); save(st)
                raise RuntimeError(f"step {s['n']} failed (exit {rc})")
            if s.get("check"):
                crc, cout, _ = sh(s["target"], s["check"], 120)
                rec.update(check_rc=crc, check_out=cout)
                if not check_ok(s, crc, cout):
                    rec["state"] = "check_failed"; done.append(s); save(st)
                    raise RuntimeError(f"step {s['n']}: the check did not pass")
            rec["state"] = "ok"; done.append(s); save(st)
        st["state"] = "done"
    except Exception as e:
        st["state"], st["error"] = "failed", str(e)[:300]
        save(st)
        for s in reversed(done):                         # undo what was done, newest first
            if not s.get("rollback"):
                st["rollback"].append({"n": s["n"], "state": "nothing to undo"}); continue
            rc, out, sec = sh(s["target"], s["rollback"], int(s.get("timeout") or 300))
            st["rollback"].append({"n": s["n"], "state": "ok" if rc == 0 else "failed", "rc": rc, "out": out})
            save(st)
        st["state"] = "rolled_back" if all(r["state"] != "failed" for r in st["rollback"]) else "rollback_failed"
    st["finished"] = time.time()
    save(st)
    log(f"mission {mid}: {st['state']}")


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _allowed(self):
        return self.client_address[0] in ALLOW and SECRET

    def _sig_ok(self, text, sig):
        return hmac.compare_digest(hmac.new(SECRET, text.encode(), hashlib.sha256).hexdigest(), str(sig or ""))

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self._allowed():
            return self._json(403, {"error": "not allowed"})
        m = re.fullmatch(r"/status/(\d+)", self.path)
        if self.path == "/health":
            return self._json(200, {"ok": True, "disabled": os.path.exists(KILL), "targets": sorted(TARGETS), "busy": any(r["state"] == "running" for r in RUNS.values())})
        if m and self._sig_ok(f"status:{m.group(1)}", self.headers.get("X-Sig")):
            st = RUNS.get(int(m.group(1)))
            if not st:
                p = os.path.join(STATE_DIR, f"mission-{m.group(1)}.json")
                st = json.load(open(p)) if os.path.exists(p) else None
            return self._json(200 if st else 404, st or {"error": "unknown mission"})
        return self._json(404, {"error": "unknown"})

    def do_POST(self):
        if not self._allowed():
            return self._json(403, {"error": "not allowed"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        m = re.fullmatch(r"/abort/(\d+)", self.path)
        if m and self._sig_ok(f"abort:{m.group(1)}", body.get("sig")):
            st = RUNS.get(int(m.group(1)))
            if st: st["abort"] = True
            return self._json(200 if st else 404, {"ok": bool(st)})
        if self.path != "/run":
            return self._json(404, {"error": "unknown"})
        mission = body.get("mission") or {}
        if not self._sig_ok(canonical(mission), body.get("sig")):
            log("refused: bad signature"); return self._json(403, {"error": "the mission is not signed with the owner's approval"})
        if hashlib.sha256(canonical({k: mission[k] for k in ("id", "goal", "steps")}).encode()).hexdigest() != mission.get("hash"):
            return self._json(400, {"error": "the mission text does not match what was approved"})
        if os.path.exists(KILL):
            return self._json(409, {"error": "the runner is switched off (kill switch)"})
        probs = validate(mission)
        if probs:
            log(f"refused mission {mission.get('id')}: {probs[0]}"); return self._json(422, {"error": "refused", "problems": probs})
        mid = int(mission["id"])
        with LOCK:
            if any(r["state"] == "running" for r in RUNS.values()):
                return self._json(409, {"error": "another mission is running"})
            if mid in RUNS or os.path.exists(os.path.join(STATE_DIR, f"mission-{mid}.json")):
                return self._json(409, {"error": "this mission already ran"})
            RUNS[mid] = {"id": mid, "hash": mission["hash"], "state": "queued", "started": time.time(), "steps": [], "rollback": [], "restore_points": []}
        log(f"mission {mid}: starting ({len(mission['steps'])} steps) - {mission.get('goal', '')[:80]}")
        threading.Thread(target=run_mission, args=(mission,), daemon=True).start()
        return self._json(202, {"ok": True, "id": mid})


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit("RUNNER_SECRET is not set (see /etc/prxy-runner.env)")
    os.makedirs(STATE_DIR, exist_ok=True)
    log(f"prxy-runner on {BIND}:{PORT}, allowing {sorted(ALLOW)}, containers {sorted(TARGETS)}")
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
