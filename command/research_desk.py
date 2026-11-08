"""The research desk: the boss asks a question, the crew researches it on the web, an outside advisor checks the answer.

  1. SEARCH   the question goes to the private SearXNG (the only thing that leaves the lab is the question itself)
  2. READ     the top pages are fetched and reduced to the paragraphs that match the question. Links to private, loopback or link-local addresses are
              refused (a search result must never make this server reach into the lab), only http(s), a size cap and a timeout per page
  3. WRITE    the local model (Shelf) answers in a few sentences; every sentence must cite a numbered source and every number must appear in the sources
  4. CHECK    the outside advisor assigned to the 'research' job reads the same extracts: supported / partly / unsupported, and may give a corrected answer
              (accepted only under the same citation and number rules). Without an advisor the local answer stands, marked as unchecked
Results are kept on the Research page (table `research`, topic 'desk'). Nothing here can change anything in the lab."""
import asyncio, html, ipaddress, json, re, socket, time
from urllib.parse import urlparse

import aiohttp

from .agents.research_librarian import SEARX, parse_results, tidy
from .frontier import FrontierError, extract_json
from .redact import scrub
from .sources import SourceError, http_text

MAX_PAGE = 600_000
STOP = {"the", "and", "for", "with", "what", "how", "why", "are", "is", "a", "an", "of", "to", "in", "on", "do", "does", "can", "should", "which", "best", "my", "i"}


def numbers(t):
    return set(re.findall(r"\d+(?:\.\d+)?", re.sub(r"\[\d+\]", "", t or "")))


def valid_answer(text, pool, n_sources, max_sents=7):
    sents = [x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x]
    if not 1 <= len(sents) <= max_sents:
        return False
    for x in sents:
        cites = [int(c) for c in re.findall(r"\[(\d+)\]", x)]
        if not cites or any(not 1 <= c <= n_sources for c in cites):
            return False
    return numbers(text) <= numbers(pool)


def page_text(raw):
    """Readable text of an HTML page: scripts, styles and navigation removed, paragraphs kept."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", raw)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    paras = [re.sub(r"[ \t\r\f\v]+", " ", p).strip() for p in txt.split("\n")]
    return [p for p in paras if len(p) > 60]


def best_paragraphs(paras, question, limit=1800):
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", question.lower()) if w not in STOP]
    scored = sorted(enumerate(paras), key=lambda ip: (-sum(ip[1].lower().count(w) for w in words), ip[0]))
    out, n = [], 0
    for i, p in sorted(scored[:12], key=lambda ip: ip[0]):
        p = p if len(p) <= 700 else p[:700].rsplit(" ", 1)[0] + "…"   # a huge paragraph is trimmed, not dropped
        if n + len(p) > limit:
            continue
        out.append(p); n += len(p)
    return " ".join(out)


async def public_url(url):
    """True only for http(s) URLs whose host resolves to public addresses (no lab, loopback, link-local or metadata addresses)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        a = ipaddress.ip_address(info[4][0])
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
            return False
    return True


async def read_page(url, timeout=15):
    if not await public_url(url):
        raise SourceError("refused: not a public web address")
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 PrxyCommand-research"}) as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True, max_redirects=3) as r:
            if not await public_url(str(r.url)):
                raise SourceError("refused: redirected to a non-public address")
            if r.status != 200:
                raise SourceError(f"HTTP {r.status}")
            if "html" not in r.headers.get("Content-Type", "html") and "text" not in r.headers.get("Content-Type", ""):
                raise SourceError("not a web page")
            buf = bytearray()                                         # content.read(n) returns only what has arrived so far: keep reading up to the cap
            async for chunk in r.content.iter_chunked(65536):
                buf += chunk
                if len(buf) >= MAX_PAGE:
                    break
            return bytes(buf[:MAX_PAGE]).decode(r.charset or "utf-8", errors="replace")


