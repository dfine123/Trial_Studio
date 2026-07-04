// Design-review harness: serves the REAL static pages (app/static) with stubbed APIs +
// sample media so every page and state can be exercised/screenshotted with ZERO prod risk.
//   node tools/design_preview.cjs   →   http://localhost:4173
// Sample media is gitignored — regenerate into tmp/fixtures with ffmpeg:
//   ffmpeg -f lavfi -i "gradients=size=540x960:duration=4:c0=0x0f1512:c1=0x2a5c3f:c2=0x7bf1a8:nb_colors=3" \
//          -f lavfi -i "sine=frequency=180:duration=4" -c:v libx264 -pix_fmt yuv420p -c:a aac sample.mp4
//   ffmpeg -f lavfi -i "sine=frequency=220:duration=14.2" -q:a 6 sample.mp3
//   ffmpeg -f lavfi -i "gradients=size=320x200:duration=0.1:c0=0x14201a:c1=0x1e3a2a" -frames:v 1 thumb.jpg
// Missing fixtures just 404 (players render empty) — the UI itself still fully reviews.
const http = require('http');
const fs = require('fs');
const path = require('path');

const STATIC = path.join(__dirname, '..', 'app', 'static');
const FIXTURES = path.join(__dirname, '..', 'tmp', 'fixtures');
const PORT = 4173;

const PAGES = {
  '/': 'app.html', '/login': 'login.html', '/grade': 'grade.html',
  '/grade-reels': 'grade_reels.html', '/promote': 'promote.html', '/templates': 'templates.html',
};

const CAPS = [
  { text: 'everybody scared someone\'s gonna steal their idea till you ask them to say the "idea" out loud', mode: 'observational', primary_lever: 'shareability' },
  { text: 'wtf is "living below your means"... just go get better means', mode: 'anticope', primary_lever: 'comment_bait' },
  { text: 'POV: you and bro did it the smart way, got the degree, climbed to middle management, and got a plaque', mode: 'pov_frame', primary_lever: 'iykyk_decode' },
  { text: 'hang in there.\n\nthis is the highlight.', mode: 'deadpan', primary_lever: 'relatability' },
  { text: 'A 30 year old still living with his parents saying he\'s close with family is like a jobless dude saying he\'s his own boss', mode: 'equivalence', primary_lever: 'shareability' },
  { text: 'Nobody watched me journal every morning.\nNobody watched me cut out sugar.\nNobody watched me hit 10k', mode: 'list', primary_lever: 'relatability' },
  { text: 'how many signs does God gotta disguise as bad luck before you finally lock in?', mode: 'sincere', primary_lever: 'comment_bait' },
  { text: 'professional driver on a closed course. do not attempt.', mode: 'bit', primary_lever: 'iykyk_decode' },
];
const AUDIOS = [
  { id: 'a1', description: 'Drake-type extra-slowed rap — deadpan, self-aware degenerate-confession energy (vibe: late night)', bpm: 68, duration: 14.2 },
  { id: 'a2', description: 'ambient house — blunt-positive POV energy', bpm: 118, duration: 16.8 },
  { id: 'a3', description: 'slowed trap rap — ironic-flex', bpm: 74, duration: 12.4 },
  { id: 'a4', description: 'upbeat hiphop — bit energy', bpm: 96, duration: 11.0 },
  { id: 'a5', description: 'ethereal poppy house — destiny', bpm: 122, duration: 15.5 },
  { id: 'a6', description: 'super slowed intro — lock-in', bpm: 60, duration: 13.1 },
];
const CLIPS = [
  { id: 'c1', status: 'indexed', summary: 'Walking through a parking garage at night, hood up, phone in hand', vibe_tags: ['night', 'moody', 'solo-walk'], setting: 'garage', time_of_day: 'night', duration: 8, folder_id: 'f1' },
  { id: 'c2', status: 'indexed', summary: 'Mirror check in a dim gym, adjusting hoodie sleeves', vibe_tags: ['gym', 'mirror', 'lock-in'], setting: 'gym', time_of_day: 'evening', duration: 6, folder_id: 'f1' },
  { id: 'c3', status: 'indexed', summary: 'Laughing at a diner booth with the boys, fries on the table', vibe_tags: ['friends', 'candid', 'laughing'], setting: 'diner', time_of_day: 'night', duration: 11, folder_id: 'f2' },
  { id: 'c4', status: 'indexing', summary: 'IMG_2041.mov', vibe_tags: [], duration: 9 },
  { id: 'c5', status: 'indexed', summary: 'Driving POV, one hand on the wheel, streetlights streaking', vibe_tags: ['driving', 'night-drive', 'pov'], setting: 'car', time_of_day: 'night', duration: 14, folder_id: 'f2' },
  { id: 'c6', status: 'rejected', summary: 'clip_lowres.mp4', vibe_tags: [], rejection_reason: 'resolution below 720p', duration: 5 },
  { id: 'c7', status: 'indexed', summary: 'Counting cash on a bed, deadpan look up at the camera', vibe_tags: ['money', 'deadpan', 'flex'], setting: 'bedroom', time_of_day: 'day', duration: 7, folder_id: 'f1' },
  { id: 'c8', status: 'indexed', summary: 'Typing on a laptop in a dark room, monitor glow', vibe_tags: ['grind', 'late-night', 'laptop'], setting: 'desk', time_of_day: 'night', duration: 10 },
];
const REELS = [
  {
    reel_id: 'r1', reel_url: '/fixtures/sample.mp4', caption: CAPS[0].text,
    candidates: [ { text: CAPS[0].text, chosen: true }, { text: CAPS[1].text }, { text: CAPS[6].text } ],
    audio: { description: AUDIOS[0].description },
    clips: [ { summary: CLIPS[0].summary }, { summary: CLIPS[1].summary }, { summary: CLIPS[4].summary } ],
  },
  {
    reel_id: 'r2', reel_url: '/fixtures/sample.mp4', caption: CAPS[3].text,
    candidates: [ { text: CAPS[3].text, chosen: true }, { text: CAPS[5].text } ],
    audio: { description: AUDIOS[2].description },
    clips: [ { summary: CLIPS[2].summary }, { summary: CLIPS[7].summary } ],
  },
];

