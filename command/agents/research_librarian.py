"""Research Librarian: follows the owner's topics through the private SearXNG instance and writes a short, cited digest for each.
It reads SearXNG's HTML (JSON output is switched off on that instance and this agent does not change it). A digest is only kept when every
sentence cites a numbered source and contains no number that is absent from the snippets; otherwise the plain list of sources is kept
and the digest is left out. Notes live in this system (Research page). Filing them into Wiki.js needs a Wiki.js API token that only the
owner can create (set WIKI_URL and WIKI_TOKEN in /etc/prxy-command/env); without it nothing is written there."""
import asyncio, html, os, re, time
import aiohttp
from ..redact import scrub
from ..runner import Agent
from ..sources import SourceError, http_text

SEARX = os.getenv("SEARXNG_URL", "http://10.20.40.125:8888")
DEFAULT_TOPICS = [
    {"id": "proxmox", "name": "Proxmox VE", "query": "Proxmox VE release security advisory", "every_h": 168},
    {"id": "homelab-sec", "name": "Homelab security", "query": "homelab security vulnerability advisory this week", "every_h": 168},
    {"id": "local-llm", "name": "Local LLMs (Ollama)", "query": "Ollama new model release local LLM", "every_h": 168},
]


def parse_results(page, n=8):
    out = []
    for art in re.findall(r"<article[^>]*class=\"result[^>]*>(.*?)</article>", page, re.S):
        m = re.search(r"<h3>\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", art, re.S)
        if not m:
            continue
        snip = re.search(r"<p class=\"content\"[^>]*>(.*?)</p>", art, re.S)
        clean = lambda t: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t))).strip()
        url = html.unescape(m.group(1))
        if url.startswith("http"):
            out.append({"title": clean(m.group(2))[:160], "url": url, "snippet": clean(snip.group(1))[:320] if snip else ""})
        if len(out) >= n:
            break
    return out


def tidy(text):
    """Drop the chat preamble small models like to add ('Here is a summary ...:')."""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if len(lines) > 1 and (lines[0].rstrip().endswith(":") or re.match(r"(?i)^(here is|here are|sure|certainly)\b", lines[0])):
        lines = lines[1:]
    return " ".join(lines)


def valid_digest(text, sources):
    """Every sentence must cite [n] with n a real source; every number must come from a snippet or title."""
    pool = " ".join(s["title"] + " " + s["snippet"] for s in sources)
    nums = lambda t: set(re.findall(r"\d+(?:\.\d+)?", re.sub(r"\[\d+\]", "", t)))
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    if not 1 <= len(sents) <= 5:
        return False
    for x in sents:
        cites = [int(c) for c in re.findall(r"\[(\d+)\]", x)]
        if not cites or any(not 1 <= c <= len(sources) for c in cites):
            return False
    return nums(text) <= nums(pool)


