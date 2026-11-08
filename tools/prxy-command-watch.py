#!/usr/bin/env python3
"""Outside watcher for Prxy-AI Command. Runs on the Proxmox host (not inside the container it watches) every two minutes from a systemd timer.
It reads the counts-only health feed; if the system does not answer, or says it is unhealthy (scheduler stalled, a critical agent far behind), twice in
a row, it posts to Discord; it posts again every 6 hours while the problem lasts, and once more when it recovers. It holds only the webhook address
(/etc/prxy-command-watch.env, root only) and writes nothing but its small state file."""
import json, os, ssl, sys, time, urllib.request

URL = os.getenv("WATCH_URL", "https://10.20.40.139:8320/feed/health.json")
STATE = os.getenv("STATE_DIRECTORY", "/var/lib/prxy-command-watch") + "/state.json"
HOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
NEED_BAD, REMIND_S = 2, 6 * 3600


def check():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE      # self-signed certificate on the LAN
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "prxy-command-watch"}), timeout=8, context=ctx) as r:
            d = json.load(r)
    except Exception as e:
        return False, f"no answer from the dashboard ({type(e).__name__}: {str(e)[:80]})"
    if d.get("ok"):
        return True, "ok"
    why = []
    if d.get("loop_age_s", 0) >= 90: why.append(f"the scheduler has not ticked for {int(d['loop_age_s'])} s")
    if d.get("late_critical"): why.append(f"{d['late_critical']} critical agent(s) far behind schedule")
    if not d.get("data_ok", True): why.append("its data disk is not writable")
    return False, "; ".join(why) or "it reports itself unhealthy"


def post(title, text, color):
    if not HOOK:
        print("no webhook configured", file=sys.stderr); return
    body = json.dumps({"username": "Prxy Watch", "embeds": [{"title": title, "description": text, "color": color, "footer": {"text": "outside watcher on the Proxmox host"}}],
                       "allowed_mentions": {"parse": ["everyone"] if color == 0xE06A5F else []}, **({"content": "@here"} if color == 0xE06A5F else {})}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(HOOK, data=body, headers={"Content-Type": "application/json", "User-Agent": "prxy-command-watch"}), timeout=10).read()
    except Exception as e:
        print("could not post:", type(e).__name__, file=sys.stderr)


def main():
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {"bad": 0, "since": None, "alerted": None}
    ok, why = check(); now = time.time()
    if ok:
        if st["alerted"]:
            post("Prxy-AI Command is back", f"Healthy again after about {int((now - st['since']) / 60)} minutes.", 0x5FBF7A)
        st = {"bad": 0, "since": None, "alerted": None}
    else:
        st["bad"] += 1; st["since"] = st["since"] or now
        if st["bad"] >= NEED_BAD and (not st["alerted"] or now - st["alerted"] >= REMIND_S):
            post("Prxy-AI Command needs attention" if st["alerted"] is None else "Prxy-AI Command is STILL unhealthy", why[:400] + f"\nFirst noticed {int((now - st['since']) / 60)} minutes ago.", 0xE06A5F)
            st["alerted"] = now
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"))
    print("ok" if ok else f"BAD ({st['bad']}): {why}")


if __name__ == "__main__":
    main()
