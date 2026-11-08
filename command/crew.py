"""The crew: who reports to whom, and how each agent talks.

A triangle with three tiers:
  1. High Command  - the Commander (the boss's second in command).
  2. Staff + leads - the Council staff (Critic, Archivist, Correlator, Watchdog) and one lead per squad.
  3. Squads        - the workers. They report to their lead; a lead escalates to the Commander.

Personalities only change HOW a line is said, never WHAT it says: every chat line is built from a real event, finding or run
(see chat.py), and the phrase banks below only add a voice around those facts."""
import hashlib

BOSS = "the boss"

COMMAND = "commander"
STAFF = ["critic", "archivist", "correlator", "watchdog"]
ADVISORS = ["claude", "openai", "gemini"]          # outside advisors (hosted models) in High Command: they plan, review and coach; they never act
ADVISOR_LOOKS = {"claude": dict(skin="#e8b98a", hair="#c8c3b8", style="long", shirt="#d97757", acc="glasses"),
                 "openai": dict(skin="#c68b59", hair="#1b1b1b", style="short", shirt="#10a37f", acc="headset"),
                 "gemini": dict(skin="#f1c9a5", hair="#4b6cd9", style="bun", shirt="#4285f4", acc="none")}
ADVISOR_TITLES = {"claude": "Outside advisor (Claude)", "openai": "Outside advisor (OpenAI)", "gemini": "Outside advisor (Gemini)"}

SQUADS = {
    "security": {"name": "Night Watch", "about": "Keeps the network honest: logs, DNS, open doors, IoT, the IDS.",
                 "lead": "sentinel", "members": ["dns_watcher", "exposure_auditor", "iot_watcher", "ids_doctor"], "color": "#e07a6a"},
    "operations": {"name": "Engine Room", "about": "Keeps the lab running: metrics, backups, updates, disks, config.",
                   "lead": "alert_triage", "members": ["backup_warden", "patch_scout", "capacity_planner", "config_drift"], "color": "#7fbf8a"},
    "personal": {"name": "Front Desk", "about": "Looks after the boss: briefings, research, jobs, the backlog, what to watch.",
                 "lead": "day_briefing", "members": ["research_librarian", "job_scout", "backlog_keeper", "media_curator", "briefing_editor"], "color": "#b79ad9"},
}
COMMAND_COLOR = "#ffc857"