async def file_to_wiki(topic, doc):
    """Create a page in Wiki.js (GraphQL) under /research/. Only runs when the owner provided WIKI_URL and WIKI_TOKEN."""
    url, tok = os.getenv("WIKI_URL", "").rstrip("/"), os.getenv("WIKI_TOKEN", "")
    if not (url and tok):
        return None
    body = "\n".join([f"# {topic['name']}: {doc['date']}", "", doc.get("digest") or "_No digest (sources only)._", "", "## Sources"] + [f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(doc["sources"], 1)])
    q = "mutation($c:String!,$p:String!,$t:String!){pages{create(content:$c,description:\"Prxy Research Librarian\",editor:\"markdown\",isPublished:true,isPrivate:false,locale:\"en\",path:$p,tags:[\"research\"],title:$t){responseResult{succeeded message}}}}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url + "/graphql", json={"query": q, "variables": {"c": body, "p": f"research/{topic['id']}-{doc['date']}", "t": f"{topic['name']} {doc['date']}"}}, headers={"Authorization": f"Bearer {tok}"}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                d = await r.json()
        res = d["data"]["pages"]["create"]["responseResult"]
        return res["succeeded"], res.get("message", "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class ResearchLibrarian(Agent):
    name = "research_librarian"
    group = "personal"
    title = "Research Librarian"
    autonomy = "P"
    interval_s = 3600

    async def run(self, ctx):
        topics = ctx.kv_get("topics")
        if topics is None:
            topics = DEFAULT_TOPICS
            ctx.kv_set("topics", topics)
        now, done, skipped = time.time(), 0, 0
        for t in topics:
            due = ctx.trigger == "manual" or now - t.get("last", 0) >= t["every_h"] * 3600
            if not due or done >= 5:
                skipped += 1
                continue
            ctx.working("reading", f"searching: {t['name']}")
            try:
                page = await http_text(f"{SEARX}/search?" + "q=" + re.sub(r"\s+", "+", t["query"]) + "&time_range=month", 25)
            except SourceError as e:
                ctx.finding("source:searxng", "low", "Research Librarian cannot reach SearXNG", str(e), [{"type": "error", "ref": SEARX, "excerpt": str(e)[:300]}], confidence=1.0)
                break
            sources = parse_results(page)
            if not sources:
                continue
            ctx.working("thinking", f"digest: {t['name']}")
            digest = None
            numbered = "\n".join(f"[{i}] {s['title']}: {s['snippet']}" for i, s in enumerate(sources, 1))
            try:
                r = await ctx.llm([{"role": "system", "content": "Summarise the search results in 3 to 4 short sentences. EVERY sentence must end with a citation like [2] naming the source it comes from. Use only what the snippets say; add no numbers or claims that are not in them."},
                                   {"role": "user", "content": f"Topic: {t['name']}\n{numbered}"}], options={"num_predict": 200, "temperature": 0.1})
                txt = tidy(r["text"])
                if valid_digest(txt, sources):
                    digest = txt
            except Exception:
                pass
            doc = {"date": time.strftime("%Y-%m-%d", time.localtime(now)), "digest": digest, "sources": sources, "query": t["query"], "wiki": None}
            w = await file_to_wiki(t, doc)
            if w:
                doc["wiki"] = {"ok": w[0], "message": w[1]}
            ctx.runner.store.research_add(t["id"], doc)
            t["last"] = now
            done += 1
            await asyncio.sleep(2)                    # be polite to the search engine
        ctx.kv_set("topics", topics)
        ctx.note(f"{done} topic(s) researched, {skipped} not due")

    SKILLS = {"research": {"about": "search the web through the private SearXNG and summarise with citations", "args": {"query": "what to look up"}, "external": True}}

    async def skill_research(self, ctx, args):
        q = re.sub(r"\s+", " ", str(args.get("query", ""))).strip()[:160]
        if not q:
            return {"summary": "No question to research.", "facts": [], "followups": [], "recommend": [], "data": {}}
        page = await http_text(f"{SEARX}/search?q=" + re.sub(r"\s+", "+", q), 25)
        sources = parse_results(page, 6)
        if not sources:
            return {"summary": f"The search for '{q}' returned nothing usable.", "facts": [], "followups": [], "recommend": [], "data": {}}
        digest = None
        try:
            r = await ctx.llm([{"role": "system", "content": "Summarise the search results in 3 short sentences. EVERY sentence must end with a citation like [2] naming the source it comes from. Use only what the results say."},
                               {"role": "user", "content": f"Question: {q}\n" + "\n".join(f"[{i}] {s['title']}: {s['snippet']}" for i, s in enumerate(sources, 1))}], options={"num_predict": 180, "temperature": 0.1})
            txt = tidy(r["text"])
            digest = txt if valid_digest(txt, sources) else None
        except Exception:
            pass
        facts = ([digest] if digest else ["(no verified summary: the sources are listed instead)"]) + [f"[{i}] {s['title']} - {s['url']}" for i, s in enumerate(sources, 1)]
        return {"summary": (digest or f"{len(sources)} sources found for '{q}'")[:240], "facts": facts, "followups": [], "recommend": [], "data": {"sources": sources}}
