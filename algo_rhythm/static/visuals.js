// algo-rhythm visuals: four living worlds inspired by teamLab, locked to the music, with an art kaleidoscope and
// borderless transitions. Plain Canvas 2D, no build step. live.html calls garden(canvas, big, touch) and feeds it with G.set({...}).
//   koi       (low)    Drawing on the water surface created by the dance of koi: luminous koi leave calligraphic light trails.
//   lanterns  (medium) Forest of resonating lamps: a beat lights a lamp and the light spreads lamp to lamp, then fades.
//   flowers   (high)   Flowers and people: flowers are born, bloom on beats, scatter their petals at phrase ends and wither.
//   holi      (max)    Holi in a universe of light: colour powder bursts in a field of hanging light points; a drop warps through it.
// Every element is born, blooms, scatters and withers; nothing is a loop. Beats come from /state "pulse" (beat times on the
// song's playhead) through a local clock that never runs backwards; without "pulse" the beat comes from the BPM, as before.
// Photosensitivity: no full-screen flashes, drops ramp in over 350 ms or more, brightness follows a smoothed loudness.
(() => {
"use strict";
const TAU = Math.PI * 2;
const ACC = {low: "117,163,255", medium: "45,212,191", high: "255,154,61", max: "255,90,103"};
const BEAT = {low: 1.3, medium: .95, high: .68, max: .5};            // seconds per beat when the song's BPM is unknown
const NEUTRAL = ["120,130,150", "90,100,125", "160,170,190"];
const LEVEL_WORLD = {low: "koi", medium: "lanterns", high: "flowers", max: "holi"};
const WORLD_LEVEL = {koi: "low", lanterns: "medium", flowers: "high", holi: "max"};
// Mood weather over every world (live.html sends the mood with G.set({mood})): its colours, how fast the world moves (pace),
// and what falls or rises. The world itself still follows the energy. Beats stay on the music's own clock whatever the pace.
const MOOD = {
  chill:      {pace: .75, c: ["94,234,212", "56,189,248", "167,243,208"]},   // slow ripples
  sad:        {pace: .6,  c: ["157,176,232", "96,112,170", "190,200,230"]}, // slow rain and drifting mist
  romantic:   {pace: .85, c: ["255,122,182", "255,175,205", "224,69,123"]}, // floating hearts and falling petals
  devotional: {pace: .8,  c: ["255,200,87", "242,140,40", "255,230,170"]},  // embers rising like diya light
  groove:     {pace: 1,   c: ["185,140,255", "45,212,191", "150,120,255"]}, // sparks on beats
  party:      {pace: 1.1, c: ["255,178,62", "255,95,162", "255,130,50"]},   // sparks on beats, confetti on downbeats
  peak:       {pace: 1.25, c: ["255,107,133", "255,43,214", "255,150,70"]}  // plus a soft ring from the centre per beat (3 a second at most)
};
const rnd = (a, b) => b === undefined ? Math.random() * a : a + Math.random() * (b - a);
const pick = a => a[Math.random() * a.length | 0];
const clamp = (v, a = 0, b = 1) => v < a ? a : v > b ? b : v;
const ease = p => { p = clamp(p); return p * p * (3 - 2 * p); };
const arr = c => c.split(",").map(Number);
const str = a => a.map(v => Math.round(v)).join(",");
const mixA = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);
const white = (c, k) => str(mixA(arr(c), [255, 255, 255], k));
const turnTo = (a, b) => Math.atan2(Math.sin(b - a), Math.cos(b - a));   // shortest signed turn from heading a to b
const calm = () => !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
const swell = (t, up, down) => t < 0 ? 0 : t < up ? ease(t / up) : Math.exp(-(t - up) / down);   // soft rise, slow fall

// ---- sprites: soft lights and petals are painted once per colour and stamped with drawImage (cheap on the GPU)
const cache = new Map();
function sprite(key, w, h, paint) {
  let s = cache.get(key);
  if (s) return s;
  if (cache.size > 320) cache.clear();
  s = document.createElement("canvas"); s.width = w; s.height = h;
  const x = s.getContext && s.getContext("2d");
  if (x) paint(x);
  cache.set(key, s);
  return s;
}
const q8 = c => c.split(",").map(v => (v >> 3) << 3).join(",");     // nearby colours share one sprite
const glow = c => { c = q8(c); return sprite("g" + c, 64, 64, x => {
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, `rgba(${white(c, .6)},1)`); g.addColorStop(.14, `rgba(${c},.62)`); g.addColorStop(.42, `rgba(${c},.15)`); g.addColorStop(1, `rgba(${c},0)`);
  x.fillStyle = g; x.fillRect(0, 0, 64, 64);
}); };
const petal = (c, c2) => { c = q8(c); c2 = q8(c2); return sprite("p" + c + "|" + c2, 96, 48, x => {
  const g = x.createLinearGradient(0, 0, 96, 0);
  g.addColorStop(0, `rgba(${white(c2, .55)},.95)`); g.addColorStop(.38, `rgba(${c},.82)`); g.addColorStop(1, `rgba(${c},.1)`);
  x.fillStyle = g; x.beginPath(); x.moveTo(0, 24); x.bezierCurveTo(26, -2, 76, 2, 96, 24); x.bezierCurveTo(76, 46, 26, 50, 0, 24); x.fill();
}); };
const heart = c => { c = q8(c); return sprite("h" + c, 64, 64, x => {
  x.fillStyle = `rgba(${white(c, .25)},.9)`; x.beginPath(); x.moveTo(32, 56);
  x.bezierCurveTo(2, 36, 6, 6, 32, 22); x.bezierCurveTo(58, 6, 62, 36, 32, 56); x.fill();
}); };
function spr(S, img, x, y, r, a) {
  if (a <= .004 || r <= .2) return;
  S.c.globalAlpha = a > 1 ? 1 : a;
  S.c.drawImage(img, x - r, y - r, r * 2, r * 2);
}

