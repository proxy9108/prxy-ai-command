"""Backlog Keeper: turns the session handoff documents into a task list and says what is stuck.
It reads the handoff files that are copied into this system's docs folder (tools/sync-docs.sh does that on every deploy). Inside sections whose
heading says pending / next / open questions / needs owner / backlog it takes every list item as a task, plus every unchecked '- [ ]' box anywhere.
A task stays open while it is in the document; when a later version of the document no longer lists it (or ticks it, or strikes it through) it counts as done.
An open task older than a week is 'stuck'. It reads files only; it never edits a document."""
import hashlib, os, re, time
from pathlib import Path
from ..runner import Agent

DOCS = Path(os.getenv("DOCS_DIR", "/var/lib/prxy-command/docs"))
SECTION = re.compile(r"^\W*(\d+[.)]?\s*)?(pending|to-?do|next( steps?| up)?|backlog|open questions?|needs (the )?owner|decisions? (needed|pending)|still to do|ideas)\b", re.I)
NOT_SECTION = re.compile(r"\blog\b|history|changelog|delivered|status|built", re.I)           # a session LOG that mentions 'next' is not a task list
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)")
BOX = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.*\S)")
STUCK_DAYS = 7


def clean(t):
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()[:220]


def extract(text, doc):
    """Markdown -> [{'id','section','text','done'}]."""
    items, section, level, in_task_section = [], "", 9, False
    for line in text.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            lv = len(h.group(1))
            if in_task_section and lv <= level:
                in_task_section = False
            if SECTION.search(h.group(2)) and not NOT_SECTION.search(h.group(2)) and not in_task_section:
                in_task_section, level = True, lv
            section = clean(h.group(2))
            continue
        b = BOX.match(line)
        if b:
            t = clean(b.group(2))
            items.append({"id": hashlib.sha1((doc + t.lower()).encode()).hexdigest()[:12], "section": section, "text": t, "done": b.group(1) != " "})
            continue
        m = ITEM.match(line)
        if m and in_task_section:
            raw = m.group(1)
            done = raw.startswith("~~") or bool(re.match(r"(?i)\*{0,2}done\b", raw)) or "**DONE" in raw
            t = clean(re.sub(r"(?i)^\*{0,2}done\*{0,2}\s*[:\-]?\s*", "", raw.replace("~~", "")))
            if len(t) >= 12:
                items.append({"id": hashlib.sha1((doc + t.lower()).encode()).hexdigest()[:12], "section": section, "text": t, "done": done})
    return items


class BacklogKeeper(Agent):
    name = "backlog_keeper"
    group = "personal"
    title = "Backlog Keeper"
    autonomy = "P"
    at = ["07:30"]
    auto_resolve = True

    async def run(self, ctx):
        st = ctx.runner.store
        files = sorted(DOCS.glob("*.md")) if DOCS.exists() else []
        if not files:
            ctx.finding("backlog:no-docs", "low", "Backlog Keeper has no handoff documents to read", f"The docs folder {DOCS} is empty. Copy the handoff files in (the deploy script does it automatically).", [{"type": "config", "ref": str(DOCS), "excerpt": "0 .md files"}], confidence=1.0)
            ctx.no_data("no documents")
            return
        new = closed = total = 0
        for f in files:
            ctx.working("reading", f.name)
            items = extract(f.read_text(encoding="utf-8", errors="replace"), f.name)
            n, c = st.tasks_sync(f.name, items)
            new, closed, total = new + n, closed + c, total + len(items)
        now = time.time()
        open_t = st.tasks_list("open")
        stuck = [t for t in open_t if now - t["first_seen"] > STUCK_DAYS * 86400]
        if stuck:
            ctx.finding("backlog:stuck", "low", f"{len(stuck)} task(s) have been open for over {STUCK_DAYS} days", "These items have stayed in the handoff documents for more than a week: " + "; ".join(f"{t['text'][:70]} ({t['doc']})" for t in stuck[:5]) + (" ..." if len(stuck) > 5 else ""),
                        [{"type": "document", "ref": t["doc"] + " > " + t["section"], "excerpt": t["text"]} for t in stuck[:6]], confidence=0.8)
        ctx.note(f"{len(files)} document(s), {total} task line(s): {len(open_t)} open, {new} new, {closed} closed, {len(stuck)} stuck")

    SKILLS = {"tasks": {"about": "what the handoff documents already list as open or done about a topic (earlier plans and decisions)", "args": {"query": "words to look for"}}}

    async def skill_tasks(self, ctx, args):
        words = [w for w in re.findall(r"[a-z0-9]{3,}", str(args.get("query", "")).lower()) if w not in ("the", "and", "for", "why", "what", "how", "are", "our", "with", "from")][:6]
        rows = ctx.runner.store.tasks_list(None, 500)
        hit = [t for t in rows if words and sum(w in t["text"].lower() for w in words) >= min(2, len(words))]
        if not hit:
            return {"summary": "The handoff documents list nothing about this.", "facts": [], "followups": [], "recommend": [], "data": {}}
        now = time.time()
        facts = [f"{t['doc'].replace('.md', '')} / {t['section']}: {t['text'][:160]} ({t['status']}, first listed {int((now - t['first_seen']) / 86400)} day(s) ago)" for t in hit[:6]]
        return {"summary": f"{len(hit)} related item(s) in the handoff documents.", "facts": facts, "followups": [], "recommend": [], "data": {}}
