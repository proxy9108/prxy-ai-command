// The Office: every agent is a pixel character at a desk. All art is drawn here with rectangles (our own sprites; nothing copied).
// The view only renders state: it is fed the roster and the event stream and never calls agents.
const T = 16, W = 32, H = 22;                       // tile size and grid size (512 x 352 internal pixels)
const GROUP_ROOM = {security: "sec", operations: "ops", personal: "per", council: "cnc", housekeeping: "hsk", test: "hsk"};
const ROOMS = {                                     // interior rectangles in tiles + floor colours + label
  sec: {x: 1, y: 1, w: 14, h: 9, a: "#3a2024", b: "#341c20", label: "SECURITY OPS"},
  ops: {x: 16, y: 1, w: 15, h: 9, a: "#1f2c3d", b: "#1b2736", label: "OPERATIONS"},
  per: {x: 1, y: 11, w: 10, h: 10, a: "#3b2f20", b: "#352a1c", label: "PERSONAL"},
  cnc: {x: 12, y: 11, w: 9, h: 10, a: "#1d2340", b: "#191e38", label: "COUNCIL CHAMBER"},
  hsk: {x: 22, y: 11, w: 9, h: 10, a: "#26332c", b: "#212d26", label: "NEWSROOM + INTERN"},
};
const DESKS = {                                     // agent -> desk tile (x, y of the left tile); the character sits behind it
  sentinel: [2, 3], dns_watcher: [6, 3], exposure_auditor: [10, 3], iot_watcher: [4, 7], ids_doctor: [9, 7],
  alert_triage: [17, 3], backup_warden: [21, 3], patch_scout: [25, 3], capacity_planner: [19, 7], config_drift: [24, 7],
  day_briefing: [2, 13], research_librarian: [6, 13], job_scout: [2, 17], backlog_keeper: [6, 17], media_curator: [4, 19],
  briefing_editor: [24, 14], watchdog: [24, 18], demo: [27, 18],
};
const DEFAULT_DESKS = JSON.parse(JSON.stringify(DESKS));
const COUNCIL_SEATS = {commander: [16.5, 14.4], critic: [13.6, 15.9], archivist: [19.4, 15.9], correlator: [16.5, 17.1]};   // pixel-ish positions (tile units)
const DOORS = [[15, 5], [5, 10], [13, 10], [18, 10], [24, 10], [11, 15], [21, 15]];
const SEV = {info: "#9a9a94", low: "#7fb0d8", medium: "#f6a04a", high: "#e06a5f", critical: "#ff3b30"};
const KIND = {reading: "…", thinking: "…", typing: "⌨", scanning: "◎"};

const R = (c, x, y, w, h, col) => { c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), w, h); };
const shade = (hex, f) => { const n = parseInt(hex.slice(1), 16); const m = (v) => Math.max(0, Math.min(255, Math.round(v * f))); return `rgb(${m(n >> 16)},${m((n >> 8) & 255)},${m(n & 255)})`; };

function roomAt(tx, ty) { for (const [k, r] of Object.entries(ROOMS)) if (tx >= r.x && tx < r.x + r.w && ty >= r.y && ty < r.y + r.h) return k; return null; }

function buildGrid(skip) {                              // walkable[ty*W+tx]
  const g = new Array(W * H).fill(false);
  for (const r of Object.values(ROOMS)) for (let y = r.y; y < r.y + r.h; y++) for (let x = r.x; x < r.x + r.w; x++) g[y * W + x] = true;
  for (const [x, y] of DOORS) g[y * W + x] = true;
  const block = (x, y, w = 1, h = 1) => { for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) g[(y + j) * W + x + i] = false; };
  for (const [n, [dx, dy]] of Object.entries(DESKS)) if (n !== skip) block(dx, dy, 2, 1);
  block(14, 15, 5, 2);                              // council table
  for (const [x, y] of [[1, 1], [14, 1], [16, 1], [30, 1], [1, 20], [10, 20], [12, 11], [20, 20], [22, 20], [30, 20]]) block(x, y);   // plants
  block(12, 1, 2, 1); block(26, 1, 2, 1); block(7, 11, 2, 1); block(26, 11, 3, 1);                                                    // rack, shelf-like furniture
  return g;
}