// ---- shared effects: ripple rings, bloom bursts, motes (sparkles, falling petals, rising light)
function ring(S, x, y, R, c, amp = .5, life = 2.4, delay = 0) { if (S.fx.rings.length < 60) S.fx.rings.push({x, y, R, c, amp, life, age: -delay}); }
function burst(S, x, y, R, c, c2) { if (S.fx.blooms.length < 12) S.fx.blooms.push({x, y, R, c, c2, n: 7 + (Math.random() * 3 | 0), rot: rnd(TAU), spin: rnd(-.3, .3), age: 0, life: 2.8}); }
function mote(S, o) {
  if (S.fx.motes.length < 700 * S.q) S.fx.motes.push(Object.assign({vx: 0, vy: 0, age: 0, life: 1, size: 2, drag: 1, grav: 0, kind: "dot", rot: 0, vr: 0}, o));
}
function fxStep(S, dt) {
  const F = S.fx;
  for (const r of F.rings) r.age += dt;
  for (const b of F.blooms) b.age += dt;
  for (const m of F.motes) {
    m.age += dt; const d = Math.exp(-m.drag * dt);
    m.vx *= d; m.vy = m.vy * d + m.grav * dt; m.x += m.vx * dt; m.y += m.vy * dt; m.rot += m.vr * dt;
  }
  F.rings = F.rings.filter(r => r.age < r.life); F.blooms = F.blooms.filter(b => b.age < b.life); F.motes = F.motes.filter(m => m.age < m.life);
  const n = S.treble * 26 * S.q * S.area * dt;                       // treble sets how many glints appear
  for (let i = 0; i < Math.floor(n) + (Math.random() < n % 1 ? 1 : 0); i++)
    mote(S, {x: rnd(S.w), y: rnd(S.h), life: rnd(.5, 1), size: rnd(1, 2.2) * S.u, c: pick(S.glints), drag: 0});
}
function fxDraw(S, a) {
  const c = S.c, F = S.fx;
  c.lineWidth = 1.5 * S.u;
  for (const r of F.rings) {
    if (r.age < 0) continue;
    const k = r.age / r.life, rad = r.R * (1 - (1 - k) ** 2.4), al = r.amp * (1 - k) ** 1.5 * a;
    c.strokeStyle = `rgb(${r.c})`;
    c.globalAlpha = clamp(al); c.beginPath(); c.arc(r.x, r.y, rad, 0, TAU); c.stroke();
    if (rad > 14) { c.globalAlpha = clamp(al * .45); c.beginPath(); c.arc(r.x, r.y, rad * .84, 0, TAU); c.stroke(); }
  }
  for (const b of F.blooms) {                                        // a big flower of light that opens, turns and fades
    const k = b.age / b.life, open = 1 - (1 - Math.min(1, k / .4)) ** 3, al = (k < .3 ? ease(k / .3) : 1 - (k - .3) / .7) * a;
    const len = b.R * open, P = petal(b.c, b.c2), P2 = petal(b.c2, b.c);
    c.save(); c.translate(b.x, b.y); c.rotate(b.rot + b.spin * b.age);
    c.globalAlpha = clamp(al * .85);
    for (let i = 0; i < b.n; i++) { c.rotate(TAU / b.n); c.drawImage(P, 0, -len * .21, len, len * .42); }
    c.rotate(Math.PI / b.n); c.globalAlpha = clamp(al * .7);
    for (let i = 0; i < b.n; i++) { c.rotate(TAU / b.n); c.drawImage(P2, 0, -len * .14, len * .6, len * .28); }
    c.restore();
    spr(S, glow(b.c2), b.x, b.y, len * .5, al * .8);
  }
  for (const m of F.motes) {
    const k = m.age / m.life;
    if (m.kind === "rain") {                                         // a thin slanted streak along its own motion
      c.globalAlpha = clamp(.4 * a * Math.min(1, m.age / .4)); c.strokeStyle = `rgb(${m.c})`; c.lineWidth = S.u;
      c.beginPath(); c.moveTo(m.x, m.y); c.lineTo(m.x - m.vx * .07, m.y - m.vy * .07); c.stroke();
    } else if (m.kind === "mist") spr(S, glow(m.c), m.x, m.y, m.size, Math.sin(Math.PI * k) * .1 * a);
    else if (m.kind === "heart") {
      const al = Math.sin(Math.PI * k) * .75 * a, x = m.x + Math.sin(m.age * 1.3 + m.rot) * 10 * S.u;
      spr(S, glow(m.c), x, m.y, m.size * 1.4, al * .5);
      c.globalAlpha = clamp(al); c.drawImage(heart(m.c), x - m.size / 2, m.y - m.size / 2, m.size, m.size);
    } else if (m.kind === "petal") {
      const al = clamp(m.age / .2) * (1 - k) ** 1.2 * a;
      c.save(); c.translate(m.x, m.y); c.rotate(m.rot); c.globalAlpha = clamp(al * .9);
      c.drawImage(petal(m.c, m.c2), 0, -m.size * .2, m.size, m.size * .4); c.restore();
    } else spr(S, glow(m.c), m.x, m.y, m.size * 3, Math.sin(Math.PI * k) * .9 * a);
  }
}

// ---- small shapes the migrating motes take on during a transition: a koi, a lamp, a petal, a point of light
function glyph(S, type, x, y, ang, sz, col, al) {
  if (al < .01) return;
  const c = S.c, u = S.u * sz;
  if (type === "koi") {
    spr(S, glow(col), x, y, 16 * u, al * .5);
    c.save(); c.translate(x, y); c.rotate(ang); c.globalAlpha = clamp(al); c.fillStyle = `rgb(${white(col, .3)})`;
    c.beginPath(); c.ellipse(-6 * u, 0, 9 * u, 3 * u, 0, 0, TAU); c.fill();
    c.beginPath(); c.moveTo(-13 * u, 0); c.lineTo(-20 * u, -4 * u); c.lineTo(-18 * u, 0); c.lineTo(-20 * u, 4 * u); c.fill(); c.restore();
  } else if (type === "lantern") {
    spr(S, glow(col), x, y, 26 * u, al * .7);
    c.globalAlpha = clamp(al); c.fillStyle = `rgb(${white(col, .45)})`; c.beginPath(); c.ellipse(x, y, 4.5 * u, 6.5 * u, 0, 0, TAU); c.fill();
  } else if (type === "petal") {
    c.save(); c.translate(x, y); c.rotate(ang); c.globalAlpha = clamp(al); c.drawImage(petal(col, white(col, .4)), -14 * u, -5 * u, 28 * u, 10 * u); c.restore();
  } else spr(S, glow(col), x, y, 9 * u, al);
}

// Each world: {name, glyph, pal, img, col(i), step(dt), draw(alpha), beat(strong), event(kind), anchors(), colorOf(o), tap(x, y)}.
// Elements keep their screen position in x, y (or ax, ay when x, y are 3-D), so a transition can find them.
function base(name, glyph, pal) {
  return {name, glyph, pal, img: null, key: pal.join("|"), col: i => pal[((i % pal.length) + pal.length) % pal.length],
    step() {}, draw() {}, beat() {}, event() {}, anchors: () => [], colorOf: () => pal[0], tap() {}};
}

