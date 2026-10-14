// Tests the page (algo_rhythm/static/live.html: the host at /p/CODE/host, guests at /p/CODE, the big screen at /p/CODE/screen)
// against fake server data in a simulated browser. The checks below still say "/", "/guest", "/presenter" and "/songs";
// open() maps them onto the party routes and the /api paths, and records a host key header as {host: true}.
// Setup once:  npm install jsdom@22 (anywhere; Node 16+)      Run:  NODE_PATH=<that node_modules> node tests/ui/ui_test.js
// The real browser and the real server: tests/ui/player_browser.py.
const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");
const STATIC = require("path").join(__dirname, "..", "..", "algo_rhythm", "static");
// jsdom does not fetch scripts here, so the visual engine (static/visuals.js) is inlined where the page loads it
const inline = (page, name) => {
  const tag = `<script src="/static/${name}"></script>`;
  if (!page.includes(tag)) throw new Error(`live.html no longer loads /static/${name}`);
  return page.replace(tag, () => `<script>${fs.readFileSync(require("path").join(STATIC, name), "utf8")}</script>`);
};
const html = inline(inline(fs.readFileSync(require("path").join(STATIC, "live.html"), "utf8"), "visuals.js"), "player.js");
const CODE = "TESTAB", ROUTES = {"/": `/p/${CODE}/host`, "/guest": `/p/${CODE}`, "/presenter": `/p/${CODE}/screen`};
// the page's own URLs -> the ones these checks were written against
const plain = url => url.replace(new RegExp(`^/api/p/${CODE}`), "").replace(/^\/api/, "");

const entry = (name, extra = {}) => ({name, bpm: 97, key: "10B", energy: "low", mood: "bright/laid-back", kind: "blend", match: 0.72, request: false, status: "planned", ...extra});
const base = {api: 3, status: "playing", level: "low", levels: ["low","medium","high","max"], now: entry("Behti Hawa Sa Tha Woh - 3 Idiots"),
  position_s: 38.4, length_s: 70, mixing: null, next: [entry("Kabira Encore - YJHD", {status: "locked"}), entry("Tum Hi Ho", {request: true, kind: "echo", match: 0.31})],
  requests: [], history: ["Baarishein - Anuv Jain"], log: ["02:10:00  vibe set to low"]};
const wait = ms => new Promise(r => setTimeout(r, ms));

// Opens the page at `path` with a fake server. Collects script errors (jsdom's "not implemented" canvas notice is expected and ignored).
function open(path, fetchFn, setup) {
  const errors = [], vc = new VirtualConsole();
  vc.on("jsdomError", e => { if (!/Not implemented/.test(e.message)) errors.push(e.message); });
  const [route, query] = path.split("?");
  const dom = new JSDOM(html, {runScripts: "dangerously", pretendToBeVisual: true, url: "http://127.0.0.1:8765" + (ROUTES[route] || route) + (query ? "?" + query : ""), virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (url, opt) => {
        if (opt && opt.method === "POST" && opt.body && opt.headers && opt.headers["X-Host-Key"]) {
          const b = JSON.parse(opt.body); b.host = true; opt = {...opt, body: JSON.stringify(b)};
        }
        // an answer that arrives after the check closed the window is dropped (the page is gone)
        const gone = () => new Promise(() => {});
        return Promise.resolve(fetchFn(plain(url), opt)).then(v => (w.document ? v : gone()), e => { if (!w.document) return gone(); throw e; });
      };
      w.navigator.sendBeacon = (url, blob) => { w.beacons = (w.beacons || 0) + 1; return true; };
      w.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} };   // live updates: the checks feed /state instead
      w.localStorage.setItem(`algo-host-${CODE}`, "host-key-for-tests");                 // this browser started the party
      if (setup) setup(w);
    }});
  return {dom, errors, D: dom.window.document};
}

async function load(stateFn, {failState = false, path = "/"} = {}) {
  const {dom, errors, D} = open(path, async (url) => {
    if (url === "/songs") return {ok: true, json: async () => [{file: "a.mp3", name: "Tum Hi Ho - Aashiqui 2", mood: "moody/laid-back"}]};
    if (failState) throw new Error("network down");
    return {ok: true, json: async () => stateFn()};
  });
  await wait(300);
  const t = id => D.getElementById(id), txt = id => t(id) ? t(id).textContent.replace(/\s+/g, " ").trim() : null;
  const out = {levels: t("levels") ? [...t("levels").children].map(b => b.textContent + (b.classList.contains("on") ? "*" : "")) : [],
    transport: txt("transport"), now: txt("now"), sub: txt("nowsub"), info: txt("nowinfo"), status: txt("statusline"),
    next: t("journey") ? [...t("journey").children].map(c => c.textContent.replace(/\s+/g, " ")) : [],
    blend: txt("blend"), stale: !t("stale").hidden, conn: t("conn").hidden ? "" : t("conn").textContent, err: txt("err"),
    history: txt("history"), holding: txt("holding"), up: t("upnext") && !t("upnext").hidden ? txt("upnext") : "", meter: txt("meterlabel"),
    meterOn: t("meter") ? t("meter").querySelectorAll(".on").length : 0, htmlClass: D.documentElement.className,
    beat: D.documentElement.style.getPropertyValue("--beat"), tabs: !!D.querySelector("nav.tabs"), q: !!t("q"), talk: !!t("talk"),
    garden: t("garden") ? t("garden").getAttribute("aria-label") : null, errors};
  dom.window.dispatchEvent(new dom.window.Event("pagehide"));
  out.beacons = dom.window.beacons || 0;
  dom.window.close();
  return out;
}
let failures = 0;
const check = (name, cond, detail) => { if (!cond) { failures++; } console.log(`${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : "  -> " + JSON.stringify(detail)}`); };