function bfs(grid, from, to) {
  const key = (x, y) => y * W + x, prev = new Map([[key(...from), null]]), q = [from];
  while (q.length) {
    const [x, y] = q.shift();
    if (x === to[0] && y === to[1]) { const path = []; let k = key(x, y); while (k != null) { path.unshift([k % W, Math.floor(k / W)]); k = prev.get(k); } return path; }
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= W || ny >= H || !grid[key(nx, ny)] || prev.has(key(nx, ny))) continue;
      prev.set(key(nx, ny), key(x, y)); q.push([nx, ny]);
    }
  }
  return null;
}

function drawStatic() {
  const cv = document.createElement("canvas"); cv.width = W * T; cv.height = H * T;
  const c = cv.getContext("2d"); c.imageSmoothingEnabled = false;
  R(c, 0, 0, W * T, H * T, "#0b0c0e");
  for (const r of Object.values(ROOMS)) for (let y = 0; y < r.h; y++) for (let x = 0; x < r.w; x++) R(c, (r.x + x) * T, (r.y + y) * T, T, T, (x + y) % 2 ? r.a : r.b);
  const wall = "#2c313a", wallTop = "#3d4452";
  const walls = [[0, 0, W, 1], [0, 10, W, 1], [0, 21, W, 1], [0, 0, 1, H], [31, 0, 1, H], [15, 0, 1, 11], [11, 10, 1, 12], [21, 10, 1, 12]];
  for (const [x, y, w, h] of walls) { R(c, x * T, y * T, w * T, h * T, wall); R(c, x * T, y * T, w * T, 4, wallTop); }
  for (const [x, y] of DOORS) { const r = roomAt(x, y + 1) || roomAt(x + 1, y) || "sec"; const rr = ROOMS[roomAt(x, y - 1) || roomAt(x - 1, y) || r]; R(c, x * T, y * T, T, T, rr ? rr.a : "#222"); R(c, x * T, y * T + 12, T, 4, "#171a20"); }
  for (const [k, r] of Object.entries(ROOMS)) { c.fillStyle = "#6b7280"; c.font = "bold 7px monospace"; c.textBaseline = "top"; c.fillText(r.label, r.x * T + 4, r.y * T - 9 + 2 + (k === "sec" || k === "ops" ? 0 : 0)); }
  // plants
  for (const [x, y] of [[1, 1], [14, 1], [16, 1], [30, 1], [1, 20], [10, 20], [12, 11], [20, 20], [22, 20], [30, 20]]) { R(c, x * T + 5, y * T + 10, 6, 5, "#7a4b2a"); R(c, x * T + 3, y * T + 3, 10, 8, "#2f8f4e"); R(c, x * T + 6, y * T + 1, 5, 6, "#3fae63"); }
  // server rack (operations), whiteboard (council), bookshelf (personal), coffee machine (newsroom)
  R(c, 12 * T + 1, 1 * T + 2, 30, 14, "#1a1d23"); for (let i = 0; i < 4; i++) { R(c, 12 * T + 3, T + 4 + i * 3, 26, 2, "#2a2f38"); R(c, 12 * T + 25 + (i % 2) * 2, T + 4 + i * 3, 2, 2, i % 2 ? "#5fbf7a" : "#f6a04a"); }
  R(c, 26 * T + 1, 1 * T + 2, 30, 14, "#1a1d23"); for (let i = 0; i < 4; i++) { R(c, 26 * T + 3, T + 4 + i * 3, 26, 2, "#2a2f38"); R(c, 26 * T + 5 + (i % 3) * 2, T + 4 + i * 3, 2, 2, i % 2 ? "#5fbf7a" : "#e06a5f"); }
  R(c, 7 * T + 1, 11 * T + 2, 30, 13, "#5a4128"); for (let i = 0; i < 7; i++) R(c, 7 * T + 3 + i * 4, 11 * T + 4, 3, 9, ["#b3261e", "#2a7f9e", "#d9a31c", "#3f7f4f", "#8e3b8e"][i % 5]);
  R(c, 26 * T + 1, 11 * T + 2, 46, 13, "#d8d8d2"); R(c, 26 * T + 3, 11 * T + 4, 42, 9, "#e8e8e2"); R(c, 26 * T + 6, 11 * T + 7, 14, 1, "#c9573a"); R(c, 26 * T + 6, 11 * T + 10, 24, 1, "#3a5fa8");
  R(c, 14 * T, 11 * T + 2, 80, 9, "#d8d8d2"); R(c, 14 * T + 2, 11 * T + 4, 76, 5, "#f4f2ee"); R(c, 14 * T + 6, 11 * T + 5, 30, 1, "#1c2b4a"); R(c, 14 * T + 6, 11 * T + 7, 50, 1, "#8a1c1c");
  // council table
  R(c, 14 * T, 15 * T + 2, 5 * T, 2 * T - 4, "#5a4128"); R(c, 14 * T + 2, 15 * T + 4, 5 * T - 4, 2 * T - 10, "#7a5a3a"); R(c, 14 * T + 4, 16 * T - 3, 5 * T - 8, 2, "#8c6a46");
  R(c, 15 * T + 6, 15 * T + 7, 10, 7, "#f4f2ee"); R(c, 17 * T + 2, 15 * T + 7, 10, 7, "#f4f2ee"); R(c, 16 * T + 8, 15 * T + 6, 4, 4, "#f6a04a");
  return cv;
}