// ---- low: koi swim across dark water, drawing light with their trails; ripples spread where they turn and on beats
function koiWorld(S, pal) {
  const W = base("koi", "koi", pal), F = [];
  const want = () => Math.round(clamp(4 + 7 * S.area, 4, 11) * (.55 + .45 * S.q));
  const born = (x = rnd(S.w * .1, S.w * .9), y = rnd(S.h * .1, S.h * .9)) => F.push({x, y, h: rnd(TAU), v: rnd(.75, 1.2), ph: rnd(TAU), ph2: 0,
    wob: rnd(TAU), ci: Math.random() * pal.length | 0, sz: rnd(.8, 1.35), age: 0, life: rnd(30, 70), tr: [], kick: 0, rip: -9, aim: null, dead: false, turn: 0});
  for (let i = 0; i < want(); i++) born();
  const pools = [0, 1, 2, 3].map(i => ({x: rnd(1), y: rnd(1), r: rnd(.3, .5), ci: i, ph: rnd(TAU)}));   // faint light deep in the water
  function dissolve(f) {
    for (let i = 0; i < 12; i++) mote(S, {x: f.x - Math.cos(f.h) * i * 2.5 * S.u, y: f.y - Math.sin(f.h) * i * 2.5 * S.u, vx: rnd(-14, 14) * S.u,
      vy: rnd(-14, 14) * S.u, life: rnd(1.4, 2.6), size: rnd(1.4, 2.6) * S.u, c: W.col(f.ci), drag: 1.2});
  }
  W.step = dt => {
    if (F.filter(f => !f.dead).length < want() && Math.random() < dt * .8) born();
    for (let i = F.length - 1; i >= 0; i--) {
      const f = F[i];
      if (f.dead) { f.tr.splice(0, 6); if (f.tr.length < 9) F.splice(i, 1); continue; }   // the trail drains away after the koi dissolves
      f.age += dt;
      if (f.age > f.life) { f.dead = true; dissolve(f); continue; }
      const m = 70 * S.u, out = f.x < m || f.x > S.w - m || f.y < m || f.y > S.h - m;
      let turn = Math.sin(f.age * .45 + f.wob) * .65 + Math.sin(f.age * 1.15 + f.ph) * .3;
      if (out) turn += turnTo(f.h, Math.atan2(S.h / 2 - f.y, S.w / 2 - f.x)) * 2.2;
      if (f.aim !== null) { const d = turnTo(f.h, f.aim); turn += d * 3.2; if (Math.abs(d) < .15) f.aim = null; }
      f.turn = turn; f.h += turn * dt;
      f.kick *= Math.exp(-dt / .7);
      const sp = 58 * S.u * f.v * (1 + f.kick * 1.8) * (.8 + .4 * S.rms);
      f.x += Math.cos(f.h) * sp * dt; f.y += Math.sin(f.h) * sp * dt;
      f.ph2 += dt * (5 + 7 * f.kick + 2 * f.v);
      if (Math.abs(turn) > 1.2 && f.age - f.rip > 2.5) { f.rip = f.age; ring(S, f.x, f.y, 70 * S.u * f.sz, W.col(f.ci), .32, 2.2); }
      const L = f.tr.length;
      if (!L || Math.hypot(f.x - f.tr[L - 3], f.y - f.tr[L - 2]) > 7 * S.u) {   // brush pressure: sharper turns draw a thicker stroke
        f.tr.push(f.x, f.y, .45 + Math.min(1, Math.abs(turn)) * .95);
        if (f.tr.length > 3 * 90) f.tr.splice(0, 3);
      }
    }
  };
  function ribbon(t, from, n, width, al, col) {                       // a tapered calligraphic stroke along trail points
    const c = S.c, N = n - from;
    if (N < 3) return;
    const L = [], R = [];
    for (let i = from; i < n; i++) {
      const a = Math.max(from, i - 1) * 3, b = Math.min(n - 1, i + 1) * 3, dx = t[b] - t[a], dy = t[b + 1] - t[a + 1], d = Math.hypot(dx, dy) || 1;
      const w = width * ((i - from + 1) / N) ** 1.4 * t[i * 3 + 2];
      L.push(t[i * 3] - dy / d * w, t[i * 3 + 1] + dx / d * w); R.push(t[i * 3] + dy / d * w, t[i * 3 + 1] - dx / d * w);
    }
    c.beginPath(); c.moveTo(L[0], L[1]);
    for (let i = 2; i < L.length; i += 2) c.lineTo(L[i], L[i + 1]);
    for (let i = R.length - 2; i >= 0; i -= 2) c.lineTo(R[i], R[i + 1]);
    c.closePath(); c.globalAlpha = clamp(al); c.fillStyle = `rgb(${col})`; c.fill();
  }
  function body(f, a) {
    const c = S.c, L = 52 * S.u * f.sz, n = 10, col = W.col(f.ci), spot = W.col(f.ci + 1), P = [];
    let x = f.x, y = f.y;
    for (let j = 0; j <= n; j++) {                                    // the spine bends with the tail beat and into turns
      const k = j / n, ang = f.h + Math.PI + Math.sin(f.ph2 - j * .7) * .5 * k * k - f.turn * .07 * j;
      if (j) { x += Math.cos(ang) * L / n; y += Math.sin(ang) * L / n; }
      P.push([x, y, ang, L * .17 * Math.sin(Math.PI * Math.min(1, .16 + k * .95)) * (1 - .5 * k) + .4 * S.u]);
    }
    spr(S, glow(col), f.x - Math.cos(f.h) * L * .3, f.y - Math.sin(f.h) * L * .3, L * 1.3, .5 * a);
    const side = (from, to, sc) => {
      const pts = [];
      for (let j = from; j <= to; j++) { const [px, py, an, w] = P[j], s = w * sc; pts.push([px + Math.sin(an) * s, py - Math.cos(an) * s]); }
      for (let j = to; j >= from; j--) { const [px, py, an, w] = P[j], s = w * sc; pts.push([px - Math.sin(an) * s, py + Math.cos(an) * s]); }
      c.beginPath(); c.moveTo(pts[0][0], pts[0][1]); for (const p of pts) c.lineTo(p[0], p[1]); c.closePath(); c.fill();
    };
    c.fillStyle = `rgb(${white(col, .25)})`; c.globalAlpha = clamp(.85 * a); side(0, n, 1);
    c.fillStyle = `rgb(${spot})`; c.globalAlpha = clamp(.55 * a); side(2, 6, .7);   // koi markings in the second colour
    const [tx, ty, ta] = P[n], fan = L * .3;                          // tail fin, swishing
    c.fillStyle = `rgb(${col})`; c.globalAlpha = clamp(.6 * a); c.beginPath(); c.moveTo(tx, ty);
    c.lineTo(tx + Math.cos(ta - .5) * fan, ty + Math.sin(ta - .5) * fan); c.lineTo(tx + Math.cos(ta) * fan * .6, ty + Math.sin(ta) * fan * .6);
    c.lineTo(tx + Math.cos(ta + .5) * fan, ty + Math.sin(ta + .5) * fan); c.closePath(); c.fill();
    spr(S, glow(white(col, .5)), f.x, f.y, L * .32, .7 * a);
  }
  W.draw = a => {
    for (const p of pools) spr(S, glow(W.col(p.ci)), (p.x + .06 * Math.sin(S.t * .05 + p.ph)) * S.w, (p.y + .06 * Math.cos(S.t * .04 + p.ph)) * S.h, p.r * Math.max(S.w, S.h), .07 * a);
    for (const f of F) {
      const n = f.tr.length / 3, col = W.col(f.ci), wd = 4.4 * S.u * f.sz;
      ribbon(f.tr, 0, n, wd * 3.4, .1 * a, col);                   // a wide soft wash, then the bright brush line
      ribbon(f.tr, 0, n, wd, .6 * a, white(col, .15));
    }
    for (const f of F) if (!f.dead) body(f, a * Math.min(1, f.age / 2.5) * clamp((f.life - f.age) / 1.5));
  };
  W.beat = strong => {
    const live = F.filter(f => !f.dead);
    if (!live.length) return;
    const f = pick(live);
    f.kick = strong ? 1.3 : .8; f.aim = f.h + (Math.random() < .5 ? -1 : 1) * rnd(.9, 1.8);
    ring(S, f.x, f.y, (strong ? 150 : 90) * S.u, W.col(f.ci), strong ? .5 : .36, strong ? 2.8 : 2.2);
    if (strong) for (const g of live) g.kick = Math.max(g.kick, .35);
  };
  const scatter = k => { for (const f of F) if (!f.dead) { f.aim = Math.atan2(f.y - S.h / 2, f.x - S.w / 2); f.kick = k; } };
  W.event = kind => {
    const cx = S.w / 2, cy = S.h / 2, big = Math.hypot(S.w, S.h);
    if (kind === "phrase") { ring(S, cx, cy, big * .4, W.col(0), .4, 3.4); scatter(.9); }
    if (kind === "hook") {
      const f = pick(F.filter(g => !g.dead)) || {x: cx, y: cy};
      burst(S, f.x, f.y, 160 * S.u, W.col(1), W.col(2));
      for (let i = 0; i < 3; i++) ring(S, f.x, f.y, (140 + 60 * i) * S.u, W.col(i), .45, 2.6, i * .22);
    }
    if (kind === "drop") { for (let i = 0; i < 5; i++) ring(S, cx, cy, big * .62, W.col(i), .55 - i * .06, 3.8, .1 + i * .3); scatter(1.6); }
  };
  W.anchors = () => F.filter(f => !f.dead);
  W.colorOf = f => W.col(f.ci);
  W.tap = (x, y) => { ring(S, x, y, 80 * S.u, W.col(F.length), .45, 2); if (F.length < 18) born(x, y); };
  return W;
}

