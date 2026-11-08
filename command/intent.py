"""What did the boss mean? Reads one break-room message and returns a plain intent the crew can act on.

  update       "I moved Kali to VLAN 60", "I deleted Loki's logs on purpose"      -> a note for the shared memory + matching findings to close (owner clicks)
  instruction  "look into the backups", "research Proxmox 9.2", "stop warning me about MQTT"  -> a project, a research question, or findings to silence (owner clicks)
  question     "is the backup ok?", "why is Compass so busy?"                    -> the right crew member answers from real data
  chat         anything else                                                      -> the Chief answers with the totals

Rules first (fast, predictable); the local model is asked only when the rules can't place the message. The model's answer is validated:
it can only pick one of the four kinds, and the topic it returns must be words from the message. Nothing here changes anything."""
import json, re

UPDATE = re.compile(r"\b(?:i|we)(?:'ve| have| just)?\s+(?:moved|changed|deleted|removed|installed|uninstalled|upgraded|updated|rebooted|restarted|added|replaced|renamed|"
                    r"set up|setup|turned (?:on|off)|switched|migrated|reinstalled|wiped|cleaned|fixed|patched|disabled|enabled|created|rebuilt|resized|swapped)\b|"
                    r"\b(?:on purpose|intentional(?:ly)?|that was me|it was me|i did that|expected|by design|that's fine|that is fine|ignore (?:it|that|this))\b", re.I)
SILENCE = re.compile(r"\b(?:stop (?:warning|telling|alerting|nagging|reporting)(?: me)?(?: about)?|don'?t (?:warn|tell|alert) me(?: about)?|"
                     r"(?:it'?s|that'?s|this is) not a problem|mute|silence|ignore)\b\s*(.*)", re.I)
RESEARCH = re.compile(r"\b(?:research|look up|find out online|search (?:the )?(?:web|internet|online) for|google)\b[:,]?\s*(.*)", re.I)
INVESTIGATE = re.compile(r"\b(?:look into|investigate|dig into|find out why|figure out why|check why|work out why|can you check|please check)\b[:,]?\s*(.*)", re.I)
MISSION = re.compile(r"^\s*(?:please\s+|can you\s+|could you\s+|go\s+(?:and\s+)?|let'?s\s+)?(?:plan a mission (?:to\s+)?|mission:\s*)?"
                     r"(fix|repair|set up|setup|install|configure|upgrade|update|patch|lock down|secure|harden|enable|disable|clean up|tidy up|make sure|get .+ working|move|migrate|rotate)\b(.*)", re.I)
QUESTION = re.compile(r"\?\s*$|^\s*(?:what|why|how|is|are|was|were|did|does|do|can|could|should|when|where|which|who)\b", re.I)


def words(t):
    return set(re.findall(r"[a-z0-9][a-z0-9._/-]{2,}", (t or "").lower()))


def by_rules(text):
    t = text.strip()
    m = SILENCE.search(t)
    if m and not re.search(r"\?\s*$", t):
        return {"kind": "instruction", "action": "silence", "topic": (m.group(1) or t).strip(" .!")}
    m = RESEARCH.search(t)
    if m and len((m.group(1) or "").strip()) >= 6:
        return {"kind": "instruction", "action": "research", "topic": m.group(1).strip(" .!")}
    m = INVESTIGATE.search(t)
    if m and len((m.group(1) or "").strip()) >= 6:
        return {"kind": "instruction", "action": "investigate", "topic": m.group(1).strip(" .!")}
    if UPDATE.search(t) and not re.search(r"\?\s*$", t):
        return {"kind": "update", "action": None, "topic": t}
    m = MISSION.search(t)
    if m and len(t) >= 12 and not re.match(r"^\s*(how|what|why|which|is|are|does)\b", t, re.I):
        return {"kind": "instruction", "action": "mission", "topic": t.strip(" .!?")}
    if QUESTION.search(t):
        return {"kind": "question", "action": None, "topic": t}
    return None


async def classify(text, llm=None):
    """-> {'kind', 'action', 'topic', 'by'}. `llm(messages) -> {'text'}` is the local model (optional)."""
    r = by_rules(text)
    if r:
        return {**r, "by": "rules"}
    if llm:
        try:
            out = await llm([{"role": "system", "content": "Classify the owner's message to their home-lab assistant crew. Reply with ONE JSON object only: "
                                                            "{\"kind\": \"update\" | \"instruction\" | \"question\" | \"chat\", \"action\": \"investigate\" | \"research\" | \"silence\" | null, "
                                                            "\"topic\": \"the few words from the message it is about\"}. update = the owner tells what they changed or that something is "
                                                            "expected; instruction = asks the crew to do something; question = asks for information."},
                             {"role": "user", "content": text}])
            m = re.search(r"\{.*\}", out.get("text", ""), re.S)
            j = json.loads(m.group(0)) if m else {}
            kind = j.get("kind") if j.get("kind") in ("update", "instruction", "question", "chat") else None
            action = j.get("action") if j.get("action") in ("investigate", "research", "silence", "mission") else None
            topic = str(j.get("topic") or "").strip()
            if kind and (not topic or words(topic) <= words(text) | {"the", "and"}):             # the topic must come from the message
                if kind == "instruction" and not action:
                    kind = "question"
                return {"kind": kind, "action": action if kind == "instruction" else None, "topic": topic or text, "by": "model"}
        except Exception:
            pass
    return {"kind": "chat", "action": None, "topic": text, "by": "fallback"}


def match_findings(text, findings, targets, limit=25):
    """Open findings that an owner's message is about: same guest/host/storage, or clearly shared words in the title."""
    hosts, guests, storages = targets
    gids = {g for g, _ in guests}
    names = {n for _, n in guests if n}
    tw = words(text) - {"the", "and", "was", "that", "this", "with", "from", "because", "purpose", "expected", "moved", "changed", "stop", "warning", "about",
                        "tell", "telling", "alert", "please", "ignore", "it's", "that's", "not", "problem", "anymore", "any", "more", "them", "they", "have", "just"}
    out = []
    for f in findings:
        if f["status"] not in ("new", "seen"):
            continue
        hay = f"{f['fingerprint']} {f['title']} {f['summary']}".lower()
        hit = any(g in hay for g in gids) or any(re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", hay) for n in names) \
            or any(h in hay for h in hosts) or any(s_ in hay for s_ in storages)
        if not hit and tw:
            shared = tw & words(f["title"])
            hit = len(shared) >= 2 or any(len(w) >= 4 and w in shared for w in tw)
        if hit:
            out.append(f)
    return out[:limit]
