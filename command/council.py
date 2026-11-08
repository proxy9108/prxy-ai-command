"""The council's deliberation (multi-agent debate). Used by the Commander (orchestrator and judge) on incidents the Correlator builds.

  Analyst   (speaks for the Correlator): why the combined pattern is worrying.
  Defender  (speaks for the Critic):     the most plausible harmless explanation.
  Judge     (the Commander):             escalate | watch | dismiss.

Round 1 both sides state a stance and a reason. If they already agree, the judge confirms; if not, round 2 is a rebuttal where each side reads
the other's argument. The judge then decides. Plain code sits around the model on purpose:
  * every spoken line is thrown away if it contains a number that is not in the facts handed to the debaters (a rule-written line replaces it, labelled);
  * a stance must be one of three words, otherwise the rules' own stance is used;
  * floors the judge cannot argue away: three or more different agents, or any high member, cannot be 'dismiss'ed, and a recorded configuration change
    on the same machine can cap the incident at 'watch' but never hide it.
The whole transcript is stored on the incident so you can read how the verdict came about."""
import re, time

STANCES = ("escalate", "watch", "dismiss")
NUM = re.compile(r"\d+(?:\.\d+)?")
SEV_ORDER = ["info", "low", "medium", "high", "critical"]


def numbers(t):
    return set(NUM.findall(t or ""))


def parse_stance(text, default):
    m = re.search(r"STANCE:\s*(escalate|watch|dismiss)", text or "", re.I)
    return (m.group(1).lower() if m else default)


def clean_line(text, facts):
    """-> the spoken line without the STANCE tag, or None if it must be discarded."""
    t = re.sub(r"STANCE:\s*\w+\s*[-:.]*", "", (text or ""), flags=re.I).strip().replace("\n", " ")
    t = re.sub(r"\s+", " ", t)
    if len(t) > 80 and not t.endswith((".", "!", "?")) and "." in t:
        t = t[:t.rindex(".") + 1]                      # the reply was cut off by the length limit: keep the complete sentences
    if not 15 < len(t) < 650 or not numbers(t) <= numbers(facts):
        return None
    return t


def floor(incident):
    """The lowest verdict the rules allow. -> 'watch' or 'dismiss'."""
    if incident["agents"] >= 3 or SEV_ORDER.index(incident["max_member"]) >= SEV_ORDER.index("high"):
        return "watch"
    return "dismiss"


def rules_stance(incident, side):
    explained = incident.get("change")
    if side == "analyst":
        return "escalate" if incident["agents"] >= 2 and not explained else "watch"
    return "watch" if not explained else "dismiss"


def rules_line(incident, side):
    if side == "analyst":
        return f"{incident['agents']} different agents report on the same machine within the same hour; together that is stronger than any one of them."
    if incident.get("change"):
        return "A configuration change was recorded on this machine just before, which may explain what the agents saw."
    return "Each signal on its own has an ordinary explanation, so more evidence is needed before treating this as an attack."


