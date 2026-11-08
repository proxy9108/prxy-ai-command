"""The full team (21 characters + the demo intern). Built agents are live; the rest show in the Office as 'not hired yet'
until their roadmap session ships. `look` drives the pixel sprite (skin, hair, hair style, shirt, accessory)."""

ROSTER = [
    # name, title, group, session, look
    ("sentinel", "Sentinel", "security", "S4", dict(skin="#e8b98a", hair="#2b2b3a", style="short", shirt="#b3261e", acc="headset")),
    ("dns_watcher", "DNS Watcher", "security", "S10", dict(skin="#c68b59", hair="#1b1b1b", style="long", shirt="#8e3b8e", acc="glasses")),
    ("exposure_auditor", "Exposure Auditor", "security", "S10", dict(skin="#f1c9a5", hair="#7a4b1e", style="spiky", shirt="#9c4f1a", acc="none")),
    ("iot_watcher", "IoT Watcher", "security", "S10", dict(skin="#8d5a3b", hair="#222222", style="short", shirt="#2a7f9e", acc="cap")),
    ("ids_doctor", "IDS Doctor", "security", "S10", dict(skin="#e8b98a", hair="#c9c9d6", style="bun", shirt="#e8e8e8", acc="glasses")),
    ("alert_triage", "Alert Triage", "operations", "S3", dict(skin="#f1c9a5", hair="#a33b20", style="short", shirt="#d9a31c", acc="headset")),
    ("backup_warden", "Backup Warden", "operations", "S5", dict(skin="#c68b59", hair="#3a2a1a", style="bun", shirt="#3f7f4f", acc="none")),
    ("patch_scout", "Patch Scout", "operations", "S6", dict(skin="#e8b98a", hair="#1b1b1b", style="spiky", shirt="#3a5fa8", acc="cap")),
    ("capacity_planner", "Capacity Planner", "operations", "S5", dict(skin="#8d5a3b", hair="#111111", style="long", shirt="#5a7d9a", acc="glasses")),
    ("config_drift", "Config Drift", "operations", "S6", dict(skin="#f1c9a5", hair="#6d6d78", style="short", shirt="#6a6a8a", acc="none")),
    ("day_briefing", "Day Briefing", "personal", "S11", dict(skin="#e8b98a", hair="#d9b44a", style="long", shirt="#e08a2e", acc="none")),
    ("research_librarian", "Research Librarian", "personal", "S12", dict(skin="#c68b59", hair="#2b1b12", style="bun", shirt="#7a4b8f", acc="glasses")),
    ("job_scout", "Job and Career Scout", "personal", "S15", dict(skin="#f1c9a5", hair="#1b1b1b", style="short", shirt="#2e6f95", acc="tie")),
    ("backlog_keeper", "Backlog Keeper", "personal", "S13", dict(skin="#8d5a3b", hair="#1b1b1b", style="spiky", shirt="#4a8f5a", acc="none")),
    ("media_curator", "Media Curator", "personal", "S14", dict(skin="#e8b98a", hair="#b03a6a", style="long", shirt="#b03a6a", acc="headset")),
    ("briefing_editor", "Briefing Editor", "housekeeping", "S11", dict(skin="#c68b59", hair="#222222", style="short", shirt="#c9573a", acc="glasses")),
    ("commander", "Commander", "council", "S7", dict(skin="#e8b98a", hair="#2a2a2a", style="short", shirt="#1c2b4a", acc="tie")),
    ("critic", "The Critic", "council", "S9", dict(skin="#f1c9a5", hair="#8a1c1c", style="spiky", shirt="#3a1c1c", acc="glasses")),
    ("archivist", "The Archivist", "council", "S7", dict(skin="#c68b59", hair="#d8d8d8", style="bun", shirt="#4a3a22", acc="glasses")),
    ("correlator", "The Correlator", "council", "S17", dict(skin="#8d5a3b", hair="#e0c9a0", style="bun", shirt="#2e5f5a", acc="glasses")),
    ("watchdog", "Watchdog", "housekeeping", "S17", dict(skin="#e8b98a", hair="#4a4a52", style="spiky", shirt="#7a3a1a", acc="headset")),
    ("demo", "Demo Intern", "test", "S1", dict(skin="#f1c9a5", hair="#5a8a3a", style="short", shirt="#9a9a94", acc="cap")),
]


JOBS = {
    "sentinel": "Reviews firewall, switch and IDS logs: scans, new outbound destinations, failed logins.",
    "dns_watcher": "Spots odd DNS: DGA-like names, tunnelling volume, a device suddenly querying many new domains.",
    "exposure_auditor": "Weekly check of open ports and reachable services on our own VLANs against the expected list.",
    "iot_watcher": "Baselines the TV, UPS and other IoT devices and flags new destinations.",
    "ids_doctor": "Checks that Suricata really inspects traffic and tracks the fix.",
    "alert_triage": "There are no Prometheus alert rules: watches the metrics, decides what is abnormal, writes short alerts.",
    "backup_warden": "Confirms the nightly backup ran and is restorable; reminds about the missing off-disk copy.",
    "patch_scout": "Ranks pending updates by security relevance and writes the plan with rollback steps.",
    "capacity_planner": "Predicts when disks fill (Ollama disk, goflow2, the thin pool).",
    "config_drift": "Compares the real setup to homelab-reference.md and reports drift.",
    "day_briefing": "Morning brief: weather, calendar, lab status and news.",
    "research_librarian": "Tracks topics through SearXNG and files sourced summaries into Wiki.js.",
    "job_scout": "Finds matching roles, tailors resume drafts, tracks applications. Never submits by itself.",
    "backlog_keeper": "Turns each session handoff into tasks and reports what is stuck.",
    "media_curator": "Recommends shows for the TV launcher.",
    "briefing_editor": "Edits the TV Prxy Briefing: adds lab status (counts only) and checks the script against its sources.",
    "commander": "Reads every finding, groups duplicates, ranks them, writes the daily briefing, assigns follow-ups.",
    "critic": "Argues the other side on proposed actions and critical findings before they reach you.",
    "archivist": "Shared memory: history, decisions you made, baselines.",
    "correlator": "Joins what different agents say about the same machine inside the same hour into one incident, and links its members.",
    "watchdog": "Checks that every agent, the model, the notifier and this server itself are still working; the watcher of the watchers.",
    "demo": "Example agent that proves the pipeline end to end.",
}


def roster(runner):
    built = {a["name"]: a for a in runner.describe()}
    out = []
    for name, title, group, session, look in ROSTER:
        b = built.get(name)
        out.append({"name": name, "title": title, "group": group, "session": session, "look": look, "built": b is not None,
                    "job": JOBS.get(name, ""), "state": b["state"] if b else "planned", "enabled": b["enabled"] if b else False,
                    "open_findings": b["open_findings"] if b else {}, "last_summary": b["last_summary"] if b else "",
                    "last_status": b["last_status"] if b else None, "quality": b["quality"] if b else None})
    for name, b in built.items():                      # agents not in the roster still appear (as plain guests)
        if name not in {r[0] for r in ROSTER}:
            out.append({"name": name, "title": b["title"], "group": b["group"], "session": "", "look": dict(skin="#e8b98a", hair="#444444", style="short", shirt="#666666", acc="none"),
                        "job": "", "built": True, "state": b["state"], "enabled": b["enabled"], "open_findings": b["open_findings"], "last_summary": b["last_summary"]})
    return out
