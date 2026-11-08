"""Job and Career Scout. It FINDS roles, SCORES them against the owner's profile, DRAFTS tailored material and TRACKS applications.
It never applies: there is no code path that submits anything, it holds no job-site credentials, and the owner sets every status after
'applied'. Sources are public feeds and APIs (Remotive, Arbeitnow, the monthly Hacker News 'Who is hiring' thread), not scraped job boards.
The profile (resume text, target titles, keywords) is typed in by the owner, stored only on this server and deletable from the Careers page.
Tailoring rules: the model may only re-word lines that already exist in the resume. A rewritten line is discarded if it contains a number or a
capitalised tool/product name that is not in the original line; a cover letter is discarded if it names anything that is in neither the resume nor the job text."""
import hashlib, html, json, re, time
from ..runner import Agent
from ..sources import SourceError, http_text

VOCAB = ["proxmox", "linux", "windows", "firewall", "siem", "splunk", "elastic", "soc", "incident response", "threat hunting", "python", "bash", "powershell", "network security", "vlan", "cisco", "ccna", "security+",
         "wireshark", "nmap", "docker", "kubernetes", "aws", "azure", "gcp", "terraform", "ansible", "vpn", "ids", "ips", "suricata", "snort", "pfsense", "opnsense", "grafana", "prometheus", "loki", "active directory",
         "vulnerability", "pentest", "penetration testing", "forensics", "compliance", "nist", "iso 27001", "soc 2", "edr", "crowdstrike", "sentinel", "dns", "dhcp", "tcp/ip", "routing", "switching", "unifi", "sql", "javascript",
         "git", "ci/cd", "automation", "monitoring", "networking", "cloud", "iam", "zero trust", "malware", "owasp", "burp", "metasploit", "homelab", "virtualization", "vmware", "devops", "sysadmin", "helpdesk", "it support"]
STATUSES = ("found", "drafted", "applied", "interview", "rejected", "dismissed")