(async () => {
  let o = await load(() => base);
  check("normal: shows the playing song title and film", o.now === "Behti Hawa Sa Tha Woh" && o.sub === "3 Idiots", [o.now, o.sub]);
  check("normal: info line uses energy word (not 0%)", /energy low/.test(o.info), o.info);
  check("normal: 6 vibe buttons, none lit when the level+mood pair is not a vibe", o.levels.join() === "chill,sad,romantic,groove,party,peak", o.levels);
  check("normal: energy theme class and --beat from the BPM", /\be-low\b/.test(o.htmlClass) && o.beat === "0.619s" && !/loading/.test(o.htmlClass), [o.htmlClass, o.beat]);
  check("normal: button says Pause while playing", o.transport === "Pause", o.transport);
  check("normal: journey has 2 cards with a request and an echo-out tag", o.next.length === 2 && /request/.test(o.next[1]) && /echo-out/.test(o.next[1]) && /match 0.31/.test(o.next[1]), o.next);
  check("normal: locked song marked ready", /ready/.test(o.next[0]), o.next[0]);
  check("normal: blend bar counts down to the next song", /Behti Hawa.*Blend in ?0:31.*Kabira Encore/.test(o.blend), o.blend);
  check("normal: status line names what comes next", /Up next: Kabira Encore, as a blend/.test(o.status), o.status);
  check("normal: no banners, no script errors", !o.stale && o.conn === "" && o.err === "" && !o.errors.length, o);
  check("admin: view tabs, talk and request shown; garden has a label", o.tabs && o.talk && o.q && /decorative/.test(o.garden), o);
  check("admin: closing the tab no longer stops anything (no goodbye beacon)", o.beacons === 0, o.beacons);

  o = await load(() => ({...base, mixing: {from: "Behti Hawa Sa Tha Woh - 3 Idiots", to: "Kabira Encore - YJHD", kind: "blend", progress: 0.4}}));
  check("mixing: blend bar says blending now with both songs and 40%", /Behti Hawa.*Blending now ?40%.*Kabira Encore/.test(o.blend), o.blend);
  check("mixing: status line says blending", /Blending Behti Hawa Sa Tha Woh into Kabira Encore/.test(o.status), o.status);
  o = await load(() => ({...base, mixing: {from: "A - x", to: "B - y", kind: "echo", progress: 0.2}}));
  check("mixing: echo-out labelled honestly", /Echo-out now/.test(o.blend) && /Echoing out of A into B/.test(o.status), [o.blend, o.status]);

  o = await load(() => ({...base, status: "idle", now: null, next: [], position_s: 0, length_s: 0, history: [], log: []}));
  check("idle: Play button, Warming up, buttons still there", o.transport === "Play" && o.now === "Warming up…" && o.levels.length === 6, o);
  check("idle: status line tells you what to do", /press Play/.test(o.status), o.status);
  check("idle: queue explains itself", /after you press Play/.test(o.next.join()), o.next);

  o = await load(() => ({...base, status: "paused"}));
  check("paused: button says Resume", o.transport === "Resume", o.transport);

  // new page, OLD server (no api field, no history)
  const old = {...base}; delete old.api; delete old.history;
  o = await load(() => old);
  check("old server: asks for a reload instead of half-drawing", o.stale, o);
  check("old server: vibe buttons still drawn", o.levels.length === 6, o.levels);
  check("old server: does NOT claim the connection was lost", o.conn === "", o.conn);

  // a missing field must not blank the other sections
  const partial = {...base}; delete partial.history;
  o = await load(() => partial);
  check("missing field: other sections still draw", o.now === "Behti Hawa Sa Tha Woh" && o.next.length === 2 && o.levels.length === 6, o);
  check("missing field: tells the user which part broke", /Display problem/.test(o.err), o.err);

  o = await load(() => base, {failState: true});
  check("network down: says reconnecting, buttons still there", /Cannot reach.*Reconnecting/.test(o.conn) && o.levels.length === 6, o);

  // guest and presenter: render from a fake /state, read-only parts only, never send the goodbye beacon
  o = await load(() => ({...base, level: "high", mood: "party", vibe: "party"}));
  check("vibe: 'party' lit, holding line names it with its energy", o.levels.join() === "chill,sad,romantic,groove,party*,peak"
    && /Holding party \(high energy\)/.test(o.holding), [o.levels, o.holding]);
  o = await load(() => ({...base, level: "max", mood: "romantic", vibe: null}));
  check("vibe: an odd pair from Ask the DJ lights nothing and is spelled out", !o.levels.some(x => x.endsWith("*"))
    && /Holding max energy, romantic mood/.test(o.holding), o.holding);
  o = await load(() => base, {path: "/guest"});
  check("guest: renders now playing and up next", o.now === "Behti Hawa Sa Tha Woh" && /Up next · blend ?Kabira Encore in 0:31/.test(o.up), [o.now, o.up]);
  check("guest: has request search and Ask the DJ, no admin controls or view tabs", o.q && o.talk && !o.tabs && o.transport === null && o.levels.length === 0, o);
  check("guest: no script errors, no goodbye beacon", !o.errors.length && o.err === "" && o.beacons === 0, o);
  o = await load(() => base, {path: "/presenter"});
  check("presenter: renders title, status line, up next countdown", o.now === "Behti Hawa Sa Tha Woh" && /Up next/.test(o.status) && /Kabira Encore in 0:31/.test(o.up), [o.now, o.status, o.up]);
  check("presenter: energy meter shows the level", o.meter === "low" && o.meterOn === 1, [o.meter, o.meterOn]);
  check("presenter: read-only, no script errors, no goodbye beacon", !o.q && !o.tabs && o.transport === null && !o.errors.length && o.err === "" && o.beacons === 0, o);
  o = await load(() => ({...base, status: "idle", now: null, next: []}), {path: "/presenter"});
  check("presenter idle: Warming up, no up next", o.now === "Warming up…" && o.up === "", [o.now, o.up]);

  // guest feedback: hidden without the "feedback" key; votes post the guest id, show as pressed, can be changed
  const fbState = {...base, feedback: {song: {position: 5, up: 2, down: 1}, mix: {position: 5, up: 0, down: 0, open: true}}};
  {
    const g = open("/guest", async () => ({ok: true, json: async () => base}));
    await wait(300);
    check("guest feedback: hidden when the server sends no feedback", g.D.getElementById("fb").hidden && !g.errors.length, g.errors);
    g.dom.window.close();
  }
  {
    const fposts = [];
    let st = fbState;
    const g = open("/guest", async (url, opt) => {
      if (url === "/songs") return {ok: true, json: async () => []};
      if (opt && opt.method === "POST") { fposts.push({url, ...JSON.parse(opt.body)}); return {ok: true, json: async () => st}; }
      return {ok: true, json: async () => st};
    });
    await wait(300);
    const D2 = g.D, bs = [...D2.querySelectorAll(".fbb")], byText = t => bs.find(b => b.textContent.includes(t));
    check("guest feedback: song choices only (the host rates the joins), none pressed", !D2.getElementById("fb").hidden && !D2.getElementById("fbmix")
      && bs.length === 2 && bs.every(b => b.dataset.t === "song" && b.getAttribute("aria-pressed") === "false"), bs.map(b => b.textContent));
    byText("Loving it").click(); await wait(50);
    const id = fposts[0] && fposts[0].guest;
    check("guest feedback: Loving it posts song up with a guest id", fposts.length === 1 && fposts[0].url === "/feedback" && fposts[0].target === "song"
      && fposts[0].value === "up" && typeof id === "string" && id.length >= 8, fposts);
    check("guest feedback: pressed state and a quiet thanks", byText("Loving it").getAttribute("aria-pressed") === "true"
      && byText("Not for me").getAttribute("aria-pressed") === "false" && /Thanks/.test(D2.getElementById("fbthanks").textContent), D2.getElementById("fbthanks").textContent);
    byText("Not for me").click(); await wait(50);
    check("guest feedback: changing the vote keeps the same guest id", fposts[1] && fposts[1].value === "down" && fposts[1].guest === id
      && byText("Not for me").getAttribute("aria-pressed") === "true" && byText("Loving it").getAttribute("aria-pressed") === "false", fposts);
    check("guest feedback: a guest vote never claims to be the host", fposts.every(f => f.host === undefined), fposts);
    st = {...fbState, feedback: {song: {position: 6, up: 0, down: 0}, mix: null}};
    await wait(1200);
    check("guest feedback: a new song starts unpressed", bs.filter(b => b.dataset.t === "song").every(b => b.getAttribute("aria-pressed") === "false") && !g.errors.length, g.errors);
    g.dom.window.close();
  }
  {
    const a = open("/", async (url) => ({ok: true, json: async () => (url === "/songs" ? [] : fbState)}));
    await wait(300);
    const tally = a.D.getElementById("fbtally").textContent;
    check("admin: small guest tallies next to the system log, and the rating panel", /song: 2 up · 1 down \| last mix: 0 smooth · 0 rough/.test(tally) && !a.D.getElementById("fb").hidden, tally);
    a.dom.window.close();
  }

  // host feedback: thumbs on the join that just played (with its songs and type) and on the song, sent as the host
  {
    const hposts = [];
    let st = {...fbState, feedback: {song: {position: 5, up: 2, down: 1, name: "Kabira Encore - YJHD"},
      mix: {position: 5, up: 3, down: 0, open: true, from: "Behti Hawa Sa Tha Woh - 3 Idiots", to: "Kabira Encore - YJHD", kind: "cut"}}};
    const h = open("/", async (url, opt) => {
      if (opt && opt.method === "POST") { hposts.push({url, ...JSON.parse(opt.body)}); return {ok: true, json: async () => st}; }
      return {ok: true, json: async () => (url === "/songs" ? [] : st)};
    });
    await wait(300);
    const HD = h.D, t = id => HD.getElementById(id).textContent.replace(/\s+/g, " ").trim();
    check("host feedback: the last join is named with its type, with counts", !HD.getElementById("fb").hidden && !HD.getElementById("fbmix").hidden
      && t("ratemix") === "Behti Hawa Sa Tha Woh → Kabira Encore (cut)" && t("mixup") === "3" && t("ratesong") === "Kabira Encore" && t("songup") === "2", [t("ratemix"), t("mixup"), t("ratesong")]);
    HD.querySelector('.fbb[data-t="transition"][data-v="up"]').click(); await wait(50);
    HD.querySelector('.fbb[data-t="song"][data-v="down"]').click(); await wait(50);
    check("host feedback: thumbs post the join and the song as the host, and show pressed",
      hposts.length === 2 && hposts[0].url === "/feedback" && hposts[0].target === "transition" && hposts[0].value === "up" && hposts[0].host === true
      && hposts[1].target === "song" && hposts[1].value === "down" && hposts[1].host === true
      && HD.querySelector('.fbb[data-t="transition"][data-v="up"]').getAttribute("aria-pressed") === "true", hposts);
    st = {...st, feedback: {...st.feedback, mix: {...st.feedback.mix, open: false}}};
    await wait(1200);
    check("host feedback: once the join is over, only the song can be rated and the page says when the next join can be",
      HD.getElementById("fbmix").hidden && !HD.getElementById("ratewait").hidden && !h.errors.length, h.errors);
    h.dom.window.close();
  }

  // guest request flow: a name goes with the request; the page shows the live place in the queue, then cheers when it plays
  {
    const rposts = [];
    let st = base;
    const g = open("/guest", async (url, opt) => {
      if (url === "/songs") return {ok: true, json: async () => [{file: "t.mp3", name: "Tum Hi Ho", mood: "moody"}]};
      if (opt && opt.method === "POST") { rposts.push({url, ...JSON.parse(opt.body)}); return {ok: true, json: async () => st}; }
      return {ok: true, json: async () => st};
    });
    await wait(300);
    const GD = g.D, mineTxt = () => GD.getElementById("mine").textContent.replace(/\s+/g, " ");
    GD.getElementById("byname").value = "Priya";
    const q = GD.getElementById("q"); q.value = "tu"; q.dispatchEvent(new g.dom.window.Event("input"));
    GD.querySelector("#results button").click(); await wait(50);
    check("request: posts the file with the guest's name", rposts[0] && rposts[0].url === "/request" && rposts[0].file === "t.mp3" && rposts[0].name === "Priya", rposts);
    check("request: shows its live place in the queue", /#2.*Tum Hi Ho.*You're #2 in the queue/.test(mineTxt()), mineTxt());
    check("request: the name is kept for next time", JSON.parse(g.dom.window.localStorage.getItem("algo-name")) === "Priya");
    st = {...base, now: entry("Tum Hi Ho", {request: true, by: "Priya"}), next: [entry("Kabira Encore - YJHD")]};
    await wait(1200);
    check("request: when it plays the page cheers and credits the requester", /Now.*Your song is playing/.test(mineTxt())
      && GD.getElementById("bursts").children.length > 0 && GD.getElementById("nowby").textContent === "Requested by Priya" && !g.errors.length, [mineTxt(), g.errors]);
    st = {...base, next: []};
    await wait(1200);
    check("request: gone from the list once it has played", mineTxt() === "", mineTxt());
    g.dom.window.close();
  }

  // live reactions: guests send one of five emoji; new ones (not the ones already there on load) rise on guest and presenter screens
  {
    const xposts = [];
    let st = {...base, reactions: [{id: 4, emoji: "🔥"}]};
    const g = open("/guest", async (url, opt) => {
      if (opt && opt.method === "POST") { xposts.push({url, ...JSON.parse(opt.body)}); st = {...st, reactions: [...st.reactions, {id: 5, emoji: "🎉"}]}; return {ok: true, json: async () => st}; }
      return {ok: true, json: async () => (url === "/songs" ? [] : st)};
    });
    await wait(300);
    const rb = [...g.D.querySelectorAll("#reacts button")], bursts = () => g.D.getElementById("bursts").children.length;
    check("reactions: five buttons, nothing rises for reactions sent before the page opened", rb.length === 5 && bursts() === 0, [rb.length, bursts()]);
    rb.find(b => b.textContent === "🎉").click(); await wait(50);
    check("reactions: a tap posts /react with the guest id and the emoji rises", xposts[0] && xposts[0].url === "/react" && xposts[0].emoji === "🎉"
      && xposts[0].guest.length >= 8 && bursts() === 1, [xposts, bursts()]);
    g.dom.window.close();
    let ps = {...base, reactions: []};
    const P = open("/presenter", async () => ({ok: true, json: async () => ps}));
    await wait(300);
    ps = {...base, reactions: [{id: 9, emoji: "❤️"}, {id: 10, emoji: "🙌"}], now: entry("Tum Hi Ho", {request: true, by: "Priya"})};
    await wait(1500);
    check("presenter: guests' reactions rise on the big screen; the requester is credited", P.D.getElementById("bursts").children.length === 2
      && P.D.getElementById("nowby").textContent === "Requested by Priya" && !P.D.getElementById("nowby").hidden, P.D.getElementById("bursts").children.length);
    check("presenter: shows where guests join, big", !P.D.getElementById("join").hidden && P.D.getElementById("joinurl").textContent === `127.0.0.1:8765/p/${CODE}`
      && !P.errors.length, [P.D.getElementById("joinurl").textContent, P.errors]);
    P.dom.window.close();
    const A2 = open("/", async (url) => ({ok: true, json: async () => (url === "/songs" ? [] : {...base, reactions: [{id: 1, emoji: "🔥"}], next: [entry("Tum Hi Ho", {request: true, by: "Priya"})]})}));
    await wait(1500);
    check("admin: no reaction bursts; a requested song is tagged with who asked", A2.D.getElementById("bursts").children.length === 0
      && /request by Priya/.test(A2.D.getElementById("journey").textContent), A2.D.getElementById("journey").textContent);
    A2.dom.window.close();
  }

  // why this song + levels + transport + start over (two taps, then one POST) + talk
  const explain = {why: "tempo gap 3%, keys 10B->10B (0 steps)", score: 2.5, terms: {mixability: 0.8}, energy: 0.3, target: 0.125, plays: 1, penalty: 1.5,
    window: {from_s: 0, to_s: 100, from_start: true, vocal_swap: true}, gemini: {energy: 1, confidence: 0.9, occasion: ["late night"], familiarity: 5},
    alts: [{name: "Other Song", behind: 0.2}]};
  const posts = [];
  const {dom, D} = open("/", async (url, opt) => {
    if (url === "/songs") return {ok: true, json: async () => []};
    if (opt && opt.method === "POST") {
      const b = JSON.parse(opt.body); posts.push({url, ...b});
      if (url === "/talk") return {ok: true, json: async () => ({...base, level: "high", talk: {reply: "Peak time!", done: ["level high"], source: "gemini"}})};
      return {ok: true, json: async () => ({...base, status: "idle", now: null, next: []})};
    }
    return {ok: true, json: async () => ({...base, next: [entry("Kabira Encore - YJHD", {explain})]})};
  });
  await wait(300);
  const why = D.getElementById("why"), det = why.querySelectorAll("details");
  check("why: one block for the next song plus how the DJ picks", det.length === 2 && det[0].open && !det[1].open, det.length);
  const txt = why.textContent;
  check("why: shows why, gemini, window, penalty, next best, match, rules", /Why:.*tempo gap/.test(txt) && /Gemini:.*late night/.test(txt) && /Plays:.*0:00 to 1:40/.test(txt)
    && /reduced by 1.5/.test(txt) && /Other Song/.test(txt) && /match 0.72/.test(txt) && /near 0.125/.test(txt), txt);
  const lv = [...D.getElementById("levels").children];
  lv[0].focus();
  lv[0].dispatchEvent(new dom.window.KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}));
  check("vibes: arrow key moves focus to the next vibe without posting", D.activeElement === lv[1] && posts.length === 0, posts);
  lv[5].click(); await wait(50);
  check("vibes: tapping peak posts /vibe peak", posts.length === 1 && posts[0].url === "/vibe" && posts[0].vibe === "peak", posts);
  D.getElementById("skip").click(); await wait(50);
  check("transport: skip does nothing until this device plays the music", posts.length === 1, posts);
  posts.length = 0;
  const T = D.getElementById("talk"); T.value = "build it up";
  D.getElementById("talkform").dispatchEvent(new dom.window.Event("submit", {cancelable: true}));
  await wait(50);
  check("talk: sends the text and shows the reply", posts[0] && posts[0].url === "/talk" && posts[0].text === "build it up" && /Peak time!/.test(D.getElementById("talkreply").textContent), posts);
  check("talk: page takes the returned state", /\be-high\b/.test(D.documentElement.className), D.documentElement.className);
  posts.length = 0;
  D.getElementById("restart").click();
  check("restart: first tap only asks", posts.length === 0 && /again/.test(D.getElementById("restart").textContent), posts);
  D.getElementById("restart").click();
  await wait(100);
  check("restart: second tap sends one restart", posts.length === 1 && posts[0].action === "restart", posts);
  check("restart: page returns to Play", D.getElementById("transport").textContent === "Play");
  check("mic: hidden where the browser has no speech recognition", D.getElementById("talkmic").hidden);
  check("MC: switch hidden when the server sends no mc key", D.getElementById("mc").hidden);
  dom.window.close();

  // mic + AI MC switch: a fake SpeechRecognition, and a server that has an MC
  const recs = [], posts2 = [];
  const mcState = {...base, mc: {enabled: true, last_line: "Hands up", last_at_s: 300, next_eligible_s: 900}};
  const m = open("/", async (url, opt) => {
    if (url === "/songs") return {ok: true, json: async () => []};
    if (opt && opt.method === "POST") {
      const b = JSON.parse(opt.body); posts2.push({url, ...b});
      if (url === "/talk") return {ok: true, json: async () => ({...mcState, talk: {reply: "Okay!", done: ["level max"], source: "gemini"}})};
      return {ok: true, json: async () => ({...mcState, mc: {...mcState.mc, enabled: b.action !== "mc_off"}})};
    }
    return {ok: true, json: async () => mcState};
  }, w => { w.webkitSpeechRecognition = class { constructor() { recs.push(this); } start() { this.started = true; } stop() { this.stopped = true; if (this.onend) this.onend(); } }; });
  await wait(300);
  const MD = m.D, mic = MD.getElementById("talkmic"), W = m.dom.window;
  check("mic: shown when speech recognition exists", !mic.hidden);
  mic.click();
  check("mic: listening state (aria-pressed, label, en-IN)", recs.length === 1 && recs[0].started && recs[0].lang === "en-IN" && mic.getAttribute("aria-pressed") === "true" && mic.textContent === "Stop", recs[0]);
  const res = (t, fin) => { const r = [{transcript: t}]; r.isFinal = fin; return {results: [r]}; };
  recs[0].onresult(res("aur tez", false));
  check("mic: interim words show in the box, nothing sent yet", MD.getElementById("talk").value === "aur tez" && posts2.length === 0, posts2);
  recs[0].onresult(res("aur tez karo", true)); await wait(50);
  check("mic: final words are sent to /talk", posts2.length === 1 && posts2[0].url === "/talk" && posts2[0].text === "aur tez karo", posts2);
  check("mic: back to idle after the result", mic.getAttribute("aria-pressed") === "false" && mic.textContent === "Speak");
  mic.click(); recs[1].onerror({error: "not-allowed"});
  check("mic: permission denied in plain words", /permission was denied/.test(MD.getElementById("talkreply").textContent), MD.getElementById("talkreply").textContent);
  recs[1].stop();
  const sw = MD.getElementById("mc");
  check("MC: switch shown and on", !sw.hidden && sw.getAttribute("aria-checked") === "true" && /Hands up/.test(sw.title), sw.outerHTML);
  posts2.length = 0; sw.click(); await wait(50);
  check("MC: tapping it posts mc_off and the switch turns off", posts2[0] && posts2[0].action === "mc_off" && sw.getAttribute("aria-checked") === "false", posts2);
  m.dom.window.close();

  // song search: under 3 characters filters the list here; 3+ asks /search (debounced); a failed /search filters here
  const urls = [];
  let searchOk = true;
  const S = open("/guest", async (url) => {
    urls.push(url);
    if (url === "/songs") return {ok: true, json: async () => [{file: "a.mp3", name: "Tum Hi Ho - Aashiqui 2", mood: "moody/laid-back"}]};
    if (url.startsWith("/search")) {
      if (!searchOk) throw new Error("network down");
      if (url.includes("chikni")) return {ok: true, json: async () => [{file: "c.mp3", name: "Chicken Kuk-Doo-Koo", mood: "party", named: false}]};
      if (url.includes("zzzz")) return {ok: true, json: async () => []};
      return {ok: true, json: async () => [{file: "r.mp3", name: "Barso - Guzaarish", mood: "bright/laid-back"}]};
    }
    return {ok: true, json: async () => base};
  });
  await wait(300);
  const q = S.D.getElementById("q"), results = () => S.D.getElementById("results").textContent;
  const type = text => { q.value = text; q.dispatchEvent(new S.dom.window.Event("input")); };
  type("tu");
  check("search: 2 characters filter the list here at once", /Tum Hi Ho/.test(results()) && !urls.some(u => u.startsWith("/search")), [results(), urls]);
  type("rai"); type("rain song");
  await wait(100);
  check("search: 3+ characters wait for the debounce", !urls.some(u => u.startsWith("/search")), urls);
  await wait(300);
  const asked = urls.filter(u => u.startsWith("/search"));
  check("search: one /search call for the last text, its results shown", asked.length === 1 && asked[0] === "/search?q=rain%20song" && /Barso/.test(results()), [asked, results()]);
  check("search: real matches have no 'similar' label", !/Similar|No song called/.test(results()), results());
  type("chikni chameli"); await wait(400);
  check("search: only similar songs -> says the song is not in the library", /No song called "chikni chameli" in the library\. Similar songs:.*Chicken/.test(results()), results());
  type("zzzz"); await wait(400);
  check("search: nothing at all -> says so", /No song called "zzzz"/.test(results()), results());
  searchOk = false; type("tum h");
  await wait(400);
  check("search: a failed /search falls back to the name filter", /Tum Hi Ho/.test(results()) && !/Barso/.test(results()), results());
  check("search: no script errors", !S.errors.length, S.errors);
  S.dom.window.close();

  // song art: jsdom cannot decode images, so Image is a stub that "loads" on the next tick (a url with "bad" fails).
  // The canvas is unavailable in jsdom, so palette sampling falls back to the name palette; artColours is checked on its own.
  {
    const K = "/art/kesariya.jpg", C = "/art/kala.jpg";
    const artState = {...base, now: entry("Kesariya - Brahmastra", {art: K}), next: [entry("Kala Chashma - Baar Baar Dekho", {art: C})]};
    const asked = [];
    const stubs = (calmMotion) => w => {
      w.Image = class { set src(u) { this._s = u; asked.push(u); setTimeout(() => { const f = /bad/.test(u) ? this.onerror : this.onload; if (f) f(); }, 0); } get src() { return this._s; } };
      if (calmMotion) w.matchMedia = q => ({matches: /reduce/.test(q)});
    };
    let st = artState;
    const A = open("/", async (url) => ({ok: true, json: async () => (url === "/songs" ? [] : st)}), stubs(false));
    await wait(300);
    const box = A.D.querySelector(".songart"), imgs = [...box.querySelectorAll("img")];
    const layer = u => imgs.find(i => i.getAttribute("src") === u), op = u => layer(u) ? Number(layer(u).style.opacity) : null;
    check("art: shown with the song's image at full strength, moving", !box.hidden && op(K) === 1 && box.classList.contains("moving"), [box.hidden, imgs.map(i => [i.getAttribute("src"), i.style.opacity]), box.className]);
    check("art: the next song's image is preloaded", asked.includes(C), asked);
    st = {...artState, mixing: {from: "Kesariya - Brahmastra", to: "Kala Chashma - Baar Baar Dekho", kind: "blend", progress: 0.25, from_art: K, to_art: C}};
    await wait(1100);
    check("art: mid-mix the incoming layer is smoothstep(0.25) = 0.156, outgoing stays 1, incoming on top",
      Math.abs(op(C) - 0.156) < 0.002 && op(K) === 1 && layer(C).style.zIndex === "1", [op(K), op(C)]);
    st = {...st, mixing: {...st.mixing, progress: 0.75}};
    await wait(1100);
    check("art: at progress 0.75 the incoming layer is 0.844", Math.abs(op(C) - 0.844) < 0.002, op(C));
    st = {...artState, now: entry("Kala Chashma - Baar Baar Dekho", {art: C}), next: []};
    await wait(1100);
    check("art: after the mix the incoming layer is the base", op(C) === 1 && op(K) === 0 && layer(C).style.zIndex === "1" && !box.hidden, [op(K), op(C)]);
    st = {...st, mixing: {from: "Kala Chashma - Baar Baar Dekho", to: "Tum Hi Ho", kind: "echo", progress: 0.5, from_art: C, to_art: null}};
    await wait(1100);
    check("art: mixing into a song with no art fades the art out", op(C) === 0.5 && !box.hidden, op(C));
    st = {...base, now: entry("Tum Hi Ho", {art: null})};
    await wait(1100);
    check("art: hidden when now.art is null", box.hidden && !box.classList.contains("moving"), box.className);
    st = {...base, now: entry("Broken - x", {art: "/art/bad.jpg"})};
    await wait(1100);
    check("art: an image that fails to load falls back quietly", box.hidden && !A.errors.length && A.D.getElementById("err").textContent === "", A.errors);
    A.dom.window.close();

    const R = open("/presenter", async (url) => ({ok: true, json: async () => artState}), stubs(true));
    await wait(300);
    const rb = R.D.querySelector(".songart");
    check("art: presenter shows it full screen; reduced motion drops the moving class", !rb.hidden && !rb.classList.contains("moving")
      && rb.querySelector("img").getAttribute("src") === K && !R.errors.length, [rb.hidden, rb.className, R.errors]);
    R.dom.window.close();

    const O = open("/guest", async (url) => ({ok: true, json: async () => (url === "/songs" ? [] : {...base, mixing: {from: "A - x", to: "B - y", kind: "blend", progress: 0.3}})}), stubs(false));
    await wait(300);
    check("art: old server without art fields renders as today, no script errors", O.D.querySelector(".songart").hidden
      && O.D.getElementById("now").textContent === "Behti Hawa Sa Tha Woh" && !O.errors.length && O.D.getElementById("err").textContent === "", O.errors);
    const px = [255, 40, 40, 255, 40, 255, 40, 255, 30, 30, 255, 255, 20, 20, 20, 255, 128, 128, 128, 255, 250, 45, 45, 255];
    const pal = O.dom.window.artColours(px), grey = O.dom.window.artColours([128, 128, 128, 255, 20, 20, 20, 255]);
    check("art: palette keeps distinct bright colours, skips dark, grey and near-duplicates; too few gives null",
      pal && pal.join(" ") === "30,30,255 255,40,40 40,255,40" && grey === null, [pal, grey]);
    O.dom.window.close();
  }

  // ---- visuals (static/visuals.js). The canvas is a stub that accepts any drawing call; animation frames never run on
  // their own here: G.step(time) is called by hand, and performance.now() returns the same fake time, so the clock is exact.
  {
    const fakeCtx = () => new Proxy({}, {set: (t, k, v) => { t[k] = v; return true; }, get: (t, k) => k in t ? t[k]
      : /^create(Radial|Linear)Gradient$/.test(k) ? () => ({addColorStop() {}}) : k === "getImageData" ? () => ({data: new Uint8ClampedArray(4)}) : () => {}});
    const vis = (path, stateFn, {calmMotion = false, stored = null} = {}) => {
      let T = 1000;
      const o = open(path, async (url) => ({ok: true, json: async () => (url === "/songs" ? [] : stateFn(T))}), w => {
        w.HTMLCanvasElement.prototype.getContext = function () { return this._c || (this._c = fakeCtx()); };
        w.rafs = 0; w.requestAnimationFrame = () => ++w.rafs; w.cancelAnimationFrame = () => {};
        Object.defineProperty(w.performance, "now", {value: () => T, configurable: true});
        if (calmMotion) w.matchMedia = q => ({matches: /reduce/.test(q)});
        if (stored) w.localStorage.setItem("algo-visuals", JSON.stringify(stored));
      });
      const G = () => o.dom.window.eval("G");
      const run = ms => { for (let e = 0; e < ms; e += 16) { T += 16; G().step(T); } };
      return {...o, G, run, now: () => T};
    };
    const env = n => ({t0: 9, dt: 0.1, rms: Array(n).fill(0.5), bass: Array(n).fill(0.3), treble: Array(n).fill(0.4)});
    const P = s => ({server_s: s, bpm: 120, beats: [10, 10.5, 11, 11.5, 12], downbeats: [10, 12], drops: [{t: 11.2, kind: "drop"}], env: env(60)});

    // beats fire when the local clock crosses pulse.beats, once each; a reading slightly behind slows the clock, never reverses it
    const V = vis("/presenter", T => ({...base, pulse: P(9.8 + (T - 1000) / 1000)}));
    await wait(300);
    const g = V.G(), st = g.stats;
    V.run(150);
    check("pulse: no beat before the playhead reaches the first beat time", st.beats === 0 && st.mode === "pulse" && st.pos < 10, [st.beats, st.mode, st.pos]);
    V.run(112);
    check("pulse: the beat at 10.0 fires as the clock crosses it, as a downbeat", st.beats === 1 && st.downbeats === 1, [st.beats, st.downbeats, st.pos]);
    g.set({pulse: P(st.pos - 0.3)});                                // a new reading 0.3 s behind the local clock
    let prevPos = st.pos, back = false;
    for (let i = 0; i < 120 && st.pos < 11.3; i++) { V.run(16); back = back || st.pos < prevPos; prevPos = st.pos; }
    check("pulse: a reading behind the clock eases it, never backwards", !back && st.pos >= 11.3, [st.pos, back]);
    const same = g.pulse; g.set({pulse: same});                      // the same reading again (a redraw): not a new reading
    V.run(32);
    check("pulse: beats 10, 10.5 and 11 fired exactly once each", st.beats === 3 && st.downbeats === 1, [st.beats, st.downbeats]);
    check("pulse: the drop at 11.2 fires once and reaches the world's climax (koi for a low song)",
      st.events.filter(e => e === "drop").length === 1 && st.climax === "koi", [st.events, st.climax]);
    check("pulse: no script errors", !V.errors.length && V.D.getElementById("err").textContent === "", V.errors);
    V.dom.window.close();

    // the world follows the playing song's energy; a mix into a song of another energy migrates; an echo-out says so
    const low = entry("A - x", {energy: "low"}), max = entry("B - y", {energy: "max"}), mid = entry("C - z", {energy: "medium"});
    let ws = {...base, now: low, next: [max]};
    const Wv = vis("/presenter", () => ws);
    await wait(300);
    const s2 = Wv.G().stats;
    check("worlds: a low song shows the koi world", s2.world === "koi", s2.world);
    ws = {...base, now: low, next: [], mixing: {from: "A - x", to: "B - y", kind: "blend", progress: 0.3}};
    await wait(1100);
    check("worlds: mixing into a max song heads for Holi, with migrating motes", s2.world === "holi" && s2.mixKind === "blend" && s2.motes > 0, [s2.world, s2.mixKind, s2.motes]);
    Wv.run(500);
    ws = {...base, now: max, next: [mid]};
    await wait(1100);
    Wv.run(1200);
    check("worlds: after the mix the max song's world stays", s2.world === "holi" && s2.mixKind === null, [s2.world, s2.mixKind]);
    ws = {...base, now: max, next: [], mixing: {from: "B - y", to: "C - z", kind: "echo", progress: 0.2}};
    await wait(1100);
    check("worlds: an echo-out into a medium song heads for the lanterns as an echo", s2.world === "lanterns" && s2.mixKind === "echo", [s2.world, s2.mixKind]);
    ws = {...base, now: entry("D - w", {energy: undefined}), next: [], level: "high"};
    await wait(1100);
    Wv.run(1000);
    check("worlds: a song without an energy word follows the level (high: flowers)", s2.world === "flowers" && !Wv.errors.length, [s2.world, Wv.errors]);
    Wv.dom.window.close();

    // admin: the Visuals menu and the Kaleidoscope switch take effect, are kept in this browser, and ride on the Presenter link
    const A = vis("/", () => base);
    await wait(300);
    const sel = A.D.getElementById("vis"), kal = A.D.getElementById("kaleido"), aG = A.G();
    check("admin visuals: Auto by default, kaleidoscope off, world from the song", sel.value === "auto" && kal.getAttribute("aria-checked") === "false"
      && aG.stats.world === "koi" && aG.kaleido === false, [sel.value, aG.stats.world, aG.kaleido]);
    sel.value = "flowers"; sel.dispatchEvent(new A.dom.window.Event("change"));
    kal.click();
    const saved = JSON.parse(A.dom.window.localStorage.getItem("algo-visuals")), link = A.D.querySelector('nav.tabs a[data-to="screen"]').getAttribute("href");
    check("admin visuals: Flowers + Kaleidoscope apply at once, are stored, and the Presenter link carries them",
      aG.stats.world === "flowers" && aG.kaleido === true && kal.getAttribute("aria-checked") === "true" && saved.world === "flowers" && saved.kaleido === true
      && link === `/p/${CODE}/screen?world=flowers&kaleido=1`, [aG.stats.world, aG.kaleido, saved, link]);
    A.dom.window.close();
    const U = vis("/presenter?world=lanterns&kaleido=1", () => base);
    await wait(300);
    check("presenter: ?world=lanterns&kaleido=1 picks the world and turns on the kaleidoscope", U.G().stats.world === "lanterns" && U.G().kaleido === true, [U.G().stats.world, U.G().kaleido]);
    U.dom.window.close();
    const Sv = vis("/presenter", () => base, {stored: {world: "holi", kaleido: false}});
    await wait(300);
    check("presenter: without URL params it follows this browser's choice", Sv.G().stats.world === "holi", Sv.G().stats.world);
    Sv.dom.window.close();
    const Gv = vis("/guest?world=holi&kaleido=1", () => base, {stored: {world: "flowers", kaleido: true}});
    await wait(300);
    check("guest (old server, no visuals in state): Auto, no kaleidoscope", Gv.G().stats.world === "koi" && Gv.G().kaleido === false, [Gv.G().stats.world, Gv.G().kaleido]);
    Gv.dom.window.close();
    for (const path of ["/guest", "/presenter"]) {   // the admin's pick reaches every screen through /state
      const Sy = vis(path, () => ({...base, visuals: {world: "holi", kaleido: true}}), {stored: {world: "flowers", kaleido: false}});
      await wait(300);
      check(`${path.slice(1)}: follows the admin's visuals from the server (Holi + kaleidoscope)`, Sy.G().stats.world === "holi" && Sy.G().kaleido === true, [Sy.G().stats.world, Sy.G().kaleido]);
      Sy.dom.window.close();
    }

    // no pulse (older server) or pulse: null falls back to the BPM beat, without errors
    for (const [name, st0] of [["no pulse key", base], ["pulse null", {...base, pulse: null}], ["pulse without beats", {...base, pulse: {server_s: 3, beats: []}}]]) {
      const F = vis("/presenter", () => st0);
      await wait(300);
      F.run(2000);
      const fs = F.G().stats;
      check(`fallback (${name}): beats from the BPM (97 BPM: 3 in 2 s), no errors`, fs.mode === "bpm" && fs.beats >= 3 && fs.beats <= 4 && !F.errors.length
        && F.D.getElementById("err").textContent === "", [fs.mode, fs.beats, F.errors]);
      F.dom.window.close();
    }

    // reduced motion: no animation loop, only still frames, one per song, swapped with a slow fade
    let cs = base;
    const R = vis("/presenter", () => cs, {calmMotion: true});
    await wait(300);
    const rs = R.G().stats;
    check("reduced motion: no animation frames requested or drawn; one still frame", R.dom.window.rafs === 0 && rs.frames === 0 && rs.stills === 1, [R.dom.window.rafs, rs.frames, rs.stills]);
    await wait(1100);
    check("reduced motion: the same song again paints nothing new", rs.stills === 1, rs.stills);
    cs = {...base, now: entry("Malhari - Bajirao Mastani", {energy: "max"})};
    await wait(2600);
    check("reduced motion: a new song gets a new still after a slow fade, still no frames", rs.stills === 2 && rs.world === "holi" && rs.frames === 0 && R.dom.window.rafs === 0
      && /opacity 1.2s/.test(R.D.getElementById("garden").style.transition) && !R.errors.length, [rs.stills, rs.world, rs.frames, R.errors]);
    R.dom.window.close();

    // mood theme: the picked vibe first, else the picked mood, else the song's own mood; one m-* class on <html>, its colours and the visual's weather
    let ms = {...base, vibe: "romantic", mood: "romantic", level: "medium"};
    const Mv = vis("/guest", () => ms);
    await wait(300);
    const H = Mv.D.documentElement, theme = () => [...H.classList].filter(c => c.startsWith("m-")).join(),
      acc = () => Mv.dom.window.getComputedStyle(H).getPropertyValue("--acc").trim().toUpperCase();
    const seen = [[theme(), acc(), Mv.G().mood]];
    for (const [st, want] of [[{vibe: "sad", mood: "sad", level: "low"}, "m-sad"], [{vibe: null, mood: "auto", level: "low", now: entry("X - y", {mood: "chill"})}, "m-chill"],
      [{vibe: null, mood: "party", level: "max"}, "m-peak"], [{vibe: null, mood: "auto", level: "high", now: entry("Z - y", {mood: "bright/driving"})}, "m-party"]]) {
      ms = {...base, ...st};
      await wait(1100);
      seen.push([theme(), acc(), Mv.G().mood, want]);
    }
    check("mood theme: romantic, sad, chill (song's mood), peak (party at max), party (unknown mood, high level), one class at a time",
      seen.map(x => x[0]).join() === "m-romantic,m-sad,m-chill,m-peak,m-party" && seen.every(x => x[0] === "m-" + x[2]), seen);
    check("mood theme: each mood sets its own accent colour", seen[0][1] === "#FF7AB6" && seen[1][1] === "#9DB0E8" && new Set(seen.map(x => x[1])).size === 5, seen.map(x => x[1]));
    check("mood theme: no script or display errors", !Mv.errors.length && Mv.D.getElementById("err").textContent === "", [Mv.errors, Mv.D.getElementById("err").textContent]);
    Mv.run(3000);
    check("mood theme: peak/party weather runs in the visual without errors", !Mv.errors.length && Mv.G().stats.frames > 100, [Mv.errors, Mv.G().stats.frames]);
    Mv.dom.window.close();
  }

  console.log(failures ? `\n${failures} FAILED` : "\nALL PASS");
  process.exit(failures ? 1 : 0);
})();