# callsign, one-line personality, phrase banks. Keep lines short: they also appear as speech bubbles in the Office.
PERSONAS = {
    "claude": ("Sage", "Careful and thorough; plans the crew's investigations.", {
        "ack": ["Understood.", "Noted."], "status": ["Sage here. {what}"], "report": ["Sage: {what}"]}),
    "openai": ("Atlas", "Practical; checks that fixes really worked.", {
        "ack": ["Got it.", "Noted."], "status": ["Atlas here. {what}"], "report": ["Atlas: {what}"]}),
    "gemini": ("Nova", "Big-picture coach; points the crew at what matters today.", {
        "ack": ["Noted.", "Sounds right."], "status": ["Nova here. {what}"], "report": ["Nova: {what}"]}),
    "commander": ("Chief", "Calm and decisive. Listens to everyone, then makes the call.", {
        "ack": ["Understood.", "Noted. I'll take it from here.", "Good. Carry on."],
        "escalated": ["It goes on the boss's list.", "That one is for the boss. I'll make sure it's seen.", "Flagged for the boss."],
        "fixed": ["Good work.", "One less thing. Thank you.", "Nice. Log it and move on."],
        "nudge": ["It's waiting on the boss. We keep watching.", "Nothing we can do until the boss decides. Keep it fresh.", "We hold until the boss decides."],
        "greet": ["Welcome back, boss.", "Good to see you, boss.", "Boss is back. Everyone, look sharp."],
        "absent": ["We hold the line. The list will be ready when they're back.", "Keep working. I'll have the summary ready.", "No change of plan. We keep watch."],
        "open": ["Morning, team. Stand-up. Leads, go.", "Stand-up time. Short reports, please.", "Let's hear it. Leads first."],
    }),
    "critic": ("Doubt", "Dry skeptic. Asks for proof before anyone acts.", {
        "ack": ["Hm. Show me the evidence.", "Noted. I'll want proof before anyone acts on it.", "Fine. Prove it."],
        "fixed": ["Fixed, or just quiet? We'll see tomorrow.", "Good. Let's make sure it stays fixed.", "I'll believe it after the next run."],
        "nudge": ["Repeating a warning doesn't make it more true. Or less.", "Counting is not deciding.", "Loud is not the same as urgent."],
        "absent": ["Nobody acts without the boss. That's the rule.", "Good. Fewer hasty decisions.", "Then nothing changes. As it should."],
    }),
    "archivist": ("Ledger", "Remembers everything. Speaks in dates and precedents.", {
        "ack": ["For the record: noted.", "Filed.", "Written down. It's in the history now."],
        "fixed": ["Recorded as cleared.", "Filed under 'resolved'.", "Logged. That's history now."],
        "absent": ["For the record: last seen {last_seen}.", "My log says {last_seen}.", "Last entry from the boss: {last_seen}."],
        "nudge": ["The history says this has been open since {since}.", "For the record: open since {since}."],
    }),
    "correlator": ("Thread", "Connects the dots between machines and agents.", {
        "ack": ["Same box, same hour. I'm watching for a pattern.", "Noted. I'll see if anyone else is seeing it.", "I'll tie it to anything related."],
        "fixed": ["Pattern's gone.", "Nothing left to connect.", "Thread cut. Clean."],
        "absent": ["No pattern in that. They come back.", "Nothing to connect yet."],
    }),
    "watchdog": ("Patrol", "Keeps everyone honest. Counts runs, misses, repeats.", {
        "ack": ["Logged. I'll check you ran.", "Noted. I'm watching the clock on that.", "Seen."],
        "nudge": ["{agent_call}, you've flagged “{title}” {count} times. Has anyone answered?", "{agent_call}: “{title}”, {count} reports and counting. Still no decision?",
                  "Heads up: “{title}” has come up {count} times."],
        "absent": ["Anyone seen the boss? No login for {away}.", "Roll call: the boss hasn't checked in for {away}.", "The boss has been away {away}. Anyone heard anything?"],
        "fixed": ["Confirmed.", "Checked. It's back.", "Verified."],
    }),
    "sentinel": ("Hawk", "Terse night guard. Few words, eyes open.", {
        "status": ["Hawk. {what}", "{what}"],
        "report": ["{what}", "Hawk here. {what}", "Report: {what}"],
        "ack": ["Copy.", "Seen.", "Copy. Watching."],
        "fixed": ["Clear.", "Quiet again.", "Clear. Back to watching."],
        "worry": ["Still open.", "Not my call. Watching.", "Waiting on orders."],
        "absent": ["Quiet night. Nothing came in.", "Not since {last_seen}.", "No sign of them. Watching."],
        "escalate": ["Chief. {what}", "Chief, for you: {what}"],
        "ask": ["Status?", "Anything?", "Report."],
    }),
    "dns_watcher": ("Echo", "Chatty and curious. Notices every new domain.", {
        "status": ["Echo here! {what}", "Oh, hi! {what}"],
        "report": ["Ooh, look at this: {what}", "So, um, {what}", "Echo here! {what}"],
        "fixed": ["Oh good, that cleared up!", "Back to normal. Phew."],
        "worry": ["Still can't see it properly...", "I keep asking, nobody answers."],
        "absent": ["Maybe they're on holiday? Lucky.", "I haven't seen them query anything either."],
    }),
    "exposure_auditor": ("Locksmith", "Methodical. Thinks of every open port as a door.", {
        "report": ["Door check: {what}", "Found a door open: {what}", "Locksmith. {what}"],
        "fixed": ["Door's shut.", "Locked."],
        "worry": ["That door is still open.", "Still unlocked."],
    }),
    "iot_watcher": ("Gadget", "Friendly. Protective of the little devices.", {
        "report": ["One of the little ones: {what}", "Gadget here. {what}", "Hey, {what}"],
        "fixed": ["All the little ones are fine.", "Back to normal."],
        "worry": ["Still keeping an eye on it.", "Hmm. Still there."],
        "absent": ["The TV misses them too.", "The devices are quiet without them."],
    }),
    "ids_doctor": ("Doc", "Calm bedside manner. Talks in diagnoses.", {
        "status": ["Doc here. {what}", "Chart says: {what}"],
        "report": ["Diagnosis: {what}", "Doc here. {what}", "Symptoms: {what}"],
        "fixed": ["The patient is stable.", "Recovering nicely.", "Pulse is back."],
        "worry": ["No improvement yet.", "Same symptoms. Treatment pending."],
    }),
    "alert_triage": ("Switch", "Dispatcher. Fast, radio-style, always routing.", {
        "status": ["Switch: {what} Over.", "Dispatch: {what}"],
        "report": ["Switch, over: {what}", "Dispatch: {what}", "{what} Over."],
        "ack": ["Copy, logging it.", "Roger. Routing.", "Copy that."],
        "fixed": ["Copy, cleared. Over.", "Off the board."],
        "worry": ["Still on the board.", "Still holding. Over."],
        "escalate": ["Chief, priority traffic: {what}", "Chief, passing up: {what}"],
        "ask": ["Status check. Over.", "How's it looking?", "Report in."],
        "absent": ["No traffic from the boss since {last_seen}. Over.", "Radio silence from the boss."],
    }),
    "backup_warden": ("Vault", "Worrier. Sleeps badly when a backup fails.", {
        "status": ["Vault here. {what}", "Vault, checking in: {what}"],
        "report": ["Oh no. {what}", "I don't like this: {what}", "Vault here, and I'm worried: {what}"],
        "fixed": ["Oh thank goodness.", "I can breathe again.", "Finally."],
        "worry": ["Please, someone look at this.", "I'll keep reminding. I can't help it.", "It keeps me up at night."],
        "absent": ["What if something breaks while they're gone?", "I hope they come back before the disk fills."],
    }),
    "patch_scout": ("Scout", "Eager. Loves a new version.", {
        "status": ["Scout here: {what}"],
        "report": ["New ones spotted! {what}", "Scout reporting: {what}", "Guess what: {what}"],
        "fixed": ["All caught up!", "Up to date."],
        "worry": ["Still waiting to be installed.", "They're still sitting there."],
    }),
    "capacity_planner": ("Gauge", "Lives in numbers and forecasts.", {
        "report": ["By the numbers: {what}", "Gauge. {what}", "Trend check: {what}"],
        "fixed": ["Back inside the safe range.", "The curve flattened."],
        "worry": ["The numbers haven't moved.", "Still trending the wrong way."],
    }),
    "config_drift": ("Compass", "Pedantic about the baseline. Notices every change.", {
        "report": ["That's not what the baseline says: {what}", "Compass. {what}", "Drift: {what}"],
        "fixed": ["Matches the baseline again.", "Back on course."],
        "worry": ["Still off the baseline.", "Still drifting."],
    }),
    "day_briefing": ("Sunny", "Warm morning person. Leads the Front Desk.", {
        "status": ["Sunny here! {what}", "Hi! {what}"],
        "report": ["Morning! {what}", "Sunny here. {what}", "{what}"],
        "ack": ["Got it, thanks!", "Lovely, noted.", "Thanks!"],
        "fixed": ["Wonderful.", "That's good news."],
        "ask": ["How's everyone doing?", "Anything for the boss?", "All good?"],
        "escalate": ["Chief, one for you: {what}"],
        "absent": ["I left the briefing on the desk for them.", "I'll keep the coffee warm."],
    }),
    "research_librarian": ("Shelf", "Bookish. Always has a source.", {
        "report": ["From the stacks: {what}", "Shelf. {what}"], "fixed": ["Filed away."], "worry": ["Still on the reading list."]}),
    "job_scout": ("Recruit", "Upbeat. Always looking for the next opportunity.", {
        "report": ["Opportunity check: {what}", "Recruit here. {what}"], "fixed": ["Sorted."], "worry": ["Still waiting on that."]}),
    "backlog_keeper": ("Tally", "Gentle nag. Keeps the list.", {
        "report": ["On the list: {what}", "Tally. {what}"], "fixed": ["Ticked off."], "worry": ["It's still on the list.", "Gentle reminder: still open."],
        "absent": ["The list is waiting for them."]}),
    "media_curator": ("Reel", "Talks like a film critic.", {
        "report": ["Now showing: {what}", "Reel. {what}"], "fixed": ["That's a wrap."], "worry": ["Still in the queue."]}),
    "briefing_editor": ("Ink", "Precise editor. Hates a loose fact.", {
        "report": ["Editor's note: {what}", "Ink. {what}"], "fixed": ["Copy approved."], "worry": ["Still unedited."]}),
}

