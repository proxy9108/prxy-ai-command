"""Findings store (the council's Memory), run history, audit log, LLM usage. SQLite, WAL, stdlib only.
Calls are synchronous and small; at this scale they are fast enough to call from the event loop."""
import hashlib, json, sqlite3, threading, time, uuid
from .redact import scrub

SEVERITIES = ("info", "low", "medium", "high", "critical")
OPEN = ("new", "seen")
CHRONIC_S = 14 * 86400
STATUSES = ("new", "seen", "approved", "rejected", "done", "resolved", "expired")   # done = the owner dealt with it; resolved = the condition cleared by itself

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings(
  id TEXT PRIMARY KEY, agent TEXT NOT NULL, fingerprint TEXT NOT NULL, ts REAL NOT NULL, last_seen REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1,
  severity TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '[]', evidence_expires REAL, proposed_action TEXT, status TEXT NOT NULL DEFAULT 'new', expires_at REAL, decided_at REAL);
CREATE INDEX IF NOT EXISTS ix_find_fp ON findings(agent, fingerprint, status);
CREATE INDEX IF NOT EXISTS ix_find_ts ON findings(ts);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
  summary TEXT DEFAULT '', findings INTEGER DEFAULT 0, error TEXT, trigger TEXT DEFAULT 'schedule');
CREATE INDEX IF NOT EXISTS ix_runs_agent ON runs(agent, started);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT DEFAULT '',
  detail TEXT DEFAULT '', prev TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL, action TEXT NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL, decided_at REAL, executed_at REAL, note TEXT DEFAULT '', result TEXT, rollback_result TEXT, critic TEXT);
CREATE INDEX IF NOT EXISTS ix_appr_f ON approvals(finding_id);
CREATE TABLE IF NOT EXISTS briefings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ts REAL NOT NULL, trigger TEXT DEFAULT 'schedule', headline TEXT DEFAULT '', doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_brief_date ON briefings(date, ts);
CREATE TABLE IF NOT EXISTS research(
  id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, ts REAL NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_res_topic ON research(topic, ts);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, doc TEXT NOT NULL, section TEXT NOT NULL, text TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open', closed_at REAL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, url TEXT, location TEXT, posted TEXT, score INTEGER DEFAULT 0, matched TEXT DEFAULT '[]', gaps TEXT DEFAULT '[]',
  status TEXT DEFAULT 'found', notes TEXT DEFAULT '', first_seen REAL, status_ts REAL, followup REAL, drafts TEXT, want_draft INTEGER DEFAULT 0, snippet TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_jobs_score ON jobs(score);
CREATE TABLE IF NOT EXISTS kv(
  k TEXT PRIMARY KEY, v TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent TEXT NOT NULL, model TEXT, prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, ms INTEGER DEFAULT 0, priority INTEGER DEFAULT 2, ok INTEGER DEFAULT 1, error TEXT);
CREATE INDEX IF NOT EXISTS ix_usage ON usage(agent, ts);
CREATE TABLE IF NOT EXISTS signals(
  agent TEXT NOT NULL, entity TEXT NOT NULL, kind TEXT NOT NULL, first_ts REAL NOT NULL, ts REAL NOT NULL, strength REAL NOT NULL DEFAULT 0.3,
  text TEXT DEFAULT '', run_id INTEGER, count INTEGER DEFAULT 1, PRIMARY KEY(agent, entity, kind));
CREATE INDEX IF NOT EXISTS ix_sig_ent ON signals(entity, ts);
CREATE TABLE IF NOT EXISTS trace(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, ref TEXT DEFAULT '', detail TEXT DEFAULT '', ms INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_trace_run ON trace(run_id);
CREATE TABLE IF NOT EXISTS frontier_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, purpose TEXT, backend TEXT, model TEXT, tin INTEGER DEFAULT 0, tout INTEGER DEFAULT 0, cost REAL DEFAULT 0,
  ok INTEGER DEFAULT 1, error TEXT, sent TEXT, reply TEXT);
CREATE INDEX IF NOT EXISTS ix_fl ON frontier_log(ts);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT, project_id TEXT, action TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
  started REAL, finished REAL, prechecks TEXT, before TEXT, after TEXT, calls TEXT, verify TEXT, note TEXT DEFAULT '', outside TEXT);
CREATE INDEX IF NOT EXISTS ix_act ON actions(created);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL, origin TEXT DEFAULT 'owner', ref TEXT DEFAULT '', agents TEXT DEFAULT '[]',
  created REAL NOT NULL, updated REAL NOT NULL, result TEXT, approve_plan INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_proj_upd ON projects(updated);
CREATE TABLE IF NOT EXISTS project_msgs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, ts REAL NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_pm ON project_msgs(project_id, id);
CREATE TABLE IF NOT EXISTS project_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, n INTEGER NOT NULL, agent TEXT NOT NULL, skill TEXT NOT NULL, args TEXT DEFAULT '{}', why TEXT DEFAULT '',
  source TEXT DEFAULT 'rules', requested_by TEXT DEFAULT 'commander', depth INTEGER DEFAULT 0, needs_ok INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', result TEXT,
  run_id INTEGER, started REAL, finished REAL);