function drawChar(c, ch, t, x, y, pose, alpha) {
  const blink = ((t + (ch.seed || 0) * 977) % 4300) < 130;            // x = centre, y = feet. pose: stand | walk | sit | sleep
  const L = ch.look; c.save(); c.globalAlpha = alpha;
  const bob = pose === "walk" ? (Math.floor(t / 130) % 2) : 0;
  const px = (dx, dy, w, h, col) => R(c, x + dx, y + dy - bob * 0, w, h, col);
  const sit = pose === "sit" || pose === "sleep";
  const top = sit ? -2 : 0;                                  // seated figures are shorter: the desk hides the legs
  if (!sit) {
    const step = pose === "walk" ? (Math.floor(t / 130) % 2) : 0;
    px(-3, -6, 2, 5 - step, "#2b2f3a"); px(1, -6, 2, 4 + step, "#2b2f3a");
    px(-3, -1 - step, 2, 1, "#111"); px(1, -1 + step - 1, 2, 1, "#111");
  }
  const by = sit ? -8 : -13;                                 // torso top
  px(-4, by, 8, 7, L.shirt); px(-4, by, 8, 1, shade(L.shirt, 1.2));
  const hands = pose === "sit" && ch.typing ? (Math.floor(t / 120) % 2) : 0;
  const sw = pose === "walk" ? (Math.floor(t / 130) % 2 ? 1 : -1) : 0;                     // arms swing opposite to each other while walking
  px(-6, by + 1 + sw, 2, 5, L.shirt); px(4, by + 1 - sw, 2, 5, L.shirt);
  px(-6, by + 6 + sw + (hands ? 1 : 0), 2, 2, L.skin); px(4, by + 6 - sw + (hands ? 0 : 1), 2, 2, L.skin);
  const hy = by - 7;                                         // head top
  const lookDown = pose === "sleep" ? 2 : 0;
  px(-3, hy + lookDown, 6, 7, L.skin);
  px(-3, hy + 7 + lookDown, 6, 1, shade(L.skin, 0.85));
  const hair = L.hair;
  if (L.style === "short") { px(-4, hy - 1 + lookDown, 8, 3, hair); px(-4, hy + 2 + lookDown, 1, 2, hair); px(3, hy + 2 + lookDown, 1, 2, hair); }
  if (L.style === "long") { px(-4, hy - 1 + lookDown, 8, 3, hair); px(-4, hy + 2 + lookDown, 2, 8, hair); px(2, hy + 2 + lookDown, 2, 8, hair); }
  if (L.style === "spiky") { px(-4, hy - 1 + lookDown, 8, 3, hair); for (const sx of [-3, -1, 1]) px(sx, hy - 3 + lookDown, 2, 2, hair); }
  if (L.style === "bun") { px(-4, hy - 1 + lookDown, 8, 3, hair); px(-1, hy - 4 + lookDown, 3, 3, hair); px(-4, hy + 2 + lookDown, 1, 3, hair); px(3, hy + 2 + lookDown, 1, 3, hair); }
  const ey = hy + 3 + lookDown;
  if (pose === "sleep" || blink) { px(-2, ey + 1, 2, 1, "#111"); px(1, ey + 1, 2, 1, "#111"); } else { px(-2, ey, 1, 2, "#111"); px(1, ey, 1, 2, "#111"); px(-1, ey + 3, 2, 1, shade(L.skin, 0.7)); }
  if (L.acc === "glasses") { px(-3, ey - 1, 3, 3, "rgba(0,0,0,0)"); c.fillStyle = "#111"; c.fillRect(Math.round(x - 3), Math.round(y + ey - 1), 6, 1); c.fillRect(Math.round(x - 3), Math.round(y + ey + 2), 6, 1); c.fillRect(Math.round(x - 3), Math.round(y + ey - 1), 1, 4); c.fillRect(Math.round(x), Math.round(y + ey - 1), 1, 4); c.fillRect(Math.round(x + 2), Math.round(y + ey - 1), 1, 4); }
  if (L.acc === "headset") { px(-4, hy - 1 + lookDown, 8, 1, "#222"); px(-5, hy + 2 + lookDown, 1, 4, "#222"); px(4, hy + 2 + lookDown, 1, 4, "#222"); px(3, hy + 6 + lookDown, 3, 1, "#f6a04a"); }
  if (L.acc === "cap") { px(-4, hy - 2 + lookDown, 8, 3, ch.accent || L.shirt); px(-5, hy + 1 + lookDown, 10, 1, shade(ch.accent || L.shirt, 0.7)); }
  if (L.acc === "tie") px(0, by + 1, 2, 5, "#b3261e");
  c.restore();
}