// ---- medium: a forest of glass lamps in a dark mirrored room; a beat lights one and the light walks lamp to lamp
function lanternWorld(S, pal) {
  const W = base("lanterns", "lantern", pal), L = [], pend = [], cols = 10, rows = 6;
  for (let r = 0; r < rows; r++) for (let k = 0; k < cols; k++)
    L.push({gx: (k + .5) / cols * 2.3 - 1.15 + rnd(-.07, .07) + (r % 2 ? .1 : 0), gz: r / (rows - 1) * .95 + rnd(-.04, .04), gy: rnd(-.42, .14),
      ci: Math.random() * pal.length | 0, l: 0, hit: 0, wave: -1, ph: rnd(TAU), x: 0, y: 0, ry: 0, s: 1});
  L.sort((a, b) => b.gz - a.gz);                                      // far lamps first, so near ones paint over them
  const dist = (a, b) => Math.hypot(a.gx - b.gx, (a.gz - b.gz) * 1.4, (a.gy - b.gy) * .6);
  for (const o of L) { o.c = arr(W.col(o.ci)); o.tc = o.c.slice(); o.nb = L.filter(p => p !== o).sort((p, q) => dist(o, p) - dist(o, q)).slice(0, 3); }
  let wid = 0, hue = 0;
  const ignite = (o, s, c, delay = 0, id = ++wid) => pend.push({o, s, c, at: S.t + delay, id});
  W.step = dt => {
    for (let i = pend.length - 1; i >= 0; i--) {
      const p = pend[i];
      if (p.at > S.t) continue;
      pend.splice(i, 1);
      const o = p.o;
      if (o.wave === p.id) continue;                                 // each lamp answers a wave once
      o.wave = p.id; o.hit = Math.max(o.hit, p.s); o.tc = p.c;
      if (p.s > .22) for (const n of o.nb) pend.push({o: n, s: p.s * .8, c: p.c, at: S.t + rnd(.1, .17), id: p.id});
    }
    for (const o of L) {
      const rest = .16 + .06 * Math.sin(S.t * .35 + o.ph);
      if (o.hit > o.l) { o.l = Math.min(o.hit, o.l + dt * 5); if (o.l >= o.hit) o.hit = 0; }   // lights up over ~0.2 s, never a flash
      else o.l = rest + (o.l - rest) * Math.exp(-dt / 1.5);
      for (let k = 0; k < 3; k++) o.c[k] += (o.tc[k] - o.c[k]) * Math.min(1, dt * 2.5);
    }
  };
  W.draw = a => {
    const c = S.c, cam = Math.sin(S.t * .045) * .12, hz = S.h * .44, F = S.h * .6;
    for (const o of L) {
      const s = 1 / (.62 + o.gz * 1.3);
      o.s = s; o.x = S.w / 2 + (o.gx + cam * (1 - o.gz * .6)) * S.w * .48 * s; o.y = hz + o.gy * F * s; o.ry = hz + (.95 - o.gy) * F * s;
    }
    for (const o of L) if (o.ry < S.h + 60) spr(S, glow(str(o.c)), o.x, o.ry, (40 + 90 * o.l) * o.s * S.u, (.1 + .3 * o.l) * a);   // the mirror floor
    c.lineWidth = .8 * S.u;
    for (const o of L) {
      c.globalAlpha = clamp((.05 + .1 * o.l) * a); c.strokeStyle = `rgb(${str(o.c)})`;
      c.beginPath(); c.moveTo(o.x, o.y - 26 * o.s * S.u); c.lineTo(o.x, hz - .95 * F * o.s); c.stroke();
    }
    for (const o of L) {
      const s = o.s * S.u, col = str(o.c);
      const G = glow(col), bw = 20 * s, bh = 28 * s;
      spr(S, G, o.x, o.y, (50 + 130 * o.l) * s, (.18 + .62 * o.l) * a);                    // the light the lamp throws
      c.globalAlpha = clamp((.55 + .45 * o.l) * a); c.drawImage(G, o.x - bw * 1.6, o.y - bh * 1.5, bw * 3.2, bh * 3);   // the glass, lit from inside
      c.globalAlpha = clamp((.25 + .5 * o.l) * a); c.fillStyle = `rgb(${white(col, .3 + .5 * o.l)})`;
      c.beginPath(); c.ellipse(o.x, o.y + bh * .1, bw * .55, bh * .62, 0, 0, TAU); c.fill();
      spr(S, glow(white(col, .7)), o.x, o.y + bh * .15, bw * (.5 + .7 * o.l), (.3 + .7 * o.l) * a);
    }
  };
  const near = () => pick(L.filter(o => o.gz < .75 && Math.abs(o.x - S.w / 2) < S.w * .45)) || pick(L);
  const rise = (o, n) => { for (let i = 0; i < n; i++) mote(S, {x: o.x + rnd(-6, 6) * S.u, y: o.y, vx: rnd(-5, 5) * S.u, vy: -rnd(14, 36) * S.u * o.s,
    life: rnd(2.5, 4.5), size: rnd(1.6, 2.6) * o.s * S.u, c: white(str(o.c), .3), drag: .25}); };
  W.beat = strong => { hue++; ignite(near(), strong ? 1 : .78, arr(W.col(hue))); if (strong) ignite(near(), .9, arr(W.col(hue + 2))); };
  W.event = kind => {
    if (kind === "phrase") {                                          // a wave sweeps the room left to right; some lamps release light
      const id = ++wid, col = arr(W.col(++hue));
      for (const o of L) pend.push({o, s: .8, c: col, at: S.t + (o.gx + 1.2) * .5, id});
      for (const o of L) if (Math.random() < .35) rise(o, 2);
    }
    if (kind === "hook") { const o = near(); burst(S, o.x, o.y, 150 * S.u * Math.min(1.4, o.s), W.col(hue + 1), W.col(hue + 2)); for (let i = 0; i < 3; i++) ignite(pick(L), 1, arr(W.col(hue + i)), i * .25); }
    if (kind === "drop") {                                            // every lamp flares, staggered over 0.35 s
      const id = ++wid, col = arr(white(W.col(++hue), .2));
      for (const o of L) { pend.push({o, s: 1.25, c: col, at: S.t + rnd(0, .35), id}); rise(o, 1); }
    }
  };
  W.anchors = () => L;
  W.colorOf = o => str(o.c);
  W.tap = (x, y) => { let b = L[0]; for (const o of L) if (Math.hypot(o.x - x, o.y - y) < Math.hypot(b.x - x, b.y - y)) b = o; ignite(b, 1, arr(W.col(++hue))); };
  return W;
}

// ---- high: a field of flowers that is never the same twice: born, bloom on beats, scatter at phrase ends, wither
function flowerWorld(S, pal) {
  const W = base("flowers", "petal", pal), F = [];
  const want = () => Math.round(clamp(12 + 22 * S.area, 10, 34) * (.55 + .45 * S.q));
  const spot = () => {                                                 // somewhere with room: best of five random places
    let best = [S.w / 2, S.h / 2], bd = -1e9;
    for (let k = 0; k < 5; k++) {
      const x = rnd(S.w * .04, S.w * .96), y = rnd(S.h * .06, S.h * .94);
      let d = 1e9; for (const f of F) if (!f.gone) d = Math.min(d, Math.hypot(f.x - x, f.y - y) - f.R);
      if (d > bd) { bd = d; best = [x, y]; }
    }
    return best;
  };
  const grow = (x, y, R, open = 0, to = .2) => {
    if (x === undefined) [x, y] = spot();
    const f = {x, y, R: R || rnd(40, 95) * S.u, n: 5 + (Math.random() * 4 | 0), rot: rnd(TAU), spin: rnd(-.06, .06), ci: Math.random() * pal.length | 0,
      open, to, age: 0, life: rnd(9, 20), gone: 0, wr: rnd(.36, .52), sway: rnd(TAU)};
    F.push(f);
    return f;
  };
  for (let i = 0; i < want() * .8; i++) { const f = grow(); f.age = rnd(0, f.life * .7); f.open = f.to = rnd(.3, 1); }
  let rush = -9;
  function scatter(f) {
    f.gone = .001;
    const len = f.R * (.22 + .78 * f.open);
    for (let i = 0; i < f.n; i++) {
      const a = f.rot + (i + 1) * TAU / f.n, v = rnd(20, 60) * S.u;
      mote(S, {kind: "petal", x: f.x + Math.cos(a) * len * .4, y: f.y + Math.sin(a) * len * .4, vx: Math.cos(a) * v + 12 * S.u, vy: Math.sin(a) * v - 8 * S.u,
        grav: 16 * S.u, drag: 1.1, rot: a, vr: rnd(-1.6, 1.6), size: len, c: W.col(f.ci), c2: W.col(f.ci + 1), life: rnd(2.4, 4)});
    }
  }
  W.step = dt => {
    if (F.filter(f => !f.gone).length < want() && Math.random() < dt * 3) grow();
    for (let i = F.length - 1; i >= 0; i--) {
      const f = F[i];
      f.age += dt; f.rot += f.spin * dt;
      if (f.gone) { f.gone += dt; if (f.gone > 1.8) F.splice(i, 1); continue; }
      f.to = Math.min(1, f.to + dt * .05);
      f.open += (f.to - f.open) * Math.min(1, dt * (S.t < rush ? 5 : 2));
      if (f.age > f.life) scatter(f);
    }
  };
  W.draw = a => {
    const c = S.c;
    for (const f of F) {
      const al = a * Math.min(1, f.age / 1.4) * (f.gone ? Math.max(0, 1 - f.gone / 1.8) : 1);
      if (al < .01) continue;
      const len = f.R * (.22 + .78 * f.open), col = W.col(f.ci), col2 = W.col(f.ci + 1);
      spr(S, glow(col2), f.x, f.y, len * (f.gone ? .7 : 1.15), .3 * al);   // the flower's light; all that stays as it withers
      if (f.gone) continue;
      const w = len * f.wr, P = petal(col, col2), P2 = petal(col2, white(col, .3));
      c.save(); c.translate(f.x, f.y); c.rotate(f.rot + Math.sin(S.t * .6 + f.sway) * .04);
      c.globalAlpha = clamp(al * .9);
      for (let i = 0; i < f.n; i++) { c.rotate(TAU / f.n); c.drawImage(P, 0, -w / 2, len, w); }
      c.rotate(Math.PI / f.n); c.globalAlpha = clamp(al * .8);
      for (let i = 0; i < f.n; i++) { c.rotate(TAU / f.n); c.drawImage(P2, 0, -w * .3, len * .55, w * .6); }
      c.restore();
      spr(S, glow(white(col2, .5)), f.x, f.y, len * .2, .9 * al);
    }
  };
  W.beat = strong => {
    for (const f of F) if (!f.gone && Math.random() < (strong ? .8 : .45)) f.to = Math.min(1, f.to + (strong ? .35 : .2));
    grow();
    if (strong) grow(undefined, undefined, rnd(110, 150) * S.u);
  };
  W.event = kind => {
    if (kind === "phrase") for (const f of F) if (!f.gone && f.age > 2) scatter(f);
    if (kind === "hook") {
      const [x, y] = spot();
      burst(S, x, y, 180 * S.u, W.col(1), W.col(2));
      for (let i = 0; i < 7; i++) grow(x + Math.cos(i * TAU / 7) * 120 * S.u, y + Math.sin(i * TAU / 7) * 120 * S.u, rnd(30, 46) * S.u, .1, 1);
    }
    if (kind === "drop") {                                            // everything blooms at once
      rush = S.t + 1.5;
      for (const f of F) if (!f.gone) f.to = 1;
      for (let i = 0; i < want() * .6; i++) grow(undefined, undefined, undefined, .05, 1);
    }
  };
  W.anchors = () => F.filter(f => !f.gone);
  W.colorOf = f => W.col(f.ci);
  W.tap = (x, y) => { if (F.length < 60) grow(x, y, rnd(30, 50) * S.u, .1, 1); };
  return W;
}