CREATE INDEX IF NOT EXISTS ix_pt ON project_tasks(project_id, n);
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, thread TEXT NOT NULL, author TEXT NOT NULL, to_agent TEXT DEFAULT '', kind TEXT NOT NULL DEFAULT 'say',
  text TEXT NOT NULL, ref TEXT DEFAULT '', data TEXT);
CREATE INDEX IF NOT EXISTS ix_chat ON chat(thread, id);
CREATE INDEX IF NOT EXISTS ix_chat_ts ON chat(ts);
CREATE TABLE IF NOT EXISTS missions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, updated REAL NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL, origin TEXT DEFAULT 'owner', ref TEXT DEFAULT '',
  status TEXT NOT NULL, version INTEGER DEFAULT 1, plan TEXT, review TEXT, hash TEXT, approved_at REAL, run TEXT, result TEXT, feedback TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_missions ON missions(updated);
CREATE TABLE IF NOT EXISTS maintenance(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, start REAL NOT NULL, end REAL NOT NULL, scope TEXT NOT NULL, agents TEXT DEFAULT '[]',
  pattern TEXT DEFAULT '', note TEXT DEFAULT '', cancelled INTEGER DEFAULT 0, hits INTEGER DEFAULT 0);
"""

MIGRATE = {   # table -> [(column, definition)]: added to databases that already exist
    "findings": [("critic", "TEXT"), ("run_id", "INTEGER"), ("last_run_id", "INTEGER"), ("changed_at", "REAL"), ("links", "TEXT"), ("thread", "TEXT")],
    "runs": [("checked", "INTEGER")],
    "missions": [("attempt", "INTEGER DEFAULT 1"), ("history", "TEXT"), ("trail", "TEXT"), ("owner", "TEXT"), ("auto_rule", "TEXT")],
    "usage": [("run_id", "INTEGER")],
}


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for table, adds in MIGRATE.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in adds:
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ---- findings
    @staticmethod
    def _f(r):
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"] or "[]")
        d["proposed_action"] = json.loads(d["proposed_action"]) if d["proposed_action"] else None
        d["critic"] = json.loads(d["critic"]) if d.get("critic") else None
        d["links"] = json.loads(d["links"]) if d.get("links") else []
        d["thread"] = json.loads(d["thread"]) if d.get("thread") else None
        d["changed_at"] = d.get("changed_at") or d["ts"]
        d["chronic"] = d["status"] in OPEN and time.time() - d["changed_at"] > CHRONIC_S      # open and unchanged for two weeks: a standing condition, not news
        return d

    def upsert_finding(self, agent, fingerprint, severity, title, summary="", evidence=None, proposed_action=None,
                       confidence=0.5, evidence_ttl_s=None, ttl_s=None, suppress_days=30, run_id=None):
        """Create a finding, or bump the open one with the same fingerprint. Returns (finding|None, state) where state is
        'created', 'updated' or 'suppressed' (the owner rejected this exact finding recently, so the agent stays quiet)."""
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        now = time.time()
        with self.lock:
            cur = self.q("SELECT * FROM findings WHERE agent=? AND fingerprint=? AND status IN ('new','seen') ORDER BY ts DESC LIMIT 1", (agent, fingerprint))
            ev_exp = now + evidence_ttl_s if evidence_ttl_s else None
            if cur:
                f = cur[0]
                # the agent re-evaluated it just now, so the newest severity is the truth (it can go down as well as up)
                changed = now if severity != f["severity"] else (f["changed_at"] or f["ts"])        # only a change of severity is news; seeing the same thing again is not
                self.x("UPDATE findings SET last_seen=?, count=count+1, severity=?, title=?, summary=?, evidence=?, evidence_expires=?, confidence=?, last_run_id=?, changed_at=? WHERE id=?",
                       (now, severity, title, summary, json.dumps(evidence or []), ev_exp, confidence, run_id, changed, f["id"]))
                return self.get(f["id"]), "updated"
            rej = self.q("SELECT decided_at FROM findings WHERE agent=? AND fingerprint=? AND status='rejected' ORDER BY decided_at DESC LIMIT 1", (agent, fingerprint))
            if rej and rej[0]["decided_at"] and now - rej[0]["decided_at"] < suppress_days * 86400:
                return None, "suppressed"
            fid = uuid.uuid4().hex[:12]
            self.x("INSERT INTO findings(id,agent,fingerprint,ts,last_seen,severity,confidence,title,summary,evidence,evidence_expires,proposed_action,expires_at,run_id,last_run_id,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (fid, agent, fingerprint, now, now, severity, confidence, title, summary, json.dumps(evidence or []), ev_exp,
                    json.dumps(proposed_action) if proposed_action else None, now + ttl_s if ttl_s else None, run_id, run_id, now))
            return self.get(fid), "created"

    def get(self, fid):
        r = self.q("SELECT * FROM findings WHERE id=?", (fid,))
        return self._f(r[0]) if r else None

    def list_findings(self, agent=None, status=None, severity=None, limit=100):
        sql, args = "SELECT * FROM findings WHERE 1=1", []
        if agent: sql += " AND agent=?"; args.append(agent)
        if status: sql += " AND status=?"; args.append(status)
        if severity: sql += " AND severity=?"; args.append(severity)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        return [self._f(r) for r in self.q(sql, args)]

    def set_status(self, fid, status):
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        n = self.x("UPDATE findings SET status=?, decided_at=? WHERE id=?", (status, time.time(), fid)).rowcount
        return self.get(fid) if n else None

    def set_critic(self, fid, review, confidence=None):
        self.x("UPDATE findings SET critic=? WHERE id=?", (json.dumps(review), fid))
        if confidence is not None:
            self.x("UPDATE findings SET confidence=? WHERE id=?", (confidence, fid))
        return self.get(fid)

    def set_links(self, fid, ids):
        self.x("UPDATE findings SET links=? WHERE id=?", (json.dumps(list(ids)), fid))

    def set_thread(self, fid, thread):
        self.x("UPDATE findings SET thread=? WHERE id=?", (json.dumps(thread), fid))
        return self.get(fid)

    def open_findings(self, limit=500):
        return [self._f(r) for r in self.q("SELECT * FROM findings WHERE status IN ('new','seen') ORDER BY ts DESC LIMIT ?", (limit,))]

    def agent_quality(self, since):
        """How often the owner agrees with each agent: accepted = seen/approved/done, rejected = rejected. Findings that cleared by themselves are not counted either way."""
        out = {}
        for r in self.q("SELECT agent, status, COUNT(*) c FROM findings WHERE ts>=? GROUP BY agent, status", (since,)):
            q = out.setdefault(r["agent"], {"accepted": 0, "rejected": 0, "resolved": 0, "open": 0})
            if r["status"] in ("seen", "approved", "done"): q["accepted"] += r["c"]
            elif r["status"] == "rejected": q["rejected"] += r["c"]
            elif r["status"] == "resolved": q["resolved"] += r["c"]
            elif r["status"] in OPEN: q["open"] += r["c"]
        for q in out.values():
            d = q["accepted"] + q["rejected"]
            q["decided"], q["precision"] = d, (round(q["accepted"] / d, 2) if d >= 3 else None)
        return out

    def needs_review(self, limit=50):
        """Open findings that carry a proposed action, or are high/critical, and have no second opinion for their current state yet."""
        out = []
        for r in self.q("SELECT * FROM findings WHERE status IN ('new','seen') AND (proposed_action IS NOT NULL OR severity IN ('high','critical')) ORDER BY ts DESC LIMIT 200"):
            f = self._f(r)
            if f["agent"] == "critic":
                continue
            c = f["critic"]
            if not c or c.get("count_at_review") != f["count"] and f["count"] - c.get("count_at_review", 0) >= 5:
                out.append(f)
        return out[:limit]

    def open_counts(self):
        out = {}
        for r in self.q("SELECT agent, severity, COUNT(*) c FROM findings WHERE status IN ('new','seen') GROUP BY agent, severity"):
            out.setdefault(r["agent"], {})[r["severity"]] = r["c"]
        return out

    def purge(self, retention_s=24 * 3600):
        """Retention: drop evidence past its TTL (summaries stay, without identifiers) and expire stale open findings."""
        now = time.time()
        with self.lock:
            for r in self.q("SELECT id, title, summary FROM findings WHERE evidence_expires IS NOT NULL AND evidence_expires<?", (now,)):
                self.x("UPDATE findings SET title=?, summary=? WHERE id=?", (scrub(r["title"]), scrub(r["summary"]), r["id"]))   # the identifying part leaves with the evidence
            self.x("DELETE FROM briefings WHERE ts<?", (now - 60 * 86400,))
            ev = self.x("UPDATE findings SET evidence='[]', evidence_expires=NULL WHERE evidence_expires IS NOT NULL AND evidence_expires<? AND evidence!='[]'", (now,)).rowcount
            ex = self.x("UPDATE findings SET status='expired' WHERE expires_at IS NOT NULL AND expires_at<? AND status IN ('new','seen')", (now,)).rowcount
            self.x("DELETE FROM signals WHERE ts<?", (now - retention_s,))          # weak signals name machines and behaviour: same 24 h rule as evidence
            self.x("DELETE FROM trace WHERE ts<?", (now - retention_s,))
            self.x("DELETE FROM maintenance WHERE end<?", (now - 30 * 86400,))
            self.x("DELETE FROM frontier_log WHERE ts<?", (now - 14 * 86400,))
            for r in self.q("SELECT id FROM projects WHERE updated<?", (now - 60 * 86400,)):      # a project is kept for 60 days
                for t in ("project_msgs", "project_tasks"):
                    self.x(f"DELETE FROM {t} WHERE project_id=?", (r["id"],))
                self.x("DELETE FROM projects WHERE id=?", (r["id"],))
            self.x("DELETE FROM runs WHERE started<?", (now - 30 * 86400,))
            self.x("DELETE FROM usage WHERE ts<?", (now - 30 * 86400,))
        return {"evidence_dropped": ev, "findings_expired": ex}

    # ---- runs
    def run_start(self, agent, trigger):
        return self.x("INSERT INTO runs(agent,started,status,trigger) VALUES(?,?,?,?)", (agent, time.time(), "running", trigger)).lastrowid

    def run_end(self, rid, status, summary="", findings=0, error=None, checked=None):
        self.x("UPDATE runs SET finished=?, status=?, summary=?, findings=?, error=?, checked=? WHERE id=?", (time.time(), status, summary[:500], findings, (error or "")[:500] or None, checked, rid))

    def get_run(self, rid):
        r = self.q("SELECT * FROM runs WHERE id=?", (rid,))
        return dict(r[0]) if r else None

    def list_runs(self, agent=None, limit=50):
        sql, args = "SELECT * FROM runs", []
        if agent: sql += " WHERE agent=?"; args.append(agent)
        sql += " ORDER BY started DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        return [dict(r) for r in self.q(sql, args)]

    # ---- audit (hash chain: editing or deleting a row breaks every later hash)
    def audit(self, actor, action, target="", detail=""):
        with self.lock:
            last = self.q("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
            prev = last[0]["hash"] if last else "genesis"
            ts = round(time.time(), 3)
            h = hashlib.sha256((prev + json.dumps([ts, actor, action, target, detail])).encode()).hexdigest()
            self.x("INSERT INTO audit(ts,actor,action,target,detail,prev,hash) VALUES(?,?,?,?,?,?,?)", (ts, actor, action, target, str(detail)[:1000], prev, h))

    def list_audit(self, limit=100):
        return [dict(r) for r in self.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),))]

    def verify_audit(self):
        prev = "genesis"
        for r in self.q("SELECT * FROM audit ORDER BY id"):
            h = hashlib.sha256((prev + json.dumps([r["ts"], r["actor"], r["action"], r["target"], r["detail"]])).encode()).hexdigest()
            if r["prev"] != prev or r["hash"] != h:
                return {"ok": False, "broken_at": r["id"]}
            prev = r["hash"]
        return {"ok": True}

    # ---- approvals: the owner's decisions on proposed actions (every action keeps its plan and rollback plan as approved)
    @staticmethod
    def _a(r):
        d = dict(r)
        for k in ("action", "result", "rollback_result", "critic"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def approval_create(self, finding_id, agent, action, status, note=""):
        now = time.time()
        return self.x("INSERT INTO approvals(finding_id,agent,kind,action,status,created,decided_at,note) VALUES(?,?,?,?,?,?,?,?)",
                      (finding_id, agent, action.get("kind", "manual"), json.dumps(action), status, now, now, note[:500])).lastrowid

    def approval_get(self, aid):
        r = self.q("SELECT * FROM approvals WHERE id=?", (aid,))
        return self._a(r[0]) if r else None

    def approval_update(self, aid, **kw):
        cols = {"status", "executed_at", "note", "result", "rollback_result", "critic", "decided_at"}
        sets, args = [], []
        for k, v in kw.items():
            if k not in cols:
                raise ValueError(k)
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k in ("result", "rollback_result", "critic") and v is not None else v)
        self.x(f"UPDATE approvals SET {', '.join(sets)} WHERE id=?", (*args, aid))
        return self.approval_get(aid)

    def approvals_for_finding(self, fid):
        return [self._a(r) for r in self.q("SELECT * FROM approvals WHERE finding_id=? ORDER BY created DESC", (fid,))]

    def list_approvals(self, status=None, limit=100):
        sql, args = "SELECT * FROM approvals", []
        if status: sql += " WHERE status=?"; args.append(status)
        sql += " ORDER BY created DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        return [self._a(r) for r in self.q(sql, args)]

    def pending_proposals(self):
        """Open findings that carry a proposed action nobody has decided on yet."""
        out = []
        for r in self.q("SELECT * FROM findings WHERE proposed_action IS NOT NULL AND status IN ('new','seen') ORDER BY ts DESC LIMIT 200"):
            f = self._f(r)
            if not [a for a in self.approvals_for_finding(f["id"]) if a["status"] in ("approved", "executing", "done", "failed", "rejected")]:
                out.append(f)
        return out

    # ---- briefings (the Commander's daily output) and Memory lookups
    def save_briefing(self, date, doc, trigger="schedule"):
        return self.x("INSERT INTO briefings(date,ts,trigger,headline,doc) VALUES(?,?,?,?,?)", (date, time.time(), trigger, doc.get("headline", "")[:300], json.dumps(doc))).lastrowid

    def get_briefing(self, bid=None, trigger=None):
        """trigger None = the Commander's lab briefing (schedule/manual); 'personal' = the Day Briefing."""
        if bid:
            r = self.q("SELECT * FROM briefings WHERE id=?", (bid,))
        elif trigger == "personal":
            r = self.q("SELECT * FROM briefings WHERE trigger='personal' ORDER BY ts DESC LIMIT 1")
        else:
            r = self.q("SELECT * FROM briefings WHERE trigger!='personal' ORDER BY ts DESC LIMIT 1")
        return {**dict(r[0]), "doc": json.loads(r[0]["doc"])} if r else None

    def list_briefings(self, limit=30, personal=False):
        cond = "trigger='personal'" if personal else "trigger!='personal'"
        return [dict(r) for r in self.q(f"SELECT id,date,ts,trigger,headline FROM briefings WHERE {cond} ORDER BY ts DESC LIMIT ?", (max(1, min(int(limit), 200)),))]

    def briefing_today(self, date, trigger="schedule"):
        return bool(self.q("SELECT 1 FROM briefings WHERE date=? AND trigger=? LIMIT 1", (date, trigger)))

    def search_findings(self, q, limit=30):
        like = f"%{q.lower()}%"
        return [self._f(r) for r in self.q("SELECT * FROM findings WHERE lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(fingerprint) LIKE ? ORDER BY ts DESC LIMIT ?", (like, like, like, max(1, min(int(limit), 200))))]

    def timeline(self, agent, fingerprint):
        return [self._f(r) for r in self.q("SELECT * FROM findings WHERE agent=? AND fingerprint=? ORDER BY ts", (agent, fingerprint))]

    # ---- research notes (Research Librarian)
    def research_add(self, topic, doc):
        return self.x("INSERT INTO research(topic,ts,doc) VALUES(?,?,?)", (topic, time.time(), json.dumps(doc))).lastrowid

    def research_list(self, topic=None, limit=30):
        sql, args = "SELECT * FROM research", []
        if topic: sql += " WHERE topic=?"; args.append(topic)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(max(1, min(int(limit), 200)))
        return [{**dict(r), "doc": json.loads(r["doc"])} for r in self.q(sql, args)]

    # ---- backlog tasks (Backlog Keeper): a task exists while it appears in a handoff document; when it disappears it is done
    def tasks_sync(self, doc, items):
        """items: [{'id','section','text','done'}] found in `doc` now. Returns (new, closed)."""
        now, new, closed = time.time(), 0, 0
        with self.lock:
            seen = set()
            for it in items:
                seen.add(it["id"])
                r = self.q("SELECT status FROM tasks WHERE id=?", (it["id"],))
                if not r:
                    self.x("INSERT INTO tasks(id,doc,section,text,first_seen,last_seen,status,closed_at) VALUES(?,?,?,?,?,?,?,?)", (it["id"], doc, it["section"], it["text"], now, now, "done" if it["done"] else "open", now if it["done"] else None))
                    new += 0 if it["done"] else 1
                else:
                    st = r[0]["status"]
                    if it["done"] and st == "open":
                        self.x("UPDATE tasks SET status='done', closed_at=?, last_seen=? WHERE id=?", (now, now, it["id"])); closed += 1
                    else:
                        self.x("UPDATE tasks SET last_seen=?, section=? WHERE id=?", (now, it["section"], it["id"]))
            for r in self.q("SELECT id FROM tasks WHERE doc=? AND status='open'", (doc,)):
                if r["id"] not in seen:
                    self.x("UPDATE tasks SET status='done', closed_at=? WHERE id=?", (now, r["id"])); closed += 1
        return new, closed

    def tasks_list(self, status=None, limit=300):
        sql, args = "SELECT * FROM tasks", []
        if status: sql += " WHERE status=?"; args.append(status)
        sql += " ORDER BY first_seen LIMIT ?"; args.append(max(1, min(int(limit), 1000)))
        return [dict(r) for r in self.q(sql, args)]

    def task_dismiss(self, tid):
        return self.x("UPDATE tasks SET status='dismissed', closed_at=? WHERE id=? AND status='open'", (time.time(), tid)).rowcount

    # ---- job tracker (Job and Career Scout). The Scout never submits anything: statuses other than 'found'/'drafted' are set by the owner.
    @staticmethod
    def _j(r):
        d = dict(r)
        for k in ("matched", "gaps", "drafts"):
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "drafts" else None)
        return d

    def job_upsert(self, j):
        now = time.time()
        with self.lock:
            r = self.q("SELECT id FROM jobs WHERE id=?", (j["id"],))
            if r:
                self.x("UPDATE jobs SET score=?, matched=?, gaps=? WHERE id=?", (j["score"], json.dumps(j["matched"]), json.dumps(j["gaps"]), j["id"]))
                return False
            self.x("INSERT INTO jobs(id,source,title,company,url,location,posted,score,matched,gaps,first_seen,status_ts,snippet) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (j["id"], j["source"], j["title"][:160], j["company"][:100], j["url"], j["location"][:100], j["posted"], j["score"], json.dumps(j["matched"]), json.dumps(j["gaps"]), now, now, j.get("snippet", "")[:600]))
            return True

    def jobs_list(self, status=None, limit=100):
        sql, args = "SELECT * FROM jobs", []
        if status: sql += " WHERE status=?"; args.append(status)
        sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        return [self._j(r) for r in self.q(sql, args)]

    def job_get(self, jid):
        r = self.q("SELECT * FROM jobs WHERE id=?", (jid,))
        return self._j(r[0]) if r else None

    def job_update(self, jid, **kw):
        cols = {"status", "notes", "followup", "drafts", "want_draft"}
        sets, args = [], []
        for k, v in kw.items():
            if k not in cols: raise ValueError(k)
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k == "drafts" and v is not None else v)
        if "status" in kw: sets.append("status_ts=?"); args.append(time.time())
        self.x(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, jid))
        return self.job_get(jid)

    def jobs_wipe(self):
        self.x("DELETE FROM jobs")

    # ---- small key/value memory for agents (baselines of INFRASTRUCTURE config; never identifying data, which has a 24 h rule)
    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated", (k, json.dumps(v), time.time()))

    # ---- team projects: a goal, the plan (tasks handed to agents), the shared thread, the result
    @staticmethod
    def _p(r):
        d = dict(r)
        d["agents"] = json.loads(d["agents"] or "[]")
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d

    def project_create(self, title, goal, origin="owner", ref="", agents=None, approve_plan=False):
        pid, now = uuid.uuid4().hex[:10], time.time()
        self.x("INSERT INTO projects(id,title,goal,status,origin,ref,agents,created,updated,approve_plan) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (pid, title[:120], goal[:1500], "planning", origin, ref[:80], json.dumps(agents or []), now, now, 1 if approve_plan else 0))
        return pid

    def project_get(self, pid):
        r = self.q("SELECT * FROM projects WHERE id=?", (pid,))
        return self._p(r[0]) if r else None

    def project_list(self, limit=40):
        return [self._p(r) for r in self.q("SELECT * FROM projects ORDER BY updated DESC LIMIT ?", (max(1, min(int(limit), 200)),))]

    def project_update(self, pid, **kw):
        cols = {"status", "title", "result", "approve_plan"}
        sets, args = ["updated=?"], [time.time()]
        for k, v in kw.items():
            if k not in cols: raise ValueError(k)
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k == "result" and v is not None else v)
        self.x(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*args, pid))
        return self.project_get(pid)

    def msg_add(self, pid, author, kind, text, data=None):
        mid = self.x("INSERT INTO project_msgs(project_id,ts,author,kind,text,data) VALUES(?,?,?,?,?,?)", (pid, time.time(), author, kind, text[:4000], json.dumps(data) if data is not None else None)).lastrowid
        self.x("UPDATE projects SET updated=? WHERE id=?", (time.time(), pid))
        return mid

    def msgs(self, pid, after=0):
        return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in self.q("SELECT * FROM project_msgs WHERE project_id=? AND id>? ORDER BY id", (pid, after))]

    def task_add(self, pid, agent, skill, args, why, source="rules", requested_by="commander", depth=0, needs_ok=False):
        n = (self.q("SELECT COALESCE(MAX(n),0) m FROM project_tasks WHERE project_id=?", (pid,))[0]["m"]) + 1
        self.x("INSERT INTO project_tasks(project_id,n,agent,skill,args,why,source,requested_by,depth,needs_ok,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (pid, n, agent, skill, json.dumps(args or {}), why[:300], source, requested_by, depth, 1 if needs_ok else 0, "waiting_ok" if needs_ok else "pending"))
        return n

    @staticmethod
    def _t(r):
        d = dict(r)
        d["args"] = json.loads(d["args"] or "{}"); d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d

    def tasks(self, pid, status=None):
        sql, args = "SELECT * FROM project_tasks WHERE project_id=?", [pid]
        if status: sql += " AND status=?"; args.append(status)
        return [self._t(r) for r in self.q(sql + " ORDER BY n", args)]

    def task_update(self, pid, n, **kw):
        cols = {"status", "result", "run_id", "started", "finished", "needs_ok"}
        sets, args = [], []
        for k, v in kw.items():
            if k not in cols: raise ValueError(k)
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k == "result" and v is not None else v)
        self.x(f"UPDATE project_tasks SET {', '.join(sets)} WHERE project_id=? AND n=?", (*args, pid, n))

    # ---- outside model log (what was sent, what came back, what it cost): kept 14 days so the owner can always check
    def frontier_add(self, purpose, backend, model, tin, tout, cost, ok, error, sent, reply):
        return self.x("INSERT INTO frontier_log(ts,purpose,backend,model,tin,tout,cost,ok,error,sent,reply) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), purpose, backend, model, tin, tout, cost, 1 if ok else 0, (error or "")[:300] or None, sent[:12000], (reply or "")[:6000])).lastrowid

    def frontier_recent(self, limit=30):
        return [dict(r) for r in self.q("SELECT * FROM frontier_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),))]

    def frontier_stats(self, since, backend=None):
        sql = "SELECT COUNT(*) n, COALESCE(SUM(cost),0) cost, COALESCE(SUM(tin),0) tin, COALESCE(SUM(tout),0) tout FROM frontier_log WHERE ts>=? AND ok=1"
        args = [since]
        if backend: sql += " AND backend=?"; args.append(backend)
        return dict(self.q(sql, args)[0])

    # ---- actions the team carried out (or tried): every step, the state before and after, and the rollback result
    @staticmethod
    def _act(r):
        d = dict(r)
        for k in ("params", "prechecks", "before", "after", "calls", "verify", "outside"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def action_create(self, action, params, finding_id=None, project_id=None, prechecks=None):
        return self.x("INSERT INTO actions(finding_id,project_id,action,params,status,created,prechecks) VALUES(?,?,?,?,?,?,?)",
                      (finding_id, project_id, action, json.dumps(params), "approved", time.time(), json.dumps(prechecks) if prechecks is not None else None)).lastrowid

    def action_get(self, aid):
        r = self.q("SELECT * FROM actions WHERE id=?", (aid,))
        return self._act(r[0]) if r else None

    def action_update(self, aid, **kw):
        cols = {"status", "started", "finished", "prechecks", "before", "after", "calls", "verify", "note", "outside"}
        sets, args = [], []
        for k, v in kw.items():
            if k not in cols: raise ValueError(k)
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k in ("prechecks", "before", "after", "calls", "verify", "outside") and v is not None else v)
        self.x(f"UPDATE actions SET {', '.join(sets)} WHERE id=?", (*args, aid))

    def action_list(self, limit=50):
        return [self._act(r) for r in self.q("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),))]

    def actions_since(self, since):
        return self.q("SELECT COUNT(*) n FROM actions WHERE created>=? AND status NOT IN ('blocked')", (since,))[0]["n"]

    # ---- signals: the shared blackboard. Weak observations (below a finding's threshold) that other agents can read. One row per (agent, entity, kind).
    def signal_add(self, agent, entity, kind, strength, text, run_id=None):
        now = time.time()
        self.x("INSERT INTO signals(agent,entity,kind,first_ts,ts,strength,text,run_id) VALUES(?,?,?,?,?,?,?,?) "
               "ON CONFLICT(agent,entity,kind) DO UPDATE SET ts=excluded.ts, strength=excluded.strength, text=excluded.text, run_id=excluded.run_id, "
               "count=count+1, first_ts=CASE WHEN excluded.ts-signals.ts>86400 THEN excluded.ts ELSE signals.first_ts END",
               (agent, entity, kind, now, now, max(0.0, min(1.0, float(strength))), str(text)[:300], run_id))

    def signals_since(self, since, entity=None):
        sql, args = "SELECT * FROM signals WHERE ts>=?", [since]
        if entity: sql += " AND entity=?"; args.append(entity)
        return [dict(r) for r in self.q(sql + " ORDER BY ts DESC LIMIT 1000", args)]

    # ---- trace
    def trace_add(self, run_id, kind, ref, detail, ms):
        self.x("INSERT INTO trace(run_id,ts,kind,ref,detail,ms) VALUES(?,?,?,?,?,?)", (run_id, time.time(), kind, ref, detail, ms))

    def trace_for_run(self, rid):
        return [dict(r) for r in self.q("SELECT ts, kind, ref, detail, ms FROM trace WHERE run_id=? ORDER BY id", (rid,))]

    # ---- maintenance windows: the owner says 'I am working on X until Y'; matching findings and signals are held back
    @staticmethod
    def _m(r):
        d = dict(r)
        d["scope"], d["agents"] = json.loads(d["scope"] or "[]"), json.loads(d["agents"] or "[]")
        return d

    def maint_add(self, start, end, scope, agents, pattern, note):
        return self.x("INSERT INTO maintenance(created,start,end,scope,agents,pattern,note) VALUES(?,?,?,?,?,?,?)",
                      (time.time(), start, end, json.dumps(scope), json.dumps(agents), pattern, note[:300])).lastrowid

    def maint_list(self, active_only=False, now=None):
        now = now or time.time()
        sql = "SELECT * FROM maintenance WHERE cancelled=0 AND end>=?" if active_only else "SELECT * FROM maintenance ORDER BY start DESC LIMIT 100"
        return [self._m(r) for r in self.q(sql, (now,) if active_only else ())]

    def maint_cancel(self, mid):
        return self.x("UPDATE maintenance SET cancelled=1 WHERE id=? AND cancelled=0", (mid,)).rowcount

    def maint_hit(self, mid):
        self.x("UPDATE maintenance SET hits=hits+1 WHERE id=?", (mid,))

    # ---- LLM usage
    def usage_add(self, agent, model, pt, ct, ms, priority, ok, error=None, run_id=None):
        self.x("INSERT INTO usage(ts,agent,model,prompt_tokens,completion_tokens,ms,priority,ok,error,run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (time.time(), agent, model, pt, ct, ms, priority, 1 if ok else 0, (error or "")[:300] or None, run_id))

    def usage_for_run(self, rid):
        return [dict(r) for r in self.q("SELECT ts, agent, model, prompt_tokens, completion_tokens, ms, ok, error FROM usage WHERE run_id=? ORDER BY ts", (rid,))]

    def llm_health(self, since):
        r = self.q("SELECT COUNT(*) n, COALESCE(SUM(1-ok),0) bad, MAX(CASE WHEN ok=1 THEN ts END) last_ok FROM usage WHERE ts>=?", (since,))[0]
        return {"calls": r["n"], "errors": r["bad"], "last_ok": r["last_ok"]}

    def tokens_today(self, agent, since):
        r = self.q("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t FROM usage WHERE agent=? AND ts>=?", (agent, since))
        return r[0]["t"]

    def usage_summary(self, since):
        return [dict(r) for r in self.q("SELECT agent, COUNT(*) calls, SUM(prompt_tokens+completion_tokens) tokens, ROUND(AVG(ms)) avg_ms, SUM(1-ok) errors FROM usage WHERE ts>=? GROUP BY agent", (since,))]

    # ---- crew chat (break room, stand-ups, recaps). Text is scrubbed before it is stored; kept `chat.keep_days`.
    @staticmethod
    def _c(r):
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d.get("data") else None
        return d

    def chat_add(self, thread, author, text, to_agent="", kind="say", ref="", data=None, ts=None):
        cur = self.x("INSERT INTO chat(ts, thread, author, to_agent, kind, text, ref, data) VALUES(?,?,?,?,?,?,?,?)",
                     (ts or time.time(), thread, author, to_agent or "", kind, scrub(text)[:600], ref or "", json.dumps(data) if data is not None else None))
        return cur.lastrowid

    def chat_list(self, thread=None, after=0, limit=80, before=None, since=None):
        sql, args = "SELECT * FROM chat WHERE id > ?", [after]
        if thread:
            sql += " AND thread = ?"; args.append(thread)
        if before:
            sql += " AND id < ?"; args.append(before)
        if since:
            sql += " AND ts >= ?"; args.append(since)
        rows = self.q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [self._c(r) for r in reversed(rows)]

    def chat_get(self, mid):
        r = self.q("SELECT * FROM chat WHERE id=?", (mid,))
        return self._c(r[0]) if r else None

    def chat_set_data(self, mid, data):
        self.x("UPDATE chat SET data=? WHERE id=?", (json.dumps(data), mid))

    def chat_threads(self, limit=30):
        return [dict(r) for r in self.q("SELECT thread, count(*) AS n, max(ts) AS last, min(ts) AS first FROM chat GROUP BY thread ORDER BY last DESC LIMIT ?", (limit,))]

    def chat_count(self, since, thread=None):
        if thread:
            return self.q("SELECT count(*) FROM chat WHERE ts >= ? AND thread = ?", (since, thread))[0][0]
        return self.q("SELECT count(*) FROM chat WHERE ts >= ?", (since,))[0][0]

    def chat_purge(self, keep_s):
        return self.x("DELETE FROM chat WHERE ts < ?", (time.time() - keep_s,)).rowcount

    # ---- missions: goal + MOP + rollback, approved once by the owner, carried out by the runner
    @staticmethod
    def _mission(r):
        d = dict(r)
        for k in ("plan", "review", "run", "result", "history", "trail", "owner"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        d["history"] = d["history"] or []
        d["trail"] = d["trail"] or []
        d["attempt"] = d.get("attempt") or 1
        d["ticket"] = f"PRX-{d['id']:04d}"
        return d

    def mission_add(self, title, goal, origin="owner", ref=""):
        now = time.time()
        return self.x("INSERT INTO missions(ts, updated, title, goal, origin, ref, status) VALUES(?,?,?,?,?,?,?)", (now, now, title[:140], goal[:1500], origin, ref, "planning")).lastrowid

    def mission_get(self, mid):
        r = self.q("SELECT * FROM missions WHERE id=?", (mid,))
        return self._mission(r[0]) if r else None

    def mission_list(self, limit=40):
        return [self._mission(r) for r in self.q("SELECT * FROM missions ORDER BY updated DESC LIMIT ?", (limit,))]

    def mission_update(self, mid, **kw):
        sets, args = [], []
        for k, v in kw.items():
            sets.append(f"{k}=?"); args.append(json.dumps(v) if k in ("plan", "review", "run", "result", "history", "trail", "owner") and v is not None else v)
        self.x(f"UPDATE missions SET {', '.join(sets)}, updated=? WHERE id=?", (*args, time.time(), mid))
        return self.mission_get(mid)