GENERIC = {"status": ["{call} here. {what}", "{what}", "{call}: {what}"], "report": ["{what}"], "ack": ["Noted."], "fixed": ["Cleared."], "worry": ["Still open."], "absent": ["No word from the boss."], "nudge": ["Still open."],
           "escalate": ["Chief: {what}"], "ask": ["Status?"], "greet": ["Welcome back."], "open": ["Stand-up."]}


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def squad_of(name):
    if name == COMMAND or name in STAFF or name in ADVISORS:
        return "command"
    for k, s in SQUADS.items():
        if name == s["lead"] or name in s["members"]:
            return k
    return None


def lead_of(name):
    """Who does this agent report to? Workers -> their squad lead; leads and staff -> the Commander; the Commander -> nobody."""
    if name == COMMAND:
        return None
    sq = squad_of(name)
    if sq in SQUADS and SQUADS[sq]["lead"] != name:
        return SQUADS[sq]["lead"]
    return COMMAND


def tier_of(name):
    if name == COMMAND:
        return 1
    if name in STAFF or name in ADVISORS or any(s["lead"] == name for s in SQUADS.values()):
        return 2
    return 3 if squad_of(name) else None


def callsign(name):
    p = PERSONAS.get(name)
    return p[0] if p else name.replace("_", " ").title()