class ResearchDesk:
    def __init__(self, runner, advisors=None, fetch=read_page, search=None, allow=public_url):
        self.rt, self.store, self.bus, self.adv = runner, runner.store, runner.bus, advisors
        self.fetch, self.allow = fetch, allow
        self.search = search or (lambda q: http_text(f"{SEARX}/search?q=" + re.sub(r"\s+", "+", q), 25))
        self.tasks = {}

    def _save(self, rid, doc):
        self.store.x("UPDATE research SET doc=? WHERE id=?", (json.dumps(doc), rid))

    def ask(self, question):
        q = re.sub(r"\s+", " ", str(question or "")).strip()[:300]
        if len(q) < 8:
            raise ValueError("ask a question of at least a few words")
        doc = {"question": q, "status": "searching", "asked": time.time(), "sources": [], "answer": None, "local_answer": None, "check": None, "by": None, "steps": []}
        rid = self.store.research_add("desk", doc)
        self.store.audit("owner", "research.ask", f"research#{rid}", q[:150])
        self.bus.publish("research.asked", agent="research_librarian", id=rid, question=q)
        self.tasks[rid] = asyncio.create_task(self._run(rid, doc))
        return rid

    async def _run(self, rid, doc):
        try:
            await self.work(rid, doc)
        except Exception as e:
            doc.update(status="failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            self._save(rid, doc)
        self.bus.publish("research.done", agent="research_librarian", id=rid, question=doc["question"], status=doc["status"], by=doc.get("by"))

    async def work(self, rid, doc):
        q = doc["question"]
        try:
            hits = parse_results(await self.search(q), 8)
        except SourceError as e:
            doc.update(status="failed", error=f"SearXNG: {e}"); self._save(rid, doc); return
        if not hits:
            doc.update(status="failed", error="the search found nothing usable"); self._save(rid, doc); return
        doc["status"] = "reading"; self._save(rid, doc)
        sources = []
        for h in hits:
            if len(sources) >= 4:
                break
            if not await self.allow(h["url"]):                        # checked here as well as in read_page: no fetcher may reach into the lab
                doc["steps"].append(f"refused {urlparse(h['url']).hostname}: not a public web address")
                continue
            try:
                ext = best_paragraphs(page_text(await self.fetch(h["url"])), q)
            except Exception as e:
                doc["steps"].append(f"skipped {urlparse(h['url']).hostname}: {str(e)[:80]}")
                continue
            if len(ext) < 80:                                        # nothing readable on the page: keep the search snippet
                ext = h["snippet"]
            sources.append({"title": h["title"], "url": h["url"], "extract": scrub(ext)[:1800], "read": ext != h["snippet"]})
        if not sources:
            sources = [{"title": h["title"], "url": h["url"], "extract": h["snippet"], "read": False} for h in hits[:5]]
        doc["sources"] = sources
        pool = " ".join(s["title"] + " " + s["extract"] for s in sources)
        numbered = "\n\n".join(f"[{i}] {s['title']}\n{s['extract']}" for i, s in enumerate(sources, 1))
        doc["status"] = "writing"; self._save(rid, doc)
        try:
            r = await self.rt.gateway.chat("research_librarian", [
                {"role": "system", "content": "Answer the question from the numbered sources only, in 3 to 6 short sentences. EVERY sentence must end with a citation like [2]. "
                                              "Do not use any number that is not written in the sources. If the sources do not answer it, say so (with a citation)."},
                {"role": "user", "content": f"Question: {q}\n\nSources:\n{numbered}"}], options={"num_predict": 320, "temperature": 0.1}, timeout=180)
            txt = tidy(r["text"])
            if valid_answer(txt, pool, len(sources)):
                doc["local_answer"] = txt
        except Exception as e:
            doc["steps"].append(f"local model: {str(e)[:100]}")
        doc["answer"], doc["by"] = doc["local_answer"], ("research_librarian" if doc["local_answer"] else None)
        if self.adv and self.adv.ready("research"):
            doc["status"] = "checking"; self._save(rid, doc)
            sysm = ("You fact-check a short research answer written by a small local model. You get the question, numbered source extracts and the answer. "
                    "Reply with ONE JSON object: {\"verdict\": \"supported\" | \"partly\" | \"unsupported\", \"answer\": \"a better answer in 3 to 6 sentences, EVERY sentence ending with a citation like [2], "
                    "using only the extracts and no number that is not in them\", \"notes\": \"one sentence: what was wrong or missing\"}.")
            user = f"Question: {q}\n\nSources:\n{numbered}\n\nAnswer to check:\n{doc['local_answer'] or '(the local model gave no usable answer)'}"
            try:
                rr = await self.adv.ask("research", sysm, user, 900)
                j = extract_json(rr["text"]) or {}
                verdict = j.get("verdict") if j.get("verdict") in ("supported", "partly", "unsupported") else None
                better = tidy(str(j.get("answer") or ""))
                doc["check"] = {"advisor": rr.get("advisor"), "model": rr.get("model"), "verdict": verdict, "notes": scrub(str(j.get("notes") or ""))[:300]}
                if better and valid_answer(better, pool, len(sources)) and (verdict != "supported" or not doc["local_answer"]):
                    doc["answer"], doc["by"] = better, rr.get("advisor")
            except FrontierError as e:
                doc["steps"].append(f"fact-check: {str(e)[:100]}")
        doc["status"] = "done" if doc["answer"] else "sources_only"
        doc["finished"] = time.time()
        self._save(rid, doc)