async def deliberate(llm, incident, facts, model=None, emit=None):
    """llm(role, messages, model=...) -> text. incident: {'agents': n, 'max_member': sev, 'change': bool|None, 'severity': sev}. Returns the transcript dict."""
    emit = emit or (lambda *a, **k: None)
    rounds, used_model = [], model or "(default)"

    async def speak(role, side, system, user, extra_facts=""):
        stance_default = rules_stance(incident, side)
        try:
            txt = await llm(role, [{"role": "system", "content": system}, {"role": "user", "content": user}])
            stance, line = parse_stance(txt, stance_default), clean_line(txt, facts + extra_facts)
            src = "model" if line else "rules (the model's line contained something not in the facts)"
        except Exception as e:
            stance, line, src = stance_default, None, f"rules (model unavailable: {type(e).__name__})"
        line = line or rules_line(incident, side)
        return {"role": role, "side": side, "stance": stance, "text": line, "source": src}

    sys_a = ("You are the Analyst on a small infrastructure council. Argue the case that the combined signals are a real problem: say what the signals have in common and what would confirm it. "
             "In at most two plain sentences. Start with 'STANCE: escalate' (it needs action now) or 'STANCE: watch' (only worth keeping an eye on). "
             "Use only the facts given; do not add numbers or names that are not in them.")
    sys_d = ("You are the Defender on a small infrastructure council. Your job is to argue the other side: even if the signals look worrying, give the most plausible HARMLESS explanation "
             "(for example an update, a backup, a new app, a restart, a scheduled job) and say what check would settle it. In at most two plain sentences. "
             "Start with 'STANCE: dismiss' (you think it is harmless) or 'STANCE: watch' (you cannot rule it out). Use only the facts given; do not add numbers or names that are not in them.")
    a1 = await speak("correlator", "analyst", sys_a, facts)
    d1 = await speak("critic", "defender", sys_d, facts)
    rounds.append({"n": 1, "title": "Opening positions", "turns": [a1, d1]})
    for t in (a1, d1):
        emit(t["role"], t["stance"], t["text"])
    consensus = a1["stance"] == d1["stance"]
    if not consensus:
        a2 = await speak("correlator", "analyst", sys_a + " You have heard the Defender; answer them directly.", f"{facts}\nDefender said: {d1['text']}", d1["text"])
        d2 = await speak("critic", "defender", sys_d + " You have heard the Analyst; answer them directly.", f"{facts}\nAnalyst said: {a1['text']}", a1["text"])
        rounds.append({"n": 2, "title": "Rebuttals", "turns": [a2, d2]})
        for t in (a2, d2):
            emit(t["role"], t["stance"], t["text"])
        last_a, last_d = a2["stance"], d2["stance"]
        said = f"Analyst: {a2['text']}\nDefender: {d2['text']}"
    else:
        last_a, last_d, said = a1["stance"], d1["stance"], f"Analyst: {a1['text']}\nDefender: {d1['text']}"
    # the judge: agreement stands unless a floor forbids it; otherwise the model weighs both, inside the same floors
    lo = floor(incident)
    if consensus:
        verdict, jsrc, jline = last_a, "consensus of both sides", f"Both sides agree: {last_a}."
    else:
        verdict, jline, jsrc = None, None, "model"
        sys_j = ("You are the Judge of a small infrastructure council. Read both arguments and decide. Reply with 'STANCE: escalate', 'STANCE: watch' or 'STANCE: dismiss', then one plain sentence of reason. "
                 "Use only the facts and arguments given; do not add numbers or names.")
        try:
            txt = await llm("commander", [{"role": "system", "content": sys_j}, {"role": "user", "content": f"{facts}\n{said}"}])
            verdict = parse_stance(txt, None)
            jline = clean_line(txt, facts + said)
        except Exception:
            pass
        if verdict is None:
            verdict, jsrc = ("escalate" if last_a == "escalate" and incident["agents"] >= 3 else "watch"), "rules (the judge's answer was unusable)"
        jline = jline or f"Decision by the rules: {verdict}."
    if verdict == "dismiss" and lo == "watch":
        verdict, jsrc = "watch", jsrc + "; the rules do not allow dismissing this"
    if incident.get("change") and verdict == "escalate" and incident["agents"] < 3 and SEV_ORDER.index(incident["max_member"]) < SEV_ORDER.index("high"):
        verdict, jsrc = "watch", jsrc + "; a recorded configuration change explains part of it"
    rounds.append({"n": len(rounds) + 1, "title": "Verdict", "turns": [{"role": "commander", "side": "judge", "stance": verdict, "text": jline, "source": jsrc}]})
    emit("commander", verdict, jline)
    return {"rounds": rounds, "verdict": verdict, "consensus": consensus, "model": used_model, "at": time.time()}


def apply_verdict(computed, verdict):
    """The incident's severity after the debate: escalate keeps the computed level, watch caps it at medium, dismiss drops it to low."""
    if verdict == "dismiss":
        return "low"
    if verdict == "watch":
        return computed if SEV_ORDER.index(computed) <= SEV_ORDER.index("medium") else "medium"
    return computed