const API = {
  'GET /health': { ok: true, commit: 'preview0' },
  'POST /api/login': { ok: true },
  'POST /api/logout': { ok: true },
  'GET /api/profiles': [ { id: 'p1', name: 'Austin', active: true }, { id: 'p2', name: 'Spence' }, { id: 'p3', name: 'Check' } ],
  'POST /api/profiles': { id: 'p9' },
  'POST /api/profiles/active': { ok: true },
  'GET /api/voices': { voices: [ { profile_id: 'p1', label: 'Base', refs: 120, active: true }, { profile_id: 'p2', label: 'Spence', refs: 153, active: false } ] },
  'POST /api/voice': { ok: true },
  'GET /api/audios': AUDIOS,
  'GET /api/folders': [ { id: 'f1', name: 'Lock-in era', clips: 34, parent_id: null }, { id: 'f2', name: 'Night drives', clips: 18, parent_id: null }, { id: 'f3', name: 'Mirror talks', clips: 9, parent_id: 'f1' } ],
  'GET /api/clips/library': CLIPS,
  'GET /api/drive/status': { configured: true, service_account: 'treelz-ingest@treelz.iam.gserviceaccount.com', connections: [
    { connection_id: 'd1', folder_name: 'austin drops', clips: 215, imported_files: 230, rejected: 3, failed: 1, status: 'connected', last_synced_at: '2026-07-03' },
    { connection_id: 'd2', folder_name: 'july batch', clips: 12, imported_files: 40, rejected: 0, failed: 0, status: 'syncing', last_synced_at: null },
  ] },
  'POST /api/drive/connect': { ok: true },
  'GET /api/captions/stats': { total: 842, keeps: 212, kills: 301, off_voice: 44, best: 61 },
  'POST /api/captions/grade': { ok: true },
  'POST /api/captions/best': { ok: true },
  'POST /api/captions/generate': { candidates: CAPS },
  'GET /api/reels/pending': REELS,
  'POST /api/reels/grade': { ok: true },
  'POST /api/reels/validate': { ok: true, drive: { link: 'https://drive.google.com/drive/folders/example' } },
  'GET /api/corpus/promotable': { promotable: [
    { reel_id: 'r1', rating: 10, caption: 'A broke dude calling himself a minimalist is like a bald dude calling himself aerodynamic' },
    { reel_id: 'r2', rating: 9, caption: 'staying broke to avoid risk is like starving yourself so you never get food poisoning' },
  ] },
  'GET /api/templates': [ { id: 't1', name: 'relatable → flex', segments: 3, audio_id: 'a1' }, { id: 't2', name: 'decode bait', segments: 2, audio_id: 'a3' } ],
  'GET /api/refs/audit': { total_refs: 120, retired_found: 0 },
};

