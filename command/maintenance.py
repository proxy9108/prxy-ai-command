"""Maintenance windows (change awareness). The owner says 'I am working on 10.20.40.131 until 12:00'; while a window is active, findings and signals
that match it are held back instead of raised, so routine work does not teach anybody to ignore the dashboard. Nothing is hidden for good:
a window ends by itself (plus a short grace period for things to settle), and every held-back finding is counted on the window and in the audit log."""
import re, time
from .entities import entities_of, alias_map, norm_scope

GRACE_S = 600


def active(store, now=None):
    now = now or time.time()
    return [w for w in store.maint_list(False, 0) if not w["cancelled"] and w["start"] <= now <= w["end"] + GRACE_S]


def match(store, agent, fingerprint, title="", summary="", now=None):
    """-> the first active window that covers this finding/signal, or None."""
    wins = active(store, now)
    if not wins:
        return None
    keys = None
    for w in wins:
        if w["agents"] and agent not in w["agents"]:
            continue
        if w["pattern"]:
            try:
                if not re.search(w["pattern"], f"{fingerprint} {title}", re.I):
                    continue
            except re.error:
                continue
        scope = [k for item in w["scope"] for k in norm_scope(item)]
        if "*" in scope:
            return w
        if keys is None:
            keys = entities_of(fingerprint, title, summary, aliases=alias_map(store))["keys"]
        if keys & set(scope):
            return w
    return None


def validate(d, now=None):
    """Owner input -> (start, end, scope, agents, pattern, note) or raises ValueError."""
    now = now or time.time()
    scope = d.get("scope")
    if not isinstance(scope, list) or not scope or len(scope) > 20 or any(not isinstance(x, str) or not x.strip() or len(x) > 60 for x in scope):
        raise ValueError("scope must be a list of 1-20 entries: an address, a guest like lxc/131, 'vlan 50', a storage name, or *")
    if any(not norm_scope(x) for x in scope):
        raise ValueError("a scope entry is not understood (use an address, lxc/131, vlan 50, a storage name or *)")
    try:
        minutes = float(d.get("minutes", 60)); delay = float(d.get("start_in_min", 0))
    except (TypeError, ValueError):
        raise ValueError("minutes and start_in_min must be numbers")
    if not 5 <= minutes <= 72 * 60:
        raise ValueError("a window lasts 5 minutes to 72 hours")
    if not 0 <= delay <= 7 * 24 * 60:
        raise ValueError("start_in_min must be 0 to 7 days")
    agents = d.get("agents") or []
    if not isinstance(agents, list) or any(not isinstance(a, str) for a in agents):
        raise ValueError("agents must be a list of agent names (empty = all)")
    pattern = str(d.get("pattern") or "")[:80]
    if pattern:
        try:
            re.compile(pattern)
        except re.error:
            raise ValueError("pattern is not a valid regular expression")
    start = now + delay * 60
    return start, start + minutes * 60, [x.strip() for x in scope], agents, pattern, str(d.get("note") or "")[:300]
