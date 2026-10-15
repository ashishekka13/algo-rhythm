// algo-rhythm player: this browser plays and mixes the party. The server plans each join and sends a recipe
// (algo_rhythm/recipe.py): "plays", each one source node with a time map (its playback rate), a gain curve and maybe a
// highpass sweep. This file only schedules them with the Web Audio API; it decides nothing about the music.
//
//   const P = new Player({report});   P.unlock() inside a click (browsers start audio only after a gesture)
//   P.update(state.engine)            on every state the server sends: new joins are scheduled, moved ones re-made
//   P.start(at) / P.pause() / P.resume() / P.skip() / P.reset()
//   P.now()                           music time: seconds since the party's first song, pauses not counted
//
// Music time -> AudioContext time: ctxTime = anchor.ctx + (music - anchor.music). Pausing suspends the context, so the
// clock and every scheduled node stop together and carry on where they were.
"use strict";

const SR = 44100;
const LEAD = 0.25;          // a node is never started closer than this to "now" (time to set it up)
const HORIZON = 20;         // plays starting within this many seconds are made into nodes; later ones wait
const SKIP_LEAD = 10;       // "skip to transition" lands this long before the join
const HP_Q = -3.0103;       // Web Audio's highpass Q is in dB: -3.01 dB = 1/sqrt(2), a Butterworth like the server's

// A soft limiter, the server's dj.soft_limit with its 0.97 output gain: under 0.9 untouched, above it squashed
// smoothly below 1. The shaper's input is halved first, so the curve covers -2..2.
function limiterCurve() {
  const n = 4096, c = new Float32Array(n), knee = 0.9;
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * 4 - 2, a = Math.abs(x);
    const y = a <= knee ? a : knee + (1 - knee) * Math.tanh((a - knee) / (1 - knee));
    c[i] = 0.97 * Math.sign(x) * y;
  }
  return c;
}

// Piecewise-linear lookup in [[t, v], ...], flat outside.
function lin(pts, t) {
  if (t <= pts[0][0]) return pts[0][1];
  for (let k = 1; k < pts.length; k++) {
    if (t <= pts[k][0]) {
      const [t0, v0] = pts[k - 1], [t1, v1] = pts[k];
      return t1 > t0 ? v0 + (v1 - v0) * (t - t0) / (t1 - t0) : v1;
    }
  }
  return pts[pts.length - 1][1];
}
// Exponential lookup (filter frequencies), flat outside.
function expo(pts, t) {
  if (t <= pts[0][0]) return pts[0][1];
  for (let k = 1; k < pts.length; k++) {
    if (t <= pts[k][0]) {
      const [t0, v0] = pts[k - 1], [t1, v1] = pts[k];
      return t1 > t0 ? v0 * Math.pow(v1 / v0, (t - t0) / (t1 - t0)) : v1;
    }
  }
  return pts[pts.length - 1][1];
}
// Song time at music time t from a play's "src" map; after the last point it goes on at the last slope.
function srcAt(src, t) {
  if (src.length === 1) return src[0][1] + (t - src[0][0]);
  if (t <= src[src.length - 1][0]) return lin(src, t);
  const [t0, s0] = src[src.length - 2], [t1, s1] = src[src.length - 1];
  return s1 + (t - t1) * (s1 - s0) / (t1 - t0);
}
// Playback rate on each stretch of the map: [[from t, rate], ...].
function rates(src) {
  if (src.length === 1) return [[src[0][0], 1]];
  const out = [];
  for (let k = 0; k + 1 < src.length; k++) out.push([src[k][0], (src[k + 1][1] - src[k][1]) / (src[k + 1][0] - src[k][0])]);
  return out;
}

class Player {
  constructor({report = () => {}, onError = () => {}, log = () => {}} = {}) {
    this.report = report; this.onError = onError; this.log = log;
    this.ctx = null; this.epoch = null; this.status = "idle";
    // the audio context exists only after a tap (unlock); songs fetched before that are decoded once it does
    this.ready = new Promise(ok => { this.onReady = ok; });
    this.anchor = {ctx: 0, music: 0}; this.pausedAt = 0;
    this.items = new Map();       // index -> item (with rid)
    this.media = {};              // file -> {mix, vocals}
    this.buffers = new Map();     // url -> {p: Promise, buf: AudioBuffer | null}
    this.nodes = new Map();       // rid -> [{play key, node, stopAt}]
    this.made = new Set();        // "rid:k" of plays already made into nodes
    this.timer = null;
  }

  // ---- set-up (inside a click)
  unlock() {
    if (this.ctx) { if (this.ctx.state === "suspended" && this.status !== "paused") this.ctx.resume(); return; }
    const AC = window.AudioContext || window.webkitAudioContext;
    let c;
    try { c = new AC({sampleRate: SR, latencyHint: "playback"}); }
    catch (e) { c = new AC(); }                   // a browser that cannot run at 44.1 kHz resamples every source itself
    this.attach(c);
    // a silent buffer started now unlocks audio on iOS
    const s = c.createBufferSource(); s.buffer = c.createBuffer(1, 1, c.sampleRate); s.connect(c.destination); s.start();
  }

