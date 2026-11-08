"""Media Curator: what is worth watching, based on a watchlist the OWNER types in (nothing is read from the TV or from viewing history).
Data: TVmaze (free, no key): the watchlist titles' status and next episode, and the next seven days of US broadcast and web schedules.
Picks are scripted shows, documentaries and animation with a good rating, ranked by genre overlap with the watchlist plus a bonus for new series.
The result is shown on the Media page and served to the TV launcher's feed host at /feed/media.json (titles only)."""
import asyncio, json, re, time, urllib.parse
from datetime import datetime, timedelta
from ..runner import Agent
from ..sources import SourceError, http_text

pause = asyncio.sleep                          # replaced by a no-op in tests
KINDS = {"Scripted", "Documentary", "Animation"}


def rank(candidates, taste, limit=10):
    """candidates: [{'show','genres','rating','type','season','number','airdate','url','id'}]; taste: {genre: weight}. -> top picks with reasons."""
    best = {}
    for c in candidates:
        if c["type"] not in KINDS or (c["rating"] or 0) < 7.0 or not c["genres"]:
            continue
        overlap = [g for g in c["genres"] if g in taste]
        score = (c["rating"] or 0) + 2.0 * sum(taste[g] for g in overlap) + (1.5 if (c["season"], c["number"]) == (1, 1) else 0)
        why = ("new series; " if (c["season"], c["number"]) == (1, 1) else "") + (f"matches your taste: {', '.join(overlap)}" if overlap else f"highly rated ({c['rating']})")
        if c["id"] not in best or score > best[c["id"]]["score"]:
            best[c["id"]] = {"show": c["show"], "genres": c["genres"], "rating": c["rating"], "airdate": c["airdate"], "url": c["url"], "why": why, "score": round(score, 1), "new": (c["season"], c["number"]) == (1, 1)}
    return sorted(best.values(), key=lambda x: -x["score"])[:limit]


def taste_from(shows):
    """Genre weights (0..1) from the watchlist shows' genres."""
    cnt = {}
    for s in shows:
        for g in s.get("genres", []):
            cnt[g] = cnt.get(g, 0) + 1
    top = max(cnt.values(), default=1)
    return {g: n / top for g, n in cnt.items()}


async def jget(url):
    return json.loads(await http_text(url, 20))


class MediaCurator(Agent):
    name = "media_curator"
    group = "personal"
    title = "Media Curator"
    autonomy = "R"
    at = ["08:00"]

    async def run(self, ctx):
        wl = [t.strip() for t in (ctx.kv_get("watchlist", []) or []) if t.strip()][:30]
        shows, matched = [], []
        for title in wl:
            ctx.working("reading", f"watchlist: {title[:30]}")
            try:
                d = await jget("https://api.tvmaze.com/singlesearch/shows?q=" + urllib.parse.quote(title) + "&embed=nextepisode")
                nxt = ((d.get("_embedded") or {}).get("nextepisode") or {})
                shows.append(d)
                matched.append({"title": d["name"], "status": d.get("status"), "next": nxt.get("airdate"), "genres": d.get("genres", [])})
            except (SourceError, ValueError, KeyError):
                matched.append({"title": title, "status": "not found", "next": None, "genres": []})
            await pause(0.4)
        cands = []
        for i in range(7):
            day = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
            ctx.working("scanning", f"schedule {day}")
            for url, key in ((f"https://api.tvmaze.com/schedule/web?date={day}&country=US", "_embedded"), (f"https://api.tvmaze.com/schedule?country=US&date={day}", "show")):
                try:
                    for ep in await jget(url):
                        sh = ep["_embedded"]["show"] if key == "_embedded" else ep["show"]
                        cands.append({"id": sh["id"], "show": sh["name"], "genres": sh.get("genres", []), "rating": (sh.get("rating") or {}).get("average"), "type": sh.get("type"),
                                      "season": ep.get("season"), "number": ep.get("number"), "airdate": ep.get("airdate"), "url": sh.get("url", "")})
                except (SourceError, ValueError, KeyError):
                    continue
            await pause(0.4)
        taste = taste_from(shows)
        picks = rank([c for c in cands if c["show"].lower() not in {m["title"].lower() for m in matched}], taste)
        doc = {"generated": time.time(), "watchlist": matched, "picks": picks, "personalised": bool(taste), "candidates": len(cands)}
        ctx.kv_set("latest", doc)
        ctx.runner.bus.publish("media.updated", agent=self.name, picks=len(picks))
        ctx.note(f"{len(matched)} watchlist title(s), {len(cands)} scheduled episodes scanned, {len(picks)} pick(s)" + ("" if wl else " (no watchlist yet: showing what is well rated this week)"))