def personality(name):
    p = PERSONAS.get(name)
    return p[1] if p else ""


def line(name, kind, seed="", **facts):
    """One phrase from the agent's bank, picked reproducibly from `seed` (so the same event gives the same line), facts filled in."""
    bank = (PERSONAS.get(name, (None, None, {}))[2].get(kind)) or GENERIC.get(kind) or ["{what}"]
    i = int(hashlib.sha1(f"{name}:{kind}:{seed}".encode()).hexdigest(), 16) % len(bank)
    facts.setdefault("call", callsign(name))
    return bank[i].format_map(_Safe({k: v for k, v in facts.items() if v is not None})).strip()


def has(name, kind):
    return kind in PERSONAS.get(name, (None, None, {}))[2]


def org(roster, enabled=None):
    """The org chart for the dashboard: tiers, squads, and each member with callsign, personality and live state from `roster`."""
    by = {r["name"]: r for r in roster}

    def card(n):
        r = by.get(n, {})
        return {"name": n, "title": r.get("title", n), "callsign": callsign(n), "personality": personality(n), "tier": tier_of(n), "squad": squad_of(n),
                "reports_to": lead_of(n), "look": r.get("look"), "built": r.get("built", False), "enabled": r.get("enabled", False), "state": r.get("state", "planned"),
                "open_findings": r.get("open_findings", {}), "last_summary": r.get("last_summary", ""), "job": r.get("job", "")}
    def advisor(n):
        return {**card(n), "title": ADVISOR_TITLES[n], "look": ADVISOR_LOOKS[n], "built": True, "enabled": n in (enabled or ()), "state": "idle" if n in (enabled or ()) else "off",
                "job": "Plans, reviews and coaches the local crew. Returns text only; never changes anything."}
    return {"command": card(COMMAND), "staff": [card(n) for n in STAFF], "advisors": [advisor(n) for n in ADVISORS], "command_color": COMMAND_COLOR,
            "squads": [{"key": k, "name": s["name"], "about": s["about"], "color": s["color"], "lead": card(s["lead"]), "members": [card(m) for m in s["members"]]}
                       for k, s in SQUADS.items()]}


def everyone():
    return [COMMAND, *STAFF, *(n for s in SQUADS.values() for n in [s["lead"], *s["members"]])]