  // the output chain on context c (an OfflineAudioContext in the tests)
  attach(c) {
    this.ctx = c;
    this.onReady(c);
    this.music = c.createGain();                 // every song goes here; the MC's duck is on it
    this.voice = c.createGain();                 // the MC's line: not ducked
    const pre = c.createGain(); pre.gain.value = 0.5;
    const shaper = c.createWaveShaper(); shaper.curve = limiterCurve(); shaper.oversample = "none";
    this.music.connect(pre); this.voice.connect(pre); pre.connect(shaper); shaper.connect(c.destination);
  }

  now() {
    if (!this.ctx || this.status === "idle") return 0;
    if (this.status === "paused") return this.pausedAt;
    return this.anchor.music + (this.ctx.currentTime - this.anchor.ctx);
  }
  ctxAt(music) { return this.anchor.ctx + (music - this.anchor.music); }

  // ---- media
  url(file, stem) {
    const m = this.media[file];
    return m ? (stem === "vocals" ? m.vocals : m.mix) : null;
  }
  load(url) {
    if (!url) return null;
    let b = this.buffers.get(url);
    if (!b) {
      b = {buf: null, p: null};
      b.p = fetch(url).then(r => { if (!r.ok) throw new Error(`${r.status} for ${url}`); return r.arrayBuffer(); })
        .then(data => this.ready.then(c => new Promise((ok, bad) => c.decodeAudioData(data, ok, bad))))
        .then(buf => { b.buf = buf; return buf; })
        .catch(e => { this.buffers.delete(url); this.onError(`Could not load audio: ${e.message || e}`); return null; });
      this.buffers.set(url, b);
    }
    return b;
  }
  playUrl(p) { return p.stem === "asset" ? p.url : this.url(p.file, p.stem); }
  // Only what the joins on hand still need: a decoded 4-minute stereo song is about 85 MB.
  evict() {
    const keep = new Set();
    for (const it of this.items.values()) for (const p of this.plays(it)) keep.add(this.playUrl(p));
    for (const u of [...this.buffers.keys()]) if (!keep.has(u)) this.buffers.delete(u);
  }

  // ---- the server's plan
  plays(it) {
    const out = it.join ? it.join.plays.map((p, k) => ({...p, key: `${it.rid}:j${k}`})) : [];
    const next = this.items.get(it.index + 1);
    out.push({...it.body, key: `${it.rid}:body`, end: next ? next.start_s : it.body.end});
    return out;
  }

  update(engine) {
    if (!engine) return;
    Object.assign(this.media, engine.media || {});
    if (this.epoch !== null && engine.epoch !== this.epoch) this.reset();   // started over on another screen
    this.epoch = engine.epoch;
    for (const it of engine.items) {
      const old = this.items.get(it.index);
      if (old && old.rid === it.rid) continue;
      if (old) this.unmake(old.rid);              // a request took this place: the old join goes
      this.items.set(it.index, it);
      for (const p of this.plays(it)) this.load(this.playUrl(p));   // fetch now: it plays a minute from now at the earliest
      const prev = this.items.get(it.index - 1);  // the song before now knows where it ends
      if (prev) this.restop(prev);
    }
    const lo = Math.min(...engine.items.map(i => i.index));
    for (const k of [...this.items.keys()]) if (k < lo - 1) { this.unmake(this.items.get(k).rid); this.items.delete(k); }
    this.evict();
    if (this.status === "playing") this.schedule();
  }

  // a body's end moved (the next join arrived or changed): tell its node
  restop(it) {
    const next = this.items.get(it.index + 1);
    for (const n of this.nodes.get(it.rid) || []) {
      if (n.key.endsWith(":body") && next) { try { n.node.stop(this.ctxAt(next.start_s)); } catch (e) {} }
    }
  }

  unmake(rid) {
    for (const n of this.nodes.get(rid) || []) { try { n.node.stop(); } catch (e) {} n.out.disconnect(); }
    this.nodes.delete(rid);
    for (const k of [...this.made]) if (k.startsWith(rid + ":")) this.made.delete(k);
  }

  stopAll() {
    for (const rid of [...this.nodes.keys()]) this.unmake(rid);
    this.made.clear();
    if (this.ctx) { this.music.gain.cancelScheduledValues(0); this.music.gain.value = 1; }
  }