// ---- max: hanging strands of light points rushing past; beats throw colour powder that paints them; a drop warps
function holiWorld(S, pal) {
  const W = base("holi", "point", pal), ST = [], B = [], PER = 20;
  const want = () => Math.round(clamp(115 * S.area, 40, 140) * (.5 + .5 * S.q));
  const FAR = 2.4;                                                    // strands are born this far away and pass the viewer at 0.07
  const strand = (z = rnd(.15, FAR)) => { const c = arr(W.col(Math.random() * pal.length | 0));
    return {x: rnd(-2.6, 2.6), z, ph: rnd(TAU), c, tc: c.slice(), top: rnd(-1.15, -.7), len: rnd(1.4, 2.2), ax: S.w / 2, ay: S.h / 2}; };
  for (let i = 0; i < want(); i++) ST.push(strand());
  let warpAt = -99, barrage = 0, warp = 0;
  const proj = () => Math.min(S.w, S.h) * .5;
  const powder = (x, y, big, col) => {
    col = col || W.col(Math.random() * pal.length | 0);
    const n = Math.round((18 + 22 * big) * (.6 + .4 * S.q)), R = (100 + 150 * big) * S.u, b = {x, y, c: col, age: 0, life: 2.8 + big, R, P: []};
    for (let i = 0; i < n; i++) { const a = rnd(TAU), v = rnd(.3, 1) * R * 2.2; b.P.push({x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 12 * S.u, s: rnd(.25, .6) * R, dust: Math.random() < .45}); }
    if (B.length > 22) B.shift();
    B.push(b);
    for (const s of ST) if (s.z < 1.6 && Math.abs(S.w / 2 + s.x / s.z * proj() - x) < R * 1.7) s.tc = arr(col);   // the powder paints nearby strands
  };
  W.step = dt => {
    const k = S.t - warpAt;
    warp = k < 0 ? 0 : k < .45 ? ease(k / .45) : k < 2.1 ? 1 : Math.max(0, 1 - (k - 2.1) / 1.8);
    const speed = .07 + .05 * S.rms + 2.2 * warp * warp;
    while (ST.length < want()) ST.push(strand(1));
    if (ST.length > want()) ST.length = want();
    for (const s of ST) {
      s.z -= speed * dt;
      if (s.z < .07) Object.assign(s, strand(FAR + s.z));               // passed the viewer: reborn far away
      for (let i = 0; i < 3; i++) s.c[i] += (s.tc[i] - s.c[i]) * Math.min(1, dt * 1.8);
    }
    if (warp > .3 && S.t - barrage > .2) { barrage = S.t; powder(rnd(S.w * .1, S.w * .9), rnd(S.h * .1, S.h * .9), .7); }
    for (const b of B) {
      b.age += dt; const d = Math.exp(-dt * 2.6);
      for (const p of b.P) { p.vx *= d; p.vy = p.vy * d - 4 * S.u * dt; p.x += p.vx * dt; p.y += p.vy * dt; }
    }
    while (B.length && B[0].age > B[0].life) B.shift();
  };
  const haze = [0, 1, 2].map(i => ({x: rnd(.2, .8), y: rnd(.2, .8), ci: i, ph: rnd(TAU)}));   // drifting clouds of colour in the dark
  W.draw = a => {
    const c = S.c, cx = S.w / 2, cy = S.h / 2, f = proj(), wp = warp;
    for (const h of haze) spr(S, glow(W.col(h.ci)), (h.x + .15 * Math.sin(S.t * .07 + h.ph)) * S.w, (h.y + .12 * Math.cos(S.t * .05 + h.ph)) * S.h, Math.max(S.w, S.h) * .4, .1 * a);
    ST.sort((p, q) => q.z - p.z);
    for (const s of ST) {
      const depth = clamp(1.25 - s.z * .42) * clamp((FAR - s.z) / .4), X = cx + s.x / s.z * f;
      s.ax = X; s.ay = cy + (s.top + s.len / 2) / s.z * f;
      if (X < -80 || X > S.w + 80 || depth < .01) continue;
      const col = str(s.c), G = glow(col), C = glow(white(col, .5)), r = (1.1 + 1.3 / s.z) * S.u;
      if (wp > .02) {                                                 // the warp: every point becomes a streak
        const z2 = s.z + .06 + .45 * wp, X2 = cx + s.x / z2 * f;
        c.beginPath();
        for (let k = 0; k < PER; k++) { const y = s.top + s.len * k / (PER - 1); c.moveTo(X, cy + y / s.z * f); c.lineTo(X2, cy + y / z2 * f); }
        c.strokeStyle = `rgb(${col})`; c.lineWidth = Math.max(.6, r * .55); c.globalAlpha = clamp(depth * wp * .75 * a); c.stroke();
      }
      for (let k = 0; k < PER; k++) {
        const Y = cy + (s.top + s.len * k / (PER - 1)) / s.z * f;
        if (Y < -40 || Y > S.h + 40) continue;
        const tw = depth * (.6 + .4 * Math.sin(S.t * 1.1 - k * .55 + s.ph)) * a;
        spr(S, G, X, Y, r * 6, tw * .7); spr(S, C, X, Y, r * 1.6, tw);   // a soft halo and a bright core per point
      }
    }
    for (const b of B) {
      const k = b.age / b.life, al = (k < .08 ? ease(k / .08) : (1 - k) ** 1.1) * a, G = glow(b.c);
      for (const p of b.P) spr(S, G, p.x, p.y, p.dust ? p.s * .12 : p.s * (1 + k * 1.4), al * (p.dust ? .9 : .3));
    }
  };
  W.beat = strong => powder(rnd(S.w * .15, S.w * .85), rnd(S.h * .2, S.h * .8), strong ? 1 : .45);
  W.event = kind => {
    const cx = S.w / 2, cy = S.h / 2, R = Math.min(S.w, S.h) * .32;
    if (kind === "phrase") for (let i = 0; i < 6; i++) powder(cx + Math.cos(i * TAU / 6) * R, cy + Math.sin(i * TAU / 6) * R, .6, W.col(i));
    if (kind === "hook") { burst(S, cx, cy, 200 * S.u, W.col(0), W.col(1)); for (let i = 0; i < 3; i++) powder(rnd(S.w * .2, S.w * .8), rnd(S.h * .25, S.h * .75), .8); }
    if (kind === "drop") { warpAt = S.t; powder(cx, cy, 1.3); }
  };
  W.anchors = () => ST.filter(s => s.z < 1.4);
  W.colorOf = s => str(s.c);
  W.tap = (x, y) => powder(x, y, .5);
  return W;
}
const WORLDS = {koi: koiWorld, lanterns: lanternWorld, flowers: flowerWorld, holi: holiWorld};

