"""Day Briefing: the owner's personal morning brief: weather, calendar, news, and the lab in one paragraph.
Facts come from sources (Open-Meteo, RSS feeds, an optional private iCal URL, the Commander's briefing); the model only writes a short greeting from
those facts and a greeting containing any number that is not in them is replaced by a template. Headlines are shown as headlines, never re-written."""
import os, re, time, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ..redact import scrub
from ..runner import Agent
from ..sources import SourceError, http_text, weather

LAT, LON, PLACE = float(os.getenv("BRIEFING_LAT", "0")), float(os.getenv("BRIEFING_LON", "0")), os.getenv("BRIEFING_PLACE", "Home")  # set in the env file
FEEDS = [("NPR", "https://feeds.npr.org/1001/rss.xml"), ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"), ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
         ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"), ("BleepingComputer", "https://www.bleepingcomputer.com/feed/")]
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "heavy showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def parse_rss(text, n=3):
    if "<!ENTITY" in text or "<!DOCTYPE" in text[:2000] and "ENTITY" in text:
        return []                                   # never expand entities from an untrusted feed
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag in ("item", "entry"):
            t = next((c.text for c in it if c.tag.split("}")[-1] == "title" and c.text), None)
            link = next((c.text or c.attrib.get("href") for c in it if c.tag.split("}")[-1] == "link"), None)
            if t:
                out.append({"title": re.sub(r"\s+", " ", t).strip()[:160], "link": (link or "").strip()})
        if len(out) >= n:
            break
    return out


def parse_ics(text, now, tz, hours=36):
    """Events starting in the next `hours`. Recurring events (RRULE) are not expanded: only their first occurrence can show."""
    text = re.sub(r"\r?\n[ \t]", "", text)
    out = []
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        g = lambda k: (re.search(rf"^{k}[^:\n]*:(.*)$", blk, re.M) or [None, ""])[1].strip()
        raw = g("DTSTART")
        if not raw:
            continue
        try:
            if "T" in raw:
                dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc if raw.endswith("Z") else ZoneInfo(tz))
            else:
                dt = datetime.strptime(raw[:8], "%Y%m%d").replace(tzinfo=ZoneInfo(tz))
        except ValueError:
            continue
        start = datetime.fromtimestamp(now, ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() if "T" not in raw else now   # an all-day event today still counts
        if start <= dt.timestamp() <= now + hours * 3600:
            out.append({"when": dt.astimezone(ZoneInfo(tz)).strftime("%a %H:%M" if "T" in raw else "%a (all day)"), "title": g("SUMMARY")[:100] or "(no title)", "ts": dt.timestamp()})
    return sorted(out, key=lambda e: e["ts"])


def numbers(t):
    return set(re.findall(r"\d+(?:\.\d+)?", t))


def template_greeting(w, lab_line):
    parts = ["Good morning."]
    if w:
        parts.append(f"It is {w['now']} degrees and {w['sky']} in {PLACE}, with a high of {w['high']} and a low of {w['low']}" + (f"; {w['rain']} percent chance of rain." if w["rain"] >= 20 else "."))
    if lab_line:
        parts.append(lab_line)
    return " ".join(parts)


class DayBriefing(Agent):
    name = "day_briefing"
    group = "personal"
    title = "Day Briefing"
    autonomy = "R"
    at = ["06:15"]

    async def run(self, ctx):
        now, tz = time.time(), ctx.cfg["tz"]
        gaps, w = [], None
        ctx.working("reading", "weather")
        try:
            j = await weather(LAT, LON)
            c, d = j["current"], j["daily"]
            w = {"now": round(c["temperature_2m"]), "sky": WMO.get(c["weather_code"], "mixed"), "wind": round(c["wind_speed_10m"]), "high": round(d["temperature_2m_max"][0]), "low": round(d["temperature_2m_min"][0]), "rain": int(d["precipitation_probability_max"][0] or 0)}
        except (SourceError, KeyError, IndexError, ValueError) as e:
            gaps.append(f"weather unavailable: {str(e)[:100]}")
        ctx.working("reading", "calendar")
        ics = os.getenv("CALENDAR_ICS_URL", "")
        cal = {"configured": bool(ics), "events": []}
        if ics:
            try:
                cal["events"] = [{k: e[k] for k in ("when", "title")} for e in parse_ics(await http_text(ics), now, tz)]
            except SourceError as e:
                gaps.append(f"calendar unreadable: {str(e)[:100]}")
        else:
            gaps.append("calendar not connected (set CALENDAR_ICS_URL in /etc/prxy-command/env to a private iCal address)")
        news = []
        for name, url in FEEDS:
            ctx.working("reading", f"news: {name}")
            try:
                items = parse_rss(await http_text(url), 3)
                if items:
                    news.append({"source": name, "items": items})
            except SourceError as e:
                gaps.append(f"{name} feed unreadable")
        lab = ctx.runner.store.get_briefing()
        lab_line = scrub(lab["doc"]["headline"]) if lab else ""
        facts = (f"Weather: {w['now']} degrees, {w['sky']}, high {w['high']}, low {w['low']}, rain chance {w['rain']} percent. " if w else "") + (f"Calendar: {len(cal['events'])} events today. " if cal["configured"] else "") + (f"Lab: {lab_line}" if lab_line else "")
        greeting = template_greeting(w, lab_line)
        ctx.working("thinking", "writing the greeting")
        try:
            r = await ctx.llm([{"role": "system", "content": "Write a plain, friendly two-sentence good-morning message from the facts. No metaphors, no flowery words. Use only the facts; add no numbers or names that are not in them."}, {"role": "user", "content": facts}], options={"num_predict": 70, "temperature": 0.2})
            txt = r["text"].strip().replace("\n", " ")
            if txt and numbers(txt) <= numbers(facts) and 20 < len(txt) < 400:
                greeting = txt
        except Exception:
            pass
        date = datetime.fromtimestamp(now, ZoneInfo(tz)).strftime("%Y-%m-%d")
        doc = {"kind": "personal", "date": date, "generated": now, "headline": greeting, "place": PLACE, "weather": w, "calendar": cal, "news": news, "lab": {"headline": lab_line}, "gaps": gaps, "facts": facts}
        bid = ctx.runner.store.save_briefing(date, doc, "personal")
        ctx.runner.store.audit("agent:day_briefing", "brief", f"#{bid}", greeting[:160])
        ctx.runner.bus.publish("council.briefing", agent=self.name, id=bid, topics=0)
        ctx.note(f"brief #{bid}: {len(news)} news sources, {len(gaps)} gap(s)")