// A head-and-shoulders portrait of a crew member, drawn with the same sprite code as the Office (used by the chat and the org chart).
export function portrait(canvas, look, bg) {
  const c = canvas.getContext("2d"); canvas.width = 16; canvas.height = 16;      // tight crop: hair to chest
  c.clearRect(0, 0, 16, 16); if (bg) { c.fillStyle = bg; c.fillRect(0, 0, 16, 16); }
  if (!look) return canvas;
  drawChar(c, {look, seed: 0, accent: look.shirt}, 1000, 8, 24, "stand", 1);
  return canvas;
}

function bubble(c, text, x, y, col) {
  c.font = "bold 8px monospace"; c.textBaseline = "middle";
  const w = Math.max(14, Math.ceil(c.measureText(text).width) + 8), h = 12, bx = Math.round(Math.min(W * T - w - 2, Math.max(2, x - w / 2))), by = Math.round(y - h);
  R(c, bx + 1, by, w - 2, h, "#f4f2ee"); R(c, bx, by + 1, w, h - 2, "#f4f2ee"); R(c, Math.round(x) - 2, by + h, 4, 2, "#f4f2ee"); R(c, Math.round(x) - 1, by + h + 2, 2, 1, "#f4f2ee");
  c.fillStyle = col || "#1d1c19"; c.fillText(text, bx + 4, by + h / 2 + 1);
}

