"""What the team is allowed to DO, as data: a short list of named, reversible actions with typed parameters. Anything not on this list cannot be requested,
by an agent, by a model or by the API. Pure functions, no network: the executor (actions.py) and the validators that guard proposals both use this."""
import re, time

ACTIONS = {
    "guest_start": {
        "title": "Start a stopped guest", "risk": "low", "reversible": "yes: it can be shut down again",
        "params": {"vmid": "int", "kind": "lxc|qemu"},
        "privileges": ["VM.PowerMgmt on the guest (path /vms/<id>)"],
    },
    "lxc_mount_backup_flag": {
        "title": "Turn the 'Backup' flag of a container mount point on or off", "risk": "medium", "reversible": "yes: the old setting is restored",
        "params": {"vmid": "int", "mp": "mp0, mp1, ...", "backup": "0|1"},
        "privileges": ["VM.Config.Disk on the guest (path /vms/<id>)"],
    },
    "backup_job_exclude": {
        "title": "Exclude a guest from the backup job (or put it back)", "risk": "medium", "reversible": "yes: the old exclude list is restored",
        "params": {"vmid": "int", "exclude": "true|false", "job": "optional job id"},
        "privileges": ["Sys.Modify on / (broad: Proxmox requires it to edit a backup job; only grant it if you accept that)"],
    },
    # ---- added in 1.4: the jobs the crew most often asks you to do by hand
    "guest_backup_now": {
        "title": "Back up a guest now", "risk": "low", "reversible": "nothing to undo: it only adds a backup archive (the job's keep-last rule prunes old ones)",
        "params": {"vmid": "int", "kind": "lxc|qemu"},
        "privileges": ["VM.Backup on the guest (/vms/<id>)", "Datastore.AllocateSpace and Datastore.Audit on the backup storage (/storage/backups)"],
    },
    "guest_snapshot": {
        "title": "Take a snapshot of a guest (a restore point before risky work)", "risk": "low", "reversible": "yes: the snapshot is deleted again",
        "params": {"vmid": "int", "kind": "lxc|qemu", "name": "optional snapshot name"},
        "privileges": ["VM.Snapshot on the guest (/vms/<id>)"],
    },
    "guest_reboot": {
        "title": "Reboot a guest", "risk": "medium", "reversible": "not needed: it comes back with the same settings (down for about a minute)",
        "params": {"vmid": "int", "kind": "lxc|qemu"},
        "privileges": ["VM.PowerMgmt on the guest (/vms/<id>)"],
    },
    "guest_shutdown": {
        "title": "Shut a guest down", "risk": "medium", "reversible": "yes: it is started again",
        "params": {"vmid": "int", "kind": "lxc|qemu"},
        "privileges": ["VM.PowerMgmt on the guest (/vms/<id>)"],
    },
    "backup_job_exclude_path": {
        "title": "Skip a folder in the nightly backup (or stop skipping it)", "risk": "medium", "reversible": "yes: the old list of skipped folders is restored",
        "params": {"path": "absolute folder inside the guests, e.g. /usr/share/ollama/.ollama/models", "exclude": "true|false", "job": "optional job id"},
        "privileges": ["Sys.Modify on / (broad: Proxmox requires it to edit a backup job; only grant it if you accept that)"],
    },
}
GUEST_ACTIONS = ("guest_start", "guest_backup_now", "guest_snapshot", "guest_reboot", "guest_shutdown")
SELF_VMID = 139                     # Prxy-AI Command's own container: it never reboots or shuts itself down


def validate_params(action, params):
    """-> cleaned params, or raises ValueError with a sentence the owner can read."""
    if action not in ACTIONS:
        raise ValueError(f"'{action}' is not an action the team may carry out")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    p = {}
    if action == "backup_job_exclude_path":
        path = params.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", path) or ".." in path or path.rstrip("/") in ("", "/"):
            raise ValueError("path must be an absolute folder like /usr/share/ollama/.ollama/models (letters, digits, . _ - / only)")
        if not isinstance(params.get("exclude"), bool):
            raise ValueError("exclude must be true or false")
        p["path"], p["exclude"] = path.rstrip("/"), params["exclude"]          # tar ignores an exclusion with a trailing slash
        if params.get("job"):
            if not isinstance(params["job"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", params["job"]):
                raise ValueError("job must be a backup job id")
            p["job"] = params["job"]
        return p
    vmid = params.get("vmid")
    if isinstance(vmid, bool) or not isinstance(vmid, int) or not 100 <= vmid <= 999999:
        raise ValueError("vmid must be a whole number (100 or more)")
    p["vmid"] = vmid
    if action in GUEST_ACTIONS and action != "guest_start":
        if params.get("kind") not in ("lxc", "qemu"):
            raise ValueError("kind must be lxc or qemu")
        p["kind"] = params["kind"]
        if action in ("guest_reboot", "guest_shutdown") and vmid == SELF_VMID:
            raise ValueError("Prxy-AI Command will not reboot or shut down its own container")
        if action == "guest_snapshot":
            nm = params.get("name") or time.strftime("prxy-%Y%m%d-%H%M")
            if not isinstance(nm, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,39}", nm):
                raise ValueError("name must start with a letter and use letters, digits, _ or - (2 to 40 characters)")
            p["name"] = nm
        return p
    if action == "guest_start":
        if params.get("kind") not in ("lxc", "qemu"):
            raise ValueError("kind must be lxc or qemu")
        p["kind"] = params["kind"]
    elif action == "lxc_mount_backup_flag":
        if not isinstance(params.get("mp"), str) or not re.fullmatch(r"mp\d{1,2}", params["mp"]):
            raise ValueError("mp must look like mp0")
        if params.get("backup") not in (0, 1) or isinstance(params.get("backup"), bool):
            raise ValueError("backup must be 0 or 1")
        p["mp"], p["backup"] = params["mp"], params["backup"]
    else:
        if not isinstance(params.get("exclude"), bool):
            raise ValueError("exclude must be true or false")
        p["exclude"] = params["exclude"]
        if params.get("job"):
            if not isinstance(params["job"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", params["job"]):
                raise ValueError("job must be a backup job id")
            p["job"] = params["job"]
    return p


def check_execute(ex):
    """For proposals: {'action', 'params'} -> cleaned dict or raises ValueError."""
    if not isinstance(ex, dict) or set(ex) - {"action", "params"}:
        raise ValueError("execute must be {action, params}")
    return {"action": ex.get("action"), "params": validate_params(ex.get("action"), ex.get("params"))}


def describe(action, params):
    n = params.get("vmid")
    if action == "guest_backup_now":
        return f"back up {params.get('kind')}/{n} now"
    if action == "guest_snapshot":
        return f"take snapshot '{params.get('name')}' of {params.get('kind')}/{n}"
    if action == "guest_reboot":
        return f"reboot {params.get('kind')}/{n}"
    if action == "guest_shutdown":
        return f"shut down {params.get('kind')}/{n}"
    if action == "backup_job_exclude_path":
        return f"{'skip' if params.get('exclude') else 'stop skipping'} {params.get('path')} in the nightly backup"
    if action == "guest_start":
        return f"start {params.get('kind')}/{n}"
    if action == "lxc_mount_backup_flag":
        return f"set Backup={'on' if params.get('backup') else 'off'} on {params.get('mp')} of lxc/{n}"
    return f"{'exclude' if params.get('exclude') else 'include'} guest {n} {'from' if params.get('exclude') else 'in'} the backup job"