// ---- the art kaleidoscope: the song's image mirrored into a round mandala, painted once per image
const mandalas = new Map();
function mandala(img) {
  if (!img || !(img.naturalWidth || img.width)) return null;
  let m = mandalas.get(img);
  if (m) return m;
  const D = 720, R = D / 2, seg = 8, wedge = TAU / seg, iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height, sc = R * 1.5 / Math.min(iw, ih);
  m = document.createElement("canvas"); m.width = m.height = D;
  const x = m.getContext && m.getContext("2d");
  if (!x) return null;
  for (let i = 0; i < seg; i++) {
    x.save(); x.translate(R, R); x.rotate(i * wedge); if (i % 2) x.scale(1, -1);   // every other wedge mirrored, so the seams match
    x.beginPath(); x.moveTo(0, 0); x.arc(0, 0, R, -wedge / 2 - .01, wedge / 2 + .01); x.closePath(); x.clip();
    x.drawImage(img, -iw * sc * .25, -ih * sc / 2, iw * sc, ih * sc);
    x.restore();
  }
  x.globalCompositeOperation = "destination-in";                     // a soft round edge
  const g = x.createRadialGradient(R, R, 0, R, R, R);
  g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(.55, "rgba(0,0,0,.9)"); g.addColorStop(1, "rgba(0,0,0,0)");
  x.fillStyle = g; x.fillRect(0, 0, D, D);
  if (mandalas.size > 6) mandalas.delete(mandalas.keys().next().value);
  mandalas.set(img, m);
  return m;
}