export function createOffice(canvas, { onSelect, onHover, layout, onLayout }) {
  const c = canvas.getContext("2d"); c.imageSmoothingEnabled = false;
  canvas.width = W * T; canvas.height = H * T;
  for (const [n, p] of Object.entries(layout || {})) if (DESKS[n] && Array.isArray(p)) DESKS[n] = [p[0] | 0, p[1] | 0];
  const stat = drawStatic();
  let grid = buildGrid(), edit = false, drag = null;
  const chars = new Map();
  let raf = 0, last = performance.now(), hover = null, meetingUntil = 0;

  function home(name, group) {
    if (DESKS[name]) { const [dx, dy] = DESKS[name]; return {x: dx * T + T, feet: dy * T + 9, seat: [dx, dy - 1], desk: [dx, dy], sit: true, room: GROUP_ROOM[group] || "hsk"}; }
    const s = COUNCIL_SEATS[name] || [16.5, 14.4];
    return {x: s[0] * T, feet: s[1] * T + 14, seat: [Math.floor(s[0]), Math.floor(s[1])], desk: null, sit: false, room: "cnc"};
  }

  function setRoster(list) {
    for (const a of list) {
      let ch = chars.get(a.name);
      if (!ch) { ch = {name: a.name, look: a.look, accent: a.look.shirt, h: home(a.name, a.group), mode: "home", pos: null, path: [], timer: 8 + Math.random() * 30, bubble: null, seed: [...a.name].reduce((x, ch2) => x + ch2.charCodeAt(0), 0) % 7, typing: false, flash: 0, err: false}; ch.pos = {x: ch.h.x, y: ch.h.feet}; chars.set(a.name, ch); }
      Object.assign(ch, {title: a.title, group: a.group, session: a.session, built: a.built, state: a.state, enabled: a.enabled, open: a.open_findings || {}, summary: a.last_summary || ""});
      ch.typing = a.state === "working";
      if (a.state === "error") ch.err = true; else if (a.state === "idle" || a.state === "working") ch.err = false;
    }
  }

  const SPOTS = [[13, 15], [13, 16], [19, 15], [19, 16], [13, 14], [14, 14], [15, 14], [17, 14], [18, 14], [19, 14], [13, 17], [14, 17], [15, 17], [16, 17], [17, 17], [18, 17], [19, 17], [15, 13], [16, 13], [17, 13]];
  function meeting(only) {                          // everyone who is free (or just the named ones: a project huddle) walks to the council table, then goes back
    const free = SPOTS.filter(([x, y]) => grid[y * W + x]); let i = 0;
    for (const ch of chars.values()) {
      if (only && !only.includes(ch.name)) continue;
      if (!ch.built || !ch.enabled || (ch.typing && !only) || ch.err || !ch.h.sit || i >= free.length) continue;
      const from = ch.mode === "home" ? ch.h.seat : [Math.floor(ch.pos.x / T), Math.floor((ch.pos.y - 1) / T)];
      const path = bfs(grid, from, free[i++]);
      if (!path || path.length > 90) continue;
      ch.path = path.map(([x, y]) => ({x: x * T + 8, y: y * T + 15})); ch.mode = "meet-out"; ch.pos = {...ch.path[0]}; ch.timer = 18;
    }
    meetingUntil = performance.now() + 30000;
  }

  function say(ch, text, ms, col) { ch.bubble = {text, until: performance.now() + ms, col}; }

  function handleEvent(ev) {
    if (ev.type === "chat.message") {                 // the break room: the speaker says a short version of the line
      const sp = chars.get(ev.agent); if (!sp || !ev.text) return;
      const t = ev.text.replace(/\s+/g, " "); say(sp, t.length > 30 ? t.slice(0, 29) + "…" : t, 7000, ev.kind === "escalate" ? "#b3261e" : ev.kind === "nudge" ? "#c15f00" : null);
      return;
    }
    if (ev.type === "council.assign") { const cm = chars.get("commander"), to = chars.get(ev.to); if (cm) say(cm, "→ " + (to ? to.title : ev.to).slice(0, 18), 5000, "#1c2b4a"); if (to) say(to, "on it", 3000); return; }
    if (ev.type === "council.review") { const cr = chars.get("critic"), tg = chars.get(ev.target); if (cr) say(cr, ev.verdict === "disagree" ? "✗ I disagree" : ev.verdict === "caution" ? "! be careful" : "✓ fine by me", 5000, ev.verdict === "disagree" ? "#b3261e" : ev.verdict === "caution" ? "#c15f00" : "#2f8f4e"); if (tg && ev.verdict !== "agree") say(tg, "noted", 2500); if (ev.verdict === "disagree") { const cm = chars.get("commander"); if (cm) say(cm, "Critic disputes", 6000, "#b3261e"); } return; }
    if (ev.type === "project.plan") { const cm = chars.get("commander"); if (cm) say(cm, "new project", 8000, "#1c2b4a"); meeting(ev.agents || []); return; }
    if (ev.type === "project.message" && ev.kind === "request") { const ch3 = chars.get(ev.agent); if (ch3) say(ch3, "@ colleague", 4000, "#1c2b4a"); return; }
    if (ev.type === "project.done") { const cm = chars.get("commander"); if (cm) say(cm, ev.status === "done" ? "team done" : ev.status, 8000, ev.status === "done" ? "#2f8f4e" : "#b3261e"); return; }
    if (ev.type === "council.incident") { const co = chars.get("correlator"); if (co) say(co, "⚠ incident " + (ev.host || "").split(".").slice(-2).join("."), 9000, "#b3261e"); return; }
    if (ev.type === "council.debate") { const ch2 = chars.get(ev.agent); if (ch2) say(ch2, ({escalate: "▲ ", watch: "◉ ", dismiss: "▽ "}[ev.stance] || "") + (ev.stance || ""), 6000, ev.stance === "escalate" ? "#b3261e" : ev.stance === "dismiss" ? "#2f8f4e" : "#c15f00"); return; }
    if (ev.type === "council.verdict") { const cm = chars.get("commander"); if (cm) say(cm, "verdict: " + ev.verdict, 9000, ev.verdict === "escalate" ? "#b3261e" : "#1c2b4a"); return; }
    if (ev.type === "council.briefing") { const cm = chars.get("commander"); if (cm) say(cm, "briefing ready", 8000, "#2f8f4e"); meeting(); return; }
    const ch = chars.get(ev.agent); if (!ch) return;
    if (ev.type === "agent.started") { ch.typing = true; ch.state = "working"; say(ch, "on it", 2000); }
    else if (ev.type === "agent.working") { ch.typing = true; say(ch, (KIND[ev.kind] || "…") + " " + (ev.detail || ev.kind || "").slice(0, 22), 4000); }
    else if (ev.type === "finding.created") { say(ch, "! " + ev.severity, 9000, SEV[ev.severity]); ch.flash = performance.now() + 9000; ch.flashCol = SEV[ev.severity]; }
    else if (ev.type === "finding.updated") { say(ch, "↻ x" + ev.count, 3000, SEV[ev.severity]); }
    else if (ev.type === "agent.finished") { ch.typing = false; ch.state = "idle"; ch.err = false; say(ch, "✓ done", 2500, "#2f8f4e"); }
    else if (ev.type === "agent.error") { ch.typing = false; ch.state = "error"; ch.err = true; say(ch, "✗ " + (ev.summary || "error").slice(0, 24), 8000, "#b3261e"); }
  }

  function update(dt, now) {
    for (const ch of chars.values()) {
      if (!ch.built || !ch.enabled || ch.typing || ch.err) { if (ch.mode !== "home") { ch.mode = "home"; ch.path = []; ch.pos = {x: ch.h.x, y: ch.h.feet}; } continue; }
      if (ch.mode === "home") {
        ch.timer -= dt; if (ch.timer > 0 || !ch.h.sit) continue;
        const room = ROOMS[ch.h.room], tiles = [];
        for (let y = room.y; y < room.y + room.h; y++) for (let x = room.x; x < room.x + room.w; x++) if (grid[y * W + x]) tiles.push([x, y]);
        const to = tiles[Math.floor(Math.random() * tiles.length)], path = bfs(grid, ch.h.seat, to);
        if (!path || path.length > 14) { ch.timer = 15; continue; }
        ch.path = path.map(([x, y]) => ({x: x * T + 8, y: y * T + 15})); ch.mode = "out"; ch.pos = {...ch.path[0]};
      } else if (ch.mode === "meeting") {
        ch.timer -= dt;
        if (ch.timer <= 0) { const cur = [Math.floor(ch.pos.x / T), Math.floor((ch.pos.y - 1) / T)], path = bfs(grid, cur, ch.h.seat); ch.path = (path || [ch.h.seat]).map(([x, y]) => ({x: x * T + 8, y: y * T + 15})); ch.mode = "meet-back"; }
      } else if (ch.mode === "out" || ch.mode === "back" || ch.mode === "meet-out" || ch.mode === "meet-back") {
        const tgt = ch.path[0];
        if (!tgt) { if (ch.mode === "meet-out") { ch.mode = "meeting"; ch.timer = 18; } else if (ch.mode === "out") { ch.mode = "pause"; ch.timer = 3 + Math.random() * 4; } else { ch.mode = "home"; ch.timer = 20 + Math.random() * 40; ch.pos = {x: ch.h.x, y: ch.h.feet}; } continue; }
        const dx = tgt.x - ch.pos.x, dy = tgt.y - ch.pos.y, d = Math.hypot(dx, dy), sp = 38 * dt;
        if (d <= sp) { ch.pos = {x: tgt.x, y: tgt.y}; ch.path.shift(); } else { ch.pos.x += dx / d * sp; ch.pos.y += dy / d * sp; ch.dir = dx; }
      } else if (ch.mode === "pause") {
        ch.timer -= dt; if (ch.timer <= 0) { const cur = [Math.floor(ch.pos.x / T), Math.floor((ch.pos.y - 1) / T)], path = bfs(grid, cur, ch.h.seat); ch.path = (path || [ch.h.seat]).map(([x, y]) => ({x: x * T + 8, y: y * T + 15})); ch.mode = "back"; }
      }
    }
  }

  function drawDesk(ch, t) {
    const [dx, dy] = ch.h.desk, x = dx * T, y = dy * T, active = ch.built && ch.enabled && (ch.typing || (ch.flash > performance.now()));
    const screen = ch.err ? "#e06a5f" : ch.typing ? ch.accent : "#2a2e36";
    R(c, x, y + 8, 32, 6, "#7a5a3a"); R(c, x, y + 8, 32, 1, "#8c6a46"); R(c, x, y + 14, 32, 2, "#4a3520");
    R(c, x + 10, y, 12, 8, "#23262d"); R(c, x + 11, y + 1, 10, 6, ch.built ? "#1a1d23" : "#15171a");
    if (active) { const p = 0.6 + 0.4 * Math.sin(t / 180); c.globalAlpha = p; R(c, x + 11, y + 1, 10, 6, screen); c.globalAlpha = 1; R(c, x + 12, y + 2, 4, 1, "#fff6"); R(c, x + 12, y + 4, 7, 1, "#fff4"); }
    else if (ch.err) R(c, x + 11, y + 1, 10, 6, "#e06a5f"); else R(c, x + 15, y + 3, 2, 2, ch.built && ch.enabled ? shade(ch.accent, 0.7) : "#333");
    R(c, x + 9, y + 9, 14, 2, "#3a3f48");
    R(c, x + 25, y + 9, 3, 4, "#d8d8d2"); R(c, x + 25, y + 8, 3, 1, "#f4f2ee");           // a mug
  }

  function frame(now) {
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    update(dt, now);
    c.drawImage(stat, 0, 0);
    const list = [...chars.values()].sort((a, b) => a.pos.y - b.pos.y);
    for (const ch of list) if (ch.h.desk) drawDesk(ch, now);
    const drawn = [];
    for (const ch of list) {
      const away = ch.mode !== "home";
      const alpha = ch.built ? (ch.enabled ? 1 : 0.55) : 0.3;
      const pose = away ? (ch.mode === "pause" || ch.mode === "meeting" ? "stand" : "walk") : (ch.h.desk ? ((!ch.built || !ch.enabled) ? "sleep" : "sit") : "sit");
      const wob = ch.flash > now ? Math.abs(Math.sin(now / 90)) * 2 : 0;
      drawChar(c, ch, now, ch.pos.x, ch.pos.y - wob, ch.h.desk && !away ? pose : (away ? pose : "sit"), alpha);
      if (ch.h.desk && !away) { /* the desk front hides the legs: redraw the desk front strip */ const [dx, dy] = ch.h.desk; R(c, dx * T, dy * T + 8, 32, 6, "#7a5a3a"); R(c, dx * T, dy * T + 8, 32, 1, "#8c6a46"); R(c, dx * T, dy * T + 14, 32, 2, "#4a3520"); R(c, dx * T + 9, dy * T + 9, 14, 2, "#3a3f48"); R(c, dx * T + 25, dy * T + 9, 3, 4, "#d8d8d2"); }
      if (!ch.built) { c.font = "bold 7px monospace"; c.textBaseline = "top"; c.fillStyle = "#9aa0aa"; c.fillText(ch.session, ch.pos.x - 6, ch.pos.y + 9); }
      if (!ch.built || !ch.enabled) { c.font = "bold 8px monospace"; c.fillStyle = "#9aa0aa"; c.textBaseline = "middle"; const z = Math.floor(now / 700) % 3; if (ch.built) c.fillText("z".repeat(z + 1), ch.pos.x + 7, ch.pos.y - 20 - z * 2); }
      drawn.push(ch);
    }
    const hr = new Date().getHours();                // night: the office dims and working monitors glow
    if (hr >= 22 || hr < 6) {
      c.globalAlpha = 0.3; R(c, 0, 0, W * T, H * T, "#0a0c24"); c.globalAlpha = 1;
      for (const ch of list) if (ch.h.desk && ch.built && ch.enabled && ch.typing) { const gx = ch.h.desk[0] * T + 16, gy = ch.h.desk[1] * T + 6, g = c.createRadialGradient(gx, gy, 2, gx, gy, 34); g.addColorStop(0, "rgba(246,160,74,.35)"); g.addColorStop(1, "rgba(246,160,74,0)"); c.fillStyle = g; c.fillRect(gx - 34, gy - 34, 68, 68); }
    }
    if (edit) {                                      // layout editor: tile grid, desk outlines, and the drop target while dragging
      c.globalAlpha = 0.15; c.fillStyle = "#fff"; for (let x = 0; x <= W; x++) c.fillRect(x * T, 0, 1, H * T); for (let y = 0; y <= H; y++) c.fillRect(0, y * T, W * T, 1); c.globalAlpha = 1;
      for (const ch of list) if (ch.h.desk) { c.strokeStyle = "#f6a04a"; c.strokeRect(ch.h.desk[0] * T + .5, ch.h.desk[1] * T - 15.5, 32, 31); }
      if (drag) { c.fillStyle = drag.ok ? "rgba(95,191,122,.45)" : "rgba(224,106,95,.45)"; c.fillRect(drag.tx * T, (drag.ty - 1) * T, 32, 32); }
    }
    for (const ch of drawn) {                      // bubbles and name tags on top of everything
      const b = ch.bubble && ch.bubble.until > now ? ch.bubble : null;
      const topY = ch.pos.y - (ch.h.desk && ch.mode === "home" ? 22 : 24);
      if (b) bubble(c, b.text, ch.pos.x, topY, b.col);
      else if (ch.err) bubble(c, "✗ error", ch.pos.x, topY, "#b3261e");
      else if (ch.built && ch.enabled) { const n = Object.entries(ch.open).filter(([k]) => k !== "info"); if (n.length) { const worst = ["critical", "high", "medium", "low"].find((k) => ch.open[k]); const tot = n.reduce((s, [, v]) => s + v, 0); R(c, ch.pos.x + 5, topY + 6, 9, 9, SEV[worst]); c.fillStyle = "#111"; c.font = "bold 7px monospace"; c.textBaseline = "middle"; c.fillText(String(tot), ch.pos.x + 7, topY + 11); } }
      if (hover === ch.name) { c.font = "bold 8px monospace"; const w = c.measureText(ch.title).width + 6; R(c, ch.pos.x - w / 2, ch.pos.y + 8, w, 11, "#000c"); c.fillStyle = "#fff"; c.textBaseline = "middle"; c.fillText(ch.title, ch.pos.x - w / 2 + 3, ch.pos.y + 14); }
    }
    raf = requestAnimationFrame(frame);
  }

  function hit(ev) {
    const r = canvas.getBoundingClientRect(), mx = (ev.clientX - r.left) * canvas.width / r.width, my = (ev.clientY - r.top) * canvas.height / r.height;
    let best = null;
    for (const ch of chars.values()) { const dx = Math.abs(mx - ch.pos.x), dy = my - (ch.pos.y - 11); if (dx < 11 && Math.abs(dy) < 15) best = ch; }
    return best;
  }
  const tile = (ev) => { const r = canvas.getBoundingClientRect(); return [Math.floor((ev.clientX - r.left) * canvas.width / r.width / T), Math.floor((ev.clientY - r.top) * canvas.height / r.height / T)]; };
  function deskAt(ev) { const [tx, ty] = tile(ev); for (const ch of chars.values()) if (ch.h.desk && ty >= ch.h.desk[1] - 1 && ty <= ch.h.desk[1] && (tx === ch.h.desk[0] || tx === ch.h.desk[0] + 1)) return ch; return null; }
  function valid(ch, nx, ny) {
    const room = ROOMS[ch.h.room]; if (!room) return false;
    if (nx < room.x || nx + 1 >= room.x + room.w || ny - 1 < room.y || ny >= room.y + room.h) return false;
    const g = buildGrid(ch.name); return g[ny * W + nx] && g[ny * W + nx + 1] && g[(ny - 1) * W + nx] && g[(ny - 1) * W + nx + 1];
  }
  canvas.addEventListener("mousedown", (e) => { if (!edit) return; const ch = deskAt(e); if (ch) { drag = {ch, tx: ch.h.desk[0], ty: ch.h.desk[1], ok: true}; e.preventDefault(); } });
  canvas.addEventListener("mouseup", () => { if (!edit || !drag) return; const {ch, tx, ty, ok} = drag; drag = null; if (!ok) return; DESKS[ch.name] = [tx, ty]; grid = buildGrid(); ch.h = home(ch.name, ch.group); ch.pos = {x: ch.h.x, y: ch.h.feet}; ch.mode = "home"; ch.path = []; onLayout && onLayout(overrides()); });
  const overrides = () => Object.fromEntries(Object.entries(DESKS).filter(([n, p]) => !DEFAULT_DESKS[n] || p[0] !== DEFAULT_DESKS[n][0] || p[1] !== DEFAULT_DESKS[n][1]));
  canvas.addEventListener("mousemove", (e) => { if (edit && drag) { const [tx, ty] = tile(e); drag.tx = tx; drag.ty = ty; drag.ok = valid(drag.ch, tx, ty); } const ch = hit(e); hover = ch ? ch.name : null; canvas.style.cursor = ch ? "pointer" : "default"; onHover && onHover(ch, e); });
  canvas.addEventListener("mouseleave", () => { hover = null; onHover && onHover(null); });
  canvas.addEventListener("click", (e) => { if (edit) return; const ch = hit(e); if (ch) onSelect(ch.name); });
  raf = requestAnimationFrame(frame);
  return { setRoster, handleEvent, destroy() { cancelAnimationFrame(raf); }, chars, meeting,
    setEdit(on) { edit = on; drag = null; canvas.style.cursor = on ? "move" : "default"; },
    resetLayout() { for (const [n, p] of Object.entries(DEFAULT_DESKS)) DESKS[n] = [...p]; grid = buildGrid(); for (const ch of chars.values()) { ch.h = home(ch.name, ch.group); ch.pos = {x: ch.h.x, y: ch.h.feet}; ch.mode = "home"; ch.path = []; } onLayout && onLayout({}); } };
}