def skills_in(text):
    t = " " + re.sub(r"[^a-z0-9+/ .#-]", " ", (text or "").lower()) + " "
    return {v for v in VOCAB if re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", t)}


def score(job, profile, have):
    """0-100: title match 40, skills overlap 40, remote/location 10, freshness 10. Returns (score, matched, gaps)."""
    text = f"{job['title']} {job.get('snippet', '')}"
    need = skills_in(text)
    matched = sorted(need & have)
    gaps = sorted(need - have)
    tt = [w for t in profile.get("titles", []) for w in re.findall(r"[a-z0-9+]{3,}", t.lower())]
    tl = job["title"].lower()
    title_part = 40 * (sum(1 for w in set(tt) if w in tl) / max(1, len(set(tt)))) if tt else 20
    skill_part = 40 * (len(matched) / max(4, len(need))) if need else 10
    loc = (job.get("location") or "").lower()
    where = 10 if (profile.get("remote_ok", True) and ("remote" in loc or "remote" in text.lower())) or any(l.lower() in loc for l in profile.get("locations", []) if l) else 3
    kw = [k.lower() for k in profile.get("keywords", []) if k]
    bonus = min(10, 5 * sum(1 for k in kw if k in text.lower()))
    return int(max(0, min(100, title_part + skill_part + where + bonus))), matched, gaps


def tokens_new(rewritten, original):
    """Numbers and capitalised words in `rewritten` that are not in `original` (the tailoring guard). The first word of a sentence is capitalised
    only because it starts the sentence, so it is checked separately: it only counts if it names a known tool or skill the original never mentioned."""
    caps = lambda t: set(re.findall(r"\b[A-Z][A-Za-z0-9+#./-]{1,}\b", t)) | set(re.findall(r"\b[A-Za-z]*[0-9][A-Za-z0-9]*\b", t))
    first = re.match(r"\W*([A-Za-z0-9+#./-]+)", rewritten)
    body = rewritten[first.end():] if first else rewritten
    new = (caps(body) - caps(original)) - {"I", "The", "A", "An", "And", "In", "On", "For", "With", "To", "Of"}
    if first and first.group(1).lower() not in original.lower() and first.group(1).lower() in VOCAB:
        new.add(first.group(1))
    return new


def pick_lines(resume, job_text, n=6):
    lines = [re.sub(r"^[\s*\-•\d.)]+", "", l).strip() for l in resume.splitlines()]
    lines = [l for l in lines if 25 <= len(l) <= 260]
    jt = skills_in(job_text) | set(re.findall(r"[a-z]{5,}", job_text.lower()))
    return sorted(lines, key=lambda l: -len((skills_in(l) | set(re.findall(r"[a-z]{5,}", l.lower()))) & jt))[:n]


# ---- sources (public feeds; each returns normalised dicts, [] on trouble)
async def src_remotive(profile):
    out = []
    for q in (profile.get("titles", [])[:3] or ["security"]):
        try:
            d = json.loads(await http_text("https://remotive.com/api/remote-jobs?limit=40&search=" + re.sub(r"\s+", "+", q), 20))
            for j in d.get("jobs", []):
                out.append({"source": "Remotive", "title": j["title"], "company": j["company_name"], "url": j["url"], "location": j.get("candidate_required_location", "Remote"), "posted": (j.get("publication_date") or "")[:10],
                            "snippet": re.sub(r"<[^>]+>", " ", html.unescape(j.get("description", "")))[:600]})
        except (SourceError, ValueError, KeyError):
            continue
    return out


async def src_arbeitnow(profile):
    try:
        d = json.loads(await http_text("https://www.arbeitnow.com/api/job-board-api", 20))
        return [{"source": "Arbeitnow", "title": j["title"], "company": j["company_name"], "url": j["url"], "location": j.get("location", ""), "posted": time.strftime("%Y-%m-%d", time.gmtime(j.get("created_at", 0))),
                 "snippet": re.sub(r"<[^>]+>", " ", html.unescape(j.get("description", "")))[:600]} for j in d.get("data", [])]
    except (SourceError, ValueError, KeyError):
        return []


async def src_hn(profile):
    try:
        s = json.loads(await http_text("https://hn.algolia.com/api/v1/search_by_date?query=%22who%20is%20hiring%22&tags=story,author_whoishiring&hitsPerPage=3", 20))
        story = next(h for h in s["hits"] if "who is hiring" in h["title"].lower())
        items = json.loads(await http_text(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", 40))
        out = []
        for c in items.get("children", [])[:400]:
            txt = re.sub(r"<[^>]+>", " ", html.unescape(c.get("text") or ""))
            head = txt.strip().split("  ")[0][:200]
            parts = [p.strip() for p in head.split("|")]
            if len(parts) >= 2:
                out.append({"source": "HN Who is hiring", "title": parts[1][:120], "company": parts[0][:80], "url": f"https://news.ycombinator.com/item?id={c['id']}", "location": next((p for p in parts[2:] if re.search(r"remote|onsite|hybrid|[A-Z]{2}\b", p)), ""),
                            "posted": (c.get("created_at") or "")[:10], "snippet": re.sub(r"\s+", " ", txt)[:600]})
        return out
    except (SourceError, ValueError, KeyError, StopIteration):
        return []


SOURCES = [src_remotive, src_arbeitnow, src_hn]


class JobScout(Agent):
    name = "job_scout"
    group = "personal"
    title = "Job and Career Scout"
    autonomy = "P"
    at = ["09:00"]
    auto_resolve = True

    async def draft(self, ctx, job, resume, profile):
        text = f"{job['title']} {job.get('snippet', '')}"
        lines = pick_lines(resume, text)
        bullets = []
        for orig in lines:
            tailored = orig
            try:
                r = await ctx.llm([{"role": "system", "content": "Rewrite ONE resume line so it uses the vocabulary of the job posting where it truly fits. Do not add facts, numbers, tools or employers. One line only."},
                                   {"role": "user", "content": f"Job: {job['title']} at {job['company']}. Posting excerpt: {job.get('snippet', '')[:300]}\nResume line: {orig}"}], options={"num_predict": 90, "temperature": 0.2})
                cand = r["text"].strip().splitlines()[0].strip("-* ")
                if cand and not tokens_new(cand, orig) and 15 < len(cand) < 300:
                    tailored = cand
            except Exception:
                pass
            bullets.append({"original": orig, "tailored": tailored, "changed": tailored != orig})
        letter = None
        try:
            r = await ctx.llm([{"role": "system", "content": "Write a short cover letter (about 120 words) from the resume lines for this job. Use only facts from the lines. No invented numbers, tools or employers. Plain tone."},
                               {"role": "user", "content": f"Role: {job['title']} at {job['company']}\nMy resume lines:\n" + "\n".join("- " + l for l in lines)}], options={"num_predict": 220, "temperature": 0.3})
            if not tokens_new(r["text"], resume + " " + text + " " + job["company"] + " " + job["title"]):
                letter = r["text"].strip()
        except Exception:
            pass
        return {"bullets": bullets, "letter": letter, "made": time.time(), "note": "Check every line before using it. Only wording was changed; nothing here was added to your history."}

    async def run(self, ctx):
        st = ctx.runner.store
        prof = ctx.kv_get("profile")
        if not prof or not prof.get("resume", "").strip():
            ctx.finding("careers:no-profile", "info", "The Job and Career Scout is waiting for your resume and targets", "Open the Careers page, paste your resume text, and list the job titles you want. Nothing is searched until then. The profile stays on this server and you can delete it there.",
                        [{"type": "config", "ref": "careers profile", "excerpt": "no profile saved"}], confidence=1.0)
            ctx.skip("no profile yet")
            return
        have = skills_in(prof["resume"]) | {k.lower() for k in prof.get("keywords", [])}
        new, seen = 0, 0
        if ctx.trigger != "draft-only":
            for src in SOURCES:
                ctx.working("reading", src.__name__.replace("src_", "") + " listings")
                for j in await src(prof):
                    sc, matched, gaps = score(j, prof, have)
                    if sc < 35:
                        continue
                    j.update(id=hashlib.sha1((j["source"] + j["url"]).encode()).hexdigest()[:12], score=sc, matched=matched, gaps=gaps)
                    new += st.job_upsert(j); seen += 1
        todo = [j for j in st.jobs_list("found", 200) if j["drafts"] is None and j["score"] >= 55][:3] + [j for j in st.jobs_list(None, 500) if j["want_draft"] and j["drafts"] is None][:3]
        for j in {j["id"]: j for j in todo}.values():
            ctx.working("typing", f"draft for {j['company'][:24]}")
            d = await self.draft(ctx, j, prof["resume"], prof)
            st.job_update(j["id"], drafts=d, want_draft=0, **({"status": "drafted"} if j["status"] == "found" else {}))
        now = time.time()
        for j in st.jobs_list("applied", 100):                         # follow-ups: an application with no change after 7 days
            due = j["followup"] or (j["status_ts"] + 7 * 86400)
            if now > due:
                ctx.finding(f"careers:followup:{j['id']}", "low", f"Follow up on your application to {j['company']} ({j['title'][:50]})", f"You marked this applied {int((now - j['status_ts']) / 86400)} days ago with no update. A short follow-up message is normal after a week.",
                            [{"type": "tracker", "ref": j["url"], "excerpt": f"status applied since {time.strftime('%Y-%m-%d', time.localtime(j['status_ts']))}"}], confidence=0.9)
        ctx.note(f"{seen} matching listing(s), {new} new; {len(st.jobs_list('found', 500))} waiting for your review")