const MIME = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.mp4': 'video/mp4', '.mp3': 'audio/mpeg', '.jpg': 'image/jpeg', '.png': 'image/png' };

function send(res, code, body, type) {
  res.writeHead(code, { 'content-type': type || 'application/json', 'cache-control': 'no-store' });
  res.end(type ? body : JSON.stringify(body));
}
function file(res, p) {
  fs.readFile(p, (err, data) => {
    if (err) return send(res, 404, { detail: 'not found' });
    send(res, 200, data, MIME[path.extname(p).toLowerCase()] || 'application/octet-stream');
  });
}

http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname;
  if (PAGES[p]) return file(res, path.join(STATIC, PAGES[p]));
  if (p.startsWith('/assets/')) return file(res, path.join(STATIC, p.slice(8)));
  if (p.startsWith('/fixtures/')) return file(res, path.join(FIXTURES, path.basename(p)));
  if (/^\/api\/clips\/[^/]+\/thumb$/.test(p)) return file(res, path.join(FIXTURES, 'thumb.jpg'));
  if (/^\/api\/audio\/[^/]+\/beats$/.test(p)) {
    const beats = Array.from({ length: 28 }, (_, i) => +(i * 0.52).toFixed(2));
    return send(res, 200, { id: 'a1', duration: 14.2, file_url: '/fixtures/sample.mp3', beat_map: beats, beat_drop_ts: 6.24 });
  }
  if (/^\/api\/drive\/sync\//.test(p)) return send(res, 200, { ok: true });
  if (/^\/api\/templates\/[^/]+\/enrich$/.test(p)) return send(res, 200, { formula: {
    title: 'relatable → flex', formula: 'open on the low moment, cut to the win on the drop',
    caption_logic: 'slot 1 sets the trap in a sincere register; slot 2 flips it with the flex',
    reskin_rules: 'keep the arc, swap the domain to the creator\'s world',
    slots: [
      { slot_id: 's0', flexibility: 'low', locked_structure: 'sincere setup, no irony yet', variables: ['domain'], vary_when: 'creator has a stronger low-moment clip' },
      { slot_id: 's1', flexibility: 'high', locked_structure: 'the flip — flex lands ON the beat drop', variables: ['flex object', 'register'], vary_when: 'always — this is the personality slot' },
    ] } });
  if (/^\/api\/templates\/[^/]+\/instantiate$/.test(p)) {
    return setTimeout(() => send(res, 200, { reel_url: '/fixtures/sample.mp4', captions: { s0: 'they said the internet money wasn\'t real', s1: 'anyway.' }, segments: 3, duration: 12.4 }), 1200);
  }
  if (p === '/api/generate' && req.method === 'POST') {
    return setTimeout(() => send(res, 200, { reel_url: '/fixtures/sample.mp4', caption: CAPS[Math.floor(Math.random() * CAPS.length)].text, duration: 7.2, shots: 4 }), 2600);
  }
  const key = req.method + ' ' + p;
  if (API[key] !== undefined) return send(res, 200, API[key]);
  if (p.startsWith('/api/')) return send(res, 200, { ok: true });   // permissive default for the rest
  send(res, 404, { detail: 'not found' });
}).listen(PORT, () => console.log('design preview on http://localhost:' + PORT));