// ---- the engine
function garden(canvas, big, touch) {
  const g = {energy: "medium", mood: null, pal: null, bpm: 0, clear: false, playing: true, pulse: null, kaleido: false, scene: null, mix: null,
    stats: {frames: 0, stills: 0, beats: 0, downbeats: 0, events: [], climax: "", world: null, mode: "bpm", pos: null, q: 1, motes: 0, mixKind: null},
    set(o) {
      const arrived = "pulse" in o && o.pulse !== g.pulse;            // the same object again (a redraw) is not a new reading
      Object.assign(g, o);
      if (!ctx) return;
      if (arrived) onPulse(g.pulse, performance.now());
      reconcile();
    },
    step: now => step(now)};
  let ctx = null;
  try { ctx = canvas && canvas.getContext && canvas.getContext("2d"); } catch (e) { ctx = null; }
  if (!ctx) return g;                                                 // no canvas support: the page works without the visual
  const S = {c: ctx, w: 1, h: 1, u: 1, area: 1, q: 1, t: 0, fx: {rings: [], blooms: [], motes: []}, glints: NEUTRAL,
    bass: 0, treble: .3, rms: .6, bright: 1, beatEnv: 0, downAt: -99, dropAt: -99, climax: 0};
  let cur = null, nxt = null, dpr = 1, dprCap = 2, raf = 0, prev = 0, times = [], lastBeat = -1, lastPlant = 0, echoAt = 0, stillSig = "", stillTimer = 0;
  const clk = {pos: null, last: 0, err: 0, cursor: 0};

  function resize() {
    dpr = Math.min(dprCap, devicePixelRatio || 1);
    const r = canvas.getBoundingClientRect();
    S.w = Math.max(1, r.width); S.h = Math.max(1, r.height);
    S.u = Math.max(.45, Math.min(S.w, S.h) / 900); S.area = clamp(S.w * S.h / (1440 * 900), .25, 1.3) * (big ? 1 : .8);
    canvas.width = Math.round(S.w * dpr); canvas.height = Math.round(S.h * dpr);
    if (calm()) { stillSig = ""; still(); }
  }
  const palOf = (pal, world) => [...(pal && pal.length ? pal : NEUTRAL).slice(0, 5), MOOD[g.mood] ? MOOD[g.mood].c[0] : ACC[WORLD_LEVEL[world]] || ACC.medium];
  const wanted = () => g.scene || {world: LEVEL_WORLD[g.energy] || "lanterns", pal: g.pal || NEUTRAL, img: null};
  function make(world, pal, img) {
    const w = (WORLDS[world] || lanternWorld)(S, palOf(pal, world));
    w.img = img || null;
    return w;
  }
  function begin(sc, kind, timed) {
    const w = make(sc.world, sc.pal, sc.img);
    nxt = {w, kind, timed, p: 0, target: timed ? 1 : 0, rate: 0, at: performance.now(), ending: false, motes: kind === "blend" ? migrate(cur, w) : []};
  }
  function finish() { cur = nxt.w; nxt = null; }
  // Which world is wanted, and is a transition needed? A mix drives one with its progress; any other change runs over 3.5 s.
  function reconcile() {
    if (calm()) return still();
    const sc = wanted(), m = g.mix;
    if (!cur || cur.placeholder) { cur = make(sc.world, sc.pal, sc.img); cur.placeholder = !g.scene; }   // remade (no transition) until the first song arrives
    if (m) {
      if (!nxt || nxt.timed) { if (nxt) finish(); begin(m, m.kind === "echo" ? "echo" : "blend", false); }
      const t = clamp(Number(m.p) || 0), now = performance.now();
      if (t !== nxt.target) { const dt = (now - nxt.at) / 1000; if (t > nxt.target && dt > .2) nxt.rate = Math.min(.5, (t - nxt.target) / dt); nxt.target = t; nxt.at = now; }
      nxt.w.img = m.img || nxt.w.img; cur.img = sc.img || cur.img;   // art that finished loading mid-mix
    } else if (nxt && !nxt.timed) nxt.ending = true;                  // the mix is over: run to the end, then swap
    else if (!nxt && (cur.name !== sc.world || cur.key !== palOf(sc.pal, sc.world).join("|"))) begin(sc, "blend", true);
    else (nxt ? nxt.w : cur).img = sc.img || null;
    S.glints = cur.pal;
    g.stats.world = nxt ? nxt.w.name : cur.name; g.stats.motes = nxt ? nxt.motes.length : 0; g.stats.mixKind = nxt ? nxt.kind : null;
  }
  const ax = o => o.ax !== undefined ? o.ax : o.x, ay = o => o.ay !== undefined ? o.ay : o.y;
  // Borderless: the outgoing world's elements become motes that fly, curving, to the incoming world's elements and take their shape
  function migrate(from, to) {
    const src = from.anchors().filter(o => isFinite(ax(o)) && isFinite(ay(o))), dst = to.anchors();
    if (!src.length || !dst.length) return [];
    const out = [], n = Math.round(Math.min(Math.max(src.length, 40), 90) * (.5 + .5 * S.q));
    for (let i = 0; i < n; i++) {
      const s = src[i % src.length], d = dst[Math.random() * dst.length | 0];
      out.push({sx: ax(s), sy: ay(s), d, c0: arr(from.colorOf(s)), c1: arr(to.colorOf(d)), bend: rnd(-.45, .45), delay: rnd(0, .3), g0: from.glyph, g1: to.glyph,
        sz: rnd(.8, 1.3), px: null, py: null});
    }
    return out;
  }
  function drawMotes(tr, a) {
    const c = S.c;
    c.lineWidth = 1.2 * S.u;
    for (const m of tr.motes) {
      const q = ease((tr.p - m.delay) / .65), vis = clamp(tr.p * 6) * clamp((1 - q) * 3) * a;
      if (vis < .01) continue;
      let dx = ax(m.d), dy = ay(m.d);
      if (!isFinite(dx) || !isFinite(dy)) { dx = S.w / 2; dy = S.h / 2; }
      const mx = (m.sx + dx) / 2 - (dy - m.sy) * m.bend, my = (m.sy + dy) / 2 + (dx - m.sx) * m.bend, r = 1 - q;
      const x = r * r * m.sx + 2 * r * q * mx + q * q * dx, y = r * r * m.sy + 2 * r * q * my + q * q * dy;
      const ang = Math.atan2(2 * r * (my - m.sy) + 2 * q * (dy - my), 2 * r * (mx - m.sx) + 2 * q * (dx - mx)), col = str(mixA(m.c0, m.c1, q));
      if (m.px !== null) { c.globalAlpha = clamp(vis * .6); c.strokeStyle = `rgb(${col})`; c.beginPath(); c.moveTo(m.px, m.py); c.lineTo(x, y); c.stroke(); }
      m.px = x; m.py = y;
      glyph(S, m.g0, x, y, ang, m.sz * 1.7, col, vis * (1 - q)); glyph(S, m.g1, x, y, ang, m.sz * 1.7, col, vis * q);
    }
  }

  // ---- the clock: the playhead from /state "pulse", advanced locally between readings and eased toward each new one
  function onPulse(p, now) {
    if (!p || !isFinite(p.server_s)) return;
    const local = clk.pos === null ? null : clk.pos + (g.playing ? Math.max(0, now - clk.last) / 1000 : 0);
    if (local === null || Math.abs(p.server_s - local) > 1.5) { clk.pos = clk.cursor = p.server_s; clk.last = now; clk.err = 0; }   // a seek or a new session
    else { clk.pos = local; clk.last = now; clk.err = p.server_s - local; }
  }
  const pulseOn = () => { const p = g.pulse; return !!(p && isFinite(p.server_s) && Array.isArray(p.beats) && p.beats.length && clk.pos !== null); };
  function beat(strong) {
    g.stats.beats++;
    if (strong) { g.stats.downbeats++; S.downAt = S.t; }
    S.beatEnv = Math.max(S.beatEnv, strong ? 1 : .6);
    cur.beat(strong); if (nxt) nxt.w.beat(strong);
    moodBeat(strong);
  }
  // ---- mood weather: a few cheap motes a second, scaled by the screen size and the frame-rate quality (S.q)
  let flashAt = -9, rippleAt = 0;
  const n = k => k * S.q * S.area;
  const spawn = (k, dt, f) => { const x = n(k) * dt; for (let i = 0; i < Math.floor(x) + (Math.random() < x % 1 ? 1 : 0); i++) f(); };
  function weather(dt) {
    const M = MOOD[g.mood];
    if (!M || !g.playing) return;
    const col = () => pick(M.c), u = S.u;
    if (g.mood === "sad") {
      spawn(45, dt, () => { const vy = rnd(130, 190) * u; mote(S, {kind: "rain", x: rnd(-.1, 1.1) * S.w, y: -20, vx: -.18 * vy, vy, life: (S.h + 40) / vy, c: col(), drag: 0}); });
      spawn(.5, dt, () => mote(S, {kind: "mist", x: rnd(S.w), y: rnd(S.h * .3, S.h), vx: rnd(-8, 8) * u, vy: -rnd(2, 6) * u, life: rnd(8, 12), size: rnd(140, 240) * u, c: col(), drag: 0}));
    } else if (g.mood === "romantic") {
      spawn(1.4, dt, () => mote(S, {kind: "heart", x: rnd(S.w * .05, S.w * .95), y: S.h + 20 * u, vy: -rnd(22, 40) * u, life: rnd(7, 11), size: rnd(24, 44) * u, rot: rnd(TAU), c: col(), drag: 0}));
      spawn(.9, dt, () => mote(S, {kind: "petal", x: rnd(S.w), y: -20, vx: rnd(4, 18) * u, vy: rnd(10, 22) * u, grav: 2 * u, drag: .1, rot: rnd(TAU), vr: rnd(-.8, .8),
        size: rnd(26, 40) * u, life: rnd(8, 12), c: col(), c2: col()}));
    } else if (g.mood === "chill") {
      if (S.t - rippleAt > 1.7) { rippleAt = S.t; ring(S, rnd(S.w * .1, S.w * .9), rnd(S.h * .15, S.h * .85), rnd(90, 160) * u, col(), .24, 4.5); }
    } else if (g.mood === "devotional") {
      spawn(6, dt, () => mote(S, {x: rnd(S.w), y: S.h + 10, vx: rnd(-4, 4) * u, vy: -rnd(18, 36) * u, life: rnd(5, 8), size: rnd(1.4, 2.6) * u, c: col(), drag: 0}));
    }
  }
  function sparks(x, y, k, M) { for (let i = 0; i < k; i++) { const a = rnd(TAU), v = rnd(60, 160) * S.u;
    mote(S, {x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: rnd(.5, .9), size: rnd(1.2, 2.2) * S.u, c: pick(M.c), drag: 2.5}); } }
  function confetti(k, M) { for (let i = 0; i < k; i++) mote(S, {kind: "petal", x: rnd(S.w), y: rnd(-30, S.h * .25), vx: rnd(-30, 30) * S.u, vy: rnd(-20, 30) * S.u,
    grav: 60 * S.u, drag: 1.2, rot: rnd(TAU), vr: rnd(-6, 6), size: rnd(16, 28) * S.u, life: rnd(1.8, 2.8), c: pick(M.c), c2: pick(M.c)}); }
  function moodBeat(strong) {
    const M = MOOD[g.mood];
    if (!M || !["groove", "party", "peak"].includes(g.mood)) return;
    sparks(rnd(S.w * .15, S.w * .85), rnd(S.h * .2, S.h * .8), Math.round(n(g.mood === "groove" ? 5 : 8)), M);
    if (strong && g.mood !== "groove") confetti(Math.round(n(g.mood === "peak" ? 22 : 14)), M);
    if (g.mood === "peak" && S.t - flashAt >= .34) {                 // a soft ring, never a full-screen flash
      flashAt = S.t; ring(S, S.w / 2, S.h / 2, Math.min(S.w, S.h) * (strong ? .55 : .38), pick(M.c), strong ? .45 : .3, .9);
    }
  }
  function event(kind) {
    g.stats.events.push(kind); if (g.stats.events.length > 50) g.stats.events.shift();
    if (kind === "drop") { S.dropAt = S.t; g.stats.climax = [cur, nxt && nxt.w].filter(Boolean).map(w => w.name).join(","); }
    cur.event(kind); if (nxt) nxt.w.event(kind);
  }
  function timing(now, dt) {
    const p = g.pulse;
    if (pulseOn()) {
      g.stats.mode = "pulse";
      const d = Math.max(0, (now - clk.last) / 1000);
      clk.last = now;
      if (!g.playing) return;                                         // paused: the clock holds
      const k = clamp(clk.err, -d * .5, d * .5);                      // ease out any error at up to half speed: never backwards
      clk.pos += d + k; clk.err -= k;
      const from = Math.max(clk.cursor, clk.pos - .25), to = clk.pos;   // a long gap (hidden tab) fires nothing old
      if (to > from) {
        const down = Array.isArray(p.downbeats) ? p.downbeats : [];
        for (const b of p.beats) if (b > from && b <= to) beat(down.some(x => Math.abs(x - b) < .03));
        for (const x of Array.isArray(p.drops) ? p.drops : []) if (x && x.t > from && x.t <= to) event(x.kind);
      }
      clk.cursor = Math.max(clk.cursor, to);
      g.stats.pos = clk.pos;
      return;
    }
    g.stats.mode = "bpm";                                             // no pulse: the beat from the BPM, as before
    const bpm = (p && p.bpm > 0 && p.bpm) || g.bpm, len = bpm ? 60 / bpm : BEAT[g.energy] || 1, bi = Math.floor(S.t / len);
    if (bi !== lastBeat) { lastBeat = bi; beat(bi % 4 === 0); }
  }
  function sample(a, i) {
    if (!Array.isArray(a) || !a.length || i < -2 || i > a.length + 2) return null;
    const j = clamp(i, 0, a.length - 1), lo = Math.floor(j), hi = Math.min(a.length - 1, lo + 1);
    return a[lo] + (a[hi] - a[lo]) * (j - lo);
  }
  function feel(dt) {                                                // loudness, bass and treble, smoothed so nothing flickers
    const e = pulseOn() && g.pulse.env, i = e && e.dt > 0 ? (clk.pos - e.t0) / e.dt : NaN;
    let rms = e ? sample(e.rms, i) : null, bass = e ? sample(e.bass, i) : null, tre = e ? sample(e.treble, i) : null;
    S.beatEnv *= Math.exp(-dt / .2);
    if (rms === null) rms = .62;
    if (bass === null) bass = S.beatEnv * .7;
    if (tre === null) tre = .25 + .1 * Math.max(0, ["low", "medium", "high", "max"].indexOf(WORLD_LEVEL[cur.name]));
    S.bass += (bass - S.bass) * Math.min(1, dt / (bass > S.bass ? .06 : .25));
    S.rms += (rms - S.rms) * Math.min(1, dt / .45);
    S.treble += (tre - S.treble) * Math.min(1, dt / .3);
    S.climax = swell(S.t - S.dropAt, .35, 2.5);
    S.bright = clamp(.72 + .42 * S.rms + .22 * S.climax, .5, 1.3);
  }
  function adapt(ms) {                                                // slow frames: fewer elements, then a lower resolution
    times.push(ms);
    if (times.length < 45) return;
    const avg = times.reduce((a, b) => a + b) / times.length;
    times = [];
    if (avg > 20) { if (S.q > .45) S.q *= .85; else if (dprCap > 1) { dprCap = 1; resize(); } }
    else if (avg < 14 && S.q < 1) S.q = Math.min(1, S.q * 1.05);
    g.stats.q = S.q;
  }
  function ground() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.globalCompositeOperation = "source-over"; ctx.globalAlpha = 1;
    if (g.clear) ctx.clearRect(0, 0, S.w, S.h);                       // the song's art shows through underneath
    else { ctx.fillStyle = "#050609"; ctx.fillRect(0, 0, S.w, S.h); }
  }
  function kaleido(img, a, spin) {
    const m = g.kaleido && a > .01 && mandala(img);
    if (!m) return;
    const kp = swell(S.t - S.downAt, .12, .5), D = Math.hypot(S.w, S.h) * .62 * (1 + .03 * kp);
    ctx.save(); ctx.translate(S.w / 2, S.h / 2); ctx.rotate(spin);
    ctx.globalAlpha = clamp(a * (.34 + .1 * kp + .08 * S.climax)); ctx.drawImage(m, -D / 2, -D / 2, D, D); ctx.restore();
  }
  function step(now) {
    const ms = prev ? now - prev : 16; prev = now;
    const dt = Math.min(.05, Math.max(0, ms) / 1000);
    S.t += dt; g.stats.frames++;
    adapt(ms);
    if (!cur) reconcile();
    timing(now, dt);
    feel(dt);
    if (nxt) {
      if (nxt.timed || nxt.ending) nxt.p = Math.min(1, nxt.p + dt / (nxt.timed ? 3.5 : .8));
      else { const aim = clamp(nxt.target + nxt.rate * Math.min(1.5, (now - nxt.at) / 1000)); nxt.p += (aim - nxt.p) * Math.min(1, dt * 4); }
      if (nxt.kind === "echo" && nxt.p < .85 && S.t - echoAt > .45) {   // an echo-out: rings spread from the centre as the old world fades
        echoAt = S.t; ring(S, S.w / 2, S.h / 2, Math.hypot(S.w, S.h) * .6, cur.col(Math.round(S.t * 3)), .5, 3);
      }
      if (nxt.p >= .999 && (nxt.timed || nxt.ending)) { finish(); reconcile(); }   // the wanted world may have moved on meanwhile
    }
    const pace = MOOD[g.mood] ? MOOD[g.mood].pace : 1;
    cur.step(dt * pace); if (nxt) nxt.w.step(dt * pace);
    weather(dt);
    fxStep(S, dt);
    ground();
    const sc = 1 + .022 * S.bass, shake = cur.name === "holi" ? S.bass * 2.2 * S.u : 0, cx = S.w / 2, cy = S.h / 2;
    const ox = Math.sin(S.t * 23) * shake, oy = Math.cos(S.t * 19) * shake;
    ctx.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * (cx - cx * sc + ox), dpr * (cy - cy * sc + oy));   // the bass breathes the scene
    ctx.globalCompositeOperation = "lighter";
    const e = nxt ? ease(nxt.p) : 0, echo = nxt && nxt.kind === "echo";
    const aCur = nxt ? (echo ? 1 - clamp(nxt.p * 1.25) : 1 - e) : 1, aNxt = nxt ? (echo ? ease((nxt.p - .4) / .6) : e) : 0;
    kaleido(cur.img, aCur, S.t * .03); if (nxt) kaleido(nxt.w.img, aNxt, S.t * .03);
    cur.draw(aCur * S.bright); if (nxt) nxt.w.draw(aNxt * S.bright);
    if (nxt && nxt.motes.length) drawMotes(nxt, S.bright);
    fxDraw(S, S.bright);
  }
  // Reduced motion: no animation loop at all; one calm frame per song, swapped with a slow fade
  function still() {
    if (!g.scene) return;                                             // nothing to show until the first /state
    const sc = wanted(), img = sc.img || null, sig = [sc.world, palOf(sc.pal, sc.world).join(), img && img.src, g.kaleido, g.clear, S.w, S.h].join("|");
    if (sig === stillSig) return;
    const first = !stillSig;
    stillSig = sig; g.stats.world = sc.world;
    clearTimeout(stillTimer);
    const paint = () => {
      g.stats.stills++;
      const w = make(sc.world, sc.pal, img), keep = S.t;
      cur = w;
      for (let i = 0; i < 90; i++) { S.t += .05; w.step(.05); if (i % 15 === 7) w.beat(i % 30 === 7); }
      S.fx = {rings: [], blooms: [], motes: []}; S.downAt = S.dropAt = -99; S.t = keep;
      ground(); ctx.globalCompositeOperation = "lighter";
      kaleido(img, 1, 0); w.draw(1);
      canvas.style.opacity = 1;
    };
    canvas.style.transition = "opacity 1.2s ease";
    if (first) paint();
    else { canvas.style.opacity = 0; stillTimer = setTimeout(paint, 1200); }
  }
  function loop(now) { raf = requestAnimationFrame(loop); step(now); }
  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas); else addEventListener("resize", resize);
  resize();
  if (touch) canvas.addEventListener("pointermove", plant), canvas.addEventListener("pointerdown", plant);
  function plant(e) {                                                 // a touch answers in the world's own language
    if (calm() || !cur || e.timeStamp - lastPlant < 140) return;
    lastPlant = e.timeStamp;
    const r = canvas.getBoundingClientRect();
    (nxt ? nxt.w : cur).tap(e.clientX - r.left, e.clientY - r.top);
  }
  document.addEventListener("visibilitychange", () => {               // pause when the tab is hidden
    cancelAnimationFrame(raf); raf = 0;
    if (!document.hidden && !calm()) { prev = 0; raf = requestAnimationFrame(loop); }
  });
  if (!calm()) raf = requestAnimationFrame(loop);
  return g;
}
Object.assign(window, {garden, LEVEL_WORLD, WORLD_LEVEL});
})();