  // Make nodes for every play that starts within HORIZON and has not been made yet.
  schedule() {
    const t = this.now();
    for (const it of [...this.items.values()].sort((a, b) => a.index - b.index)) {
      for (const p of this.plays(it)) {
        if (this.made.has(p.key)) continue;
        const end = p.end === null || p.end === undefined ? Infinity : p.end;
        if (end <= t + LEAD || p.at > t + HORIZON) continue;
        const b = this.buffers.get(this.playUrl(p)) || this.load(this.playUrl(p));
        if (!b || !b.buf) continue;              // not decoded yet: the next tick tries again (a late song comes in late)
        this.make(it, p, b.buf, Math.max(p.at, t + LEAD));
        this.made.add(p.key);
      }
      if (it.join && it.join.duck && it.join.duck.length && !this.made.has(it.rid + ":duck") && it.start_s < t + HORIZON) {
        this.duck(it.join.duck, t + LEAD);
        this.made.add(it.rid + ":duck");
      }
    }
  }

  make(it, p, buf, from) {
    const c = this.ctx, t0 = this.ctxAt(from);
    const node = c.createBufferSource();
    node.buffer = buf;
    // playback rate: a step at every point of the time map (the beat warp of a blend)
    const rs = rates(p.src);
    node.playbackRate.setValueAtTime(rs.reduce((r, [t, v]) => (t <= from ? v : r), rs[0][1]), t0);
    for (const [t, r] of rs) if (t > from) node.playbackRate.setValueAtTime(r, this.ctxAt(t));
    const g = c.createGain();
    g.gain.setValueAtTime(lin(p.gain, from), t0);
    for (const [t, v] of p.gain) if (t > from) g.gain.linearRampToValueAtTime(v, this.ctxAt(t));
    let head = node;
    if (p.hp) {
      const f = c.createBiquadFilter();
      f.type = "highpass"; f.Q.value = HP_Q;
      f.frequency.setValueAtTime(expo(p.hp, from), t0);
      for (const [t, hz] of p.hp) if (t > from) f.frequency.exponentialRampToValueAtTime(hz, this.ctxAt(t));
      node.connect(f); head = f;
    }
    head.connect(g);
    g.connect(p.role === "mc" ? this.voice : this.music);
    const offset = Math.max(0, srcAt(p.src, from));
    node.start(t0, offset);
    const end = p.end === null || p.end === undefined ? null : p.end;
    if (end !== null) node.stop(this.ctxAt(end));
    const list = this.nodes.get(it.rid) || [];
    list.push({key: p.key, node, out: g});
    this.nodes.set(it.rid, list);
    node.onended = () => { g.disconnect(); };
  }

  duck(pts, from) {
    const gp = this.music.gain;
    gp.setValueAtTime(lin(pts, from), this.ctxAt(from));
    for (const [t, v] of pts) if (t > from) gp.linearRampToValueAtTime(v, this.ctxAt(t));
  }

  // ---- transport
  async start(at = 0) {
    // wait for what plays first, then start the clock at music time `at`
    this.status = "starting";
    const first = [...this.items.values()].filter(it => it.start_s <= at + 1).flatMap(it => this.plays(it))
      .filter(p => p.at <= at + 2 && (p.end === null || p.end === undefined || p.end > at));
    await Promise.all(first.map(p => (this.load(this.playUrl(p)) || {}).p));
    this.anchor = {ctx: this.ctx.currentTime + 0.1, music: at};
    this.status = "playing";
    if (this.ctx.state === "suspended") await this.ctx.resume();
    this.schedule();
    clearInterval(this.timer);
    this.timer = setInterval(() => { if (this.status === "playing") this.schedule(); this.report(this.now(), this.status); }, 1000);
    this.report(this.now(), this.status);
  }

  pause() {
    if (this.status !== "playing") return;
    this.pausedAt = this.now(); this.status = "paused";
    this.ctx.suspend();
    this.report(this.pausedAt, "paused");
  }

  async resume() {
    if (this.status !== "paused") return;
    await this.ctx.resume();
    this.status = "playing";
    this.report(this.now(), "playing");
  }

  // jump to SKIP_LEAD before the next join (when it is known), rebuilding every node from there
  skip() {
    if (this.status !== "playing") return false;
    const t = this.now();
    const next = [...this.items.values()].filter(it => it.start_s > t + SKIP_LEAD + 1).sort((a, b) => a.start_s - b.start_s)[0];
    if (!next) return false;
    this.stopAll();
    this.anchor = {ctx: this.ctx.currentTime + 0.05, music: next.start_s - SKIP_LEAD};
    this.schedule();
    this.report(this.now(), this.status);
    return true;
  }

  reset() {
    this.stopAll();
    this.items.clear(); this.buffers.clear();
    clearInterval(this.timer); this.timer = null;
    this.status = "idle"; this.pausedAt = 0; this.anchor = {ctx: 0, music: 0};
    if (this.ctx && this.ctx.state === "suspended") this.ctx.resume();
  }
}

if (typeof module !== "undefined") module.exports = {Player, lin, expo, srcAt, rates, limiterCurve};
