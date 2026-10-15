"""Incremental planner for the live DJ: plans only the next few songs from where the night is now.

Differences from dj.plan_set (the offline set builder):
  - It starts from the song that is already playing, and plans H songs, not a whole night.
  - The target is one of 4 energy levels the listener can change at any moment.
  - A requested song is placed in whichever of the next 3 slots it blends best from, and the
    songs before it are chosen as a smooth path to it.
  - Gates (match / vibe) loosen in tiers instead of stopping the music: a bridge between two
    different moods must exist, so it is allowed, but the tight tier is always tried first.
"""
import math

import numpy as np

from algo_rhythm import dj

# live: the best bit of the next song first (hook + "best"), its own intro only as a tie-breaker
WEIGHTS = {**dj.WEIGHTS, "hook": 0.5, "build": 0.2, "exit": 1.6, "entry": 1.2, "best": 1.0, "mood_pick": 1.5}
MOODS = ("auto",) + tuple(dj.MOOD_WORDS)
# The one control the listener sees: each vibe is an energy level plus a crowd mood (library medians: sad 0.18,
# chill 0.21, romantic 0.33 (mostly medium), party 0.74).
VIBES = {"chill": ("low", "chill"), "sad": ("low", "sad"), "romantic": ("medium", "romantic"),
         "groove": ("medium", "party"), "party": ("high", "party"), "peak": ("max", "party")}  # the listener's mood choice; auto = follow the room (keep the current mood)
BUILD_EXTRA_S = 30                                   # the opening song plays this much longer so it can build up first
LEVELS = {"low": 0.125, "medium": 0.375, "high": 0.625, "max": 0.875}  # centres of the 4 energy quartiles
H = 4                                      # songs planned ahead
BEAM, BRANCH, SHORTLIST = 10, 4, 14
# (min match, min vibe, beat-matched blend only, min mood). Blends are tried at two strictness levels before any
# echo-out; the crowd's mood holds through all of them (an echo-out into a party song beats a blend into a sad one).
M = dj.MIN_MOOD
TIERS = ((dj.MIN_MATCH, dj.MIN_VIBE, True, M), (0.5, 0.6, True, M), (0.5, 0.6, False, M), (0.0, 0.0, False, 0.0))
BAND = 0.175                               # a level holds songs within this of its centre (quartile half-width 0.125 + slack)
SMOOTH_JOIN, SMOOTH_CLEAN = 0.65, 0.5      # below either, the planner searches every part of the song, not just the best few
QUIET_EDGE_DB = 8                          # a window may not start or end this far below its song's typical level
DIP_EDGE_DB = 10                           # nor may an edge phrase contain a stop this far below the song's level
ECHO_MAX_DROP_DB = 4                      # incoming start may be at most this much quieter than the outgoing end
# The night is Bollywood: an international song (lists/english_50.tsv, in tracklist/english/) plays only where it
# beat-matches into the song on air at least this well, at most one in every INTL_GAP + 1 songs, and loses any close
# call to a Bollywood song. (Without the gap a long session played one every 2-3 songs: played Bollywood songs carry a
# freshness penalty that fresh English songs do not.)
INTL_DIR = "tracklist/english/"
INTL_MIN_MATCH = 0.75
INTL_PENALTY = 0.5
INTL_GAP = 4
JOIN_W = 1.5                               # extra weight on how well the request itself joins in: it is the song the listener asked for
TOWARD_W = 2.0                             # weight on how well the song before a request hands over to it
LATE_PENALTY = 0.05                        # per slot a request waits, so equal fits go earlier
REQUEST_SLOTS = (1, 2, 3)                  # slot 1 = first song not yet committed to the audio
# A request has a deadline: it must play within these slots of when it was made. As songs are
# committed the allowed slots shrink, otherwise the planner keeps finding a "better" later slot.


def ramp(a, b, k, n):
    return a + (b - a) * min(1.0, k / n)


# Variety (only when the planner has an rng: the web app, one seed per party). The opener is a weighted pick among the
# songs scoring within OPEN_MARGIN of the best (OPEN_TOP at most); the next song among plans within NEXT_MARGIN of the
# best total. Weights exp((score - best) / temperature): the best stays the likeliest, a clearly better choice wins.
OPEN_MARGIN, OPEN_TOP, OPEN_TEMP = 0.25, 8, 0.08
NEXT_MARGIN, NEXT_TEMP = 0.12, 0.05


def weighted_pick(rng, options, temp):
    """One of options [(score, item), ...] (best first), the higher scores likelier."""
    top = options[0][0]
    return rng.choices([o[1] for o in options], weights=[math.exp((o[0] - top) / temp) for o in options])[0]


class Planner:
    def __init__(self, songs, excerpt_s):
        self.songs = songs
        self.excerpt_s = excerpt_s
        self.win = [dj.candidate_windows(s, excerpt_s, (), force_first=True, flex=True) for s in songs]
        self._base, self._wide = {}, {}
        self._level_e, self._penalty = 0.375, {}
        self.recent = []  # the songs played so far, in order (Session._plan): spaces out the international songs
        self.mood = None  # a mood family the listener picked (MOODS); None = auto. Held like the energy level.
        self.rng = None   # random.Random: vary the opener and near-tie next songs; None = always the best (offline, tests)

    def add_song(self, i):
        """A song was appended to `songs` (and the library ranks recomputed): give it windows, drop stale caches."""
        self.win.append(dj.candidate_windows(self.songs[i], self.excerpt_s, (), force_first=True, flex=True))
        self._base.clear()
        self._wide.clear()

    def all_windows(self, i):
        """Every start position and every length of song i: where a mix can go when the best-hook windows don't fit."""
        if i not in self._wide:
            s = self.songs[i]
            out = dj.candidate_windows(s, self.excerpt_s, (), keep=10 ** 6, spacing=1, force_first=True, flex=True)
            self._wide[i] = out
        return self._wide[i]

    def base(self, prev, i):
        """(terms, kind, why, match) for song i after song `prev` (indexes; prev may be None). Cached."""
        key = (prev, i)
        if key not in self._base:
            terms, kind, why = dj.pair_base(self.songs[prev] if prev is not None else None, self.songs[i])
            self._base[key] = (terms, kind, why, dj.pair_score(terms))
        return self._base[key]

    def picked(self, i):
        """0..1: how well song i fits the mood the listener picked (1 = that mood); None when on auto."""
        fam = dj.mood_family(self.songs[i].get("llm"))
        return None if self.mood is None else 0.5 if fam is None else dj.family_fit(fam, self.mood)

    def intl(self, i):
        return self.songs[i]["file"].startswith(INTL_DIR)

    def first_song(self, level_e, start=None, penalty=None):
        """Opening song: right energy for the level, played from its own beginning (longer, so it builds up
        before any mixing starts), preferring a calm start with little singing over it."""
        best, per_song = None, {}
        for i, s in enumerate(self.songs):
            if start and start.lower() not in s["file"].lower():
                continue
            if not start and self.intl(i):  # the night opens Bollywood
                continue
            for w in dj.candidate_windows(s, self.excerpt_s + BUILD_EXTRA_S, (), force_first=True):
                if not w["natural_start"]:
                    continue
                sc = math.exp(-((s["energy_pct"] - level_e) / 0.2) ** 2) + 0.25 * w["in_q"] + 0.2 * (1 - w["in_vocal"])
                sc -= (penalty or {}).get(i, 0.0)  # songs heard before are less likely to open the night
                sc += 0.0 if self.mood is None else self.picked(i)  # and the night opens in the mood picked
                if best is None or (sc, -i) > best[0]:
                    best = ((sc, -i), i, w)
                if i not in per_song or sc > per_song[i][0]:
                    per_song[i] = (sc, w)
        if best is None:
            raise ValueError(f"no song matches --start {start!r}")
        if self.rng is not None and not start:  # a different good opener each night, not always the single best
            ranked = sorted(((sc, (i, w)) for i, (sc, w) in per_song.items()), key=lambda o: (-o[0], o[1][0]))
            ranked = [o for o in ranked if o[0] >= ranked[0][0] - OPEN_MARGIN][:OPEN_TOP]
            i, w = weighted_pick(self.rng, ranked, OPEN_TEMP)
            return self.entry(i, w, {}, "start", "opening track", 1.0, 0.0)
        return self.entry(best[1], best[2], {}, "start", "opening track", 1.0, 0.0)

    def entry(self, i, w, terms, kind, why, match, score, request=False):
        return {"song": i, "window": w, "terms": terms, "transition_in": kind, "why": why,
                "match": round(match, 2), "score": score, "request": request}

    # ------------------------------------------------------------ beam search

    def _expand(self, st, goal, played, forced=None, toward=None):
        prev, prev_w = st["prev"], st["prev_w"]
        pool = [forced] if forced is not None else [i for i in range(len(self.songs))
                                                    if i not in played and i not in st["used"]]
        bases = {i: self.base(prev, i) for i in pool}
        ok = pool
        cur_e = self.songs[prev]["energy_pct"] if prev is not None else goal
        far = abs(cur_e - goal) > 0.15  # the listener wants a different energy: every step must move toward it
        step = min(0.22, 0.5 * abs(cur_e - goal))  # a DJ climbs in 2-3 songs, not 5
        in_band = lambda i: abs(self.songs[i]["energy_pct"] - self._level_e) <= BAND
        in_mood = lambda i: self.mood is None or self.picked(i) == 1.0
        last = (list(self.recent) + [e["song"] for e in st["chain"]])[-INTL_GAP:]
        intl_ok = lambda i: not any(self.intl(j) for j in last) and bases[i][1] == "blend" and bases[i][3] >= INTL_MIN_MATCH
        if toward is not None:  # the song just before a request: chosen for how well it hands over to the request
            far = False
            in_band = in_mood = lambda i: True
        # stay in the chosen level's band and the picked mood; a blend that only keeps the crowd's mood (MIN_MOOD) beats
        # an echo-out inside the picked one (holding the mood alone made Chill 35% echo-outs), then leave the band
        for use_band, use_mood, blends in ((True, True, True), (True, False, True), (True, True, False), (True, False, False),
                                           (False, False, False)):
            for min_match, min_vibe, blend_only, min_mood in ([(0.0, 0.0, False, 0.0)] if forced is not None or toward is not None else TIERS):
                if blends and not blend_only:
                    continue
                ok = [i for i in pool if bases[i][3] >= min_match and bases[i][0]["vibe"] >= min_vibe
                      and bases[i][0]["mood"] >= min_mood
                      and (not blend_only or bases[i][1] == "blend")
                      and (forced is not None or (in_band(i) if use_band and not far else True))
                      and (forced is not None or not use_mood or in_mood(i))
                      and (forced is not None or not self.intl(i) or intl_ok(i))
                      and (forced is not None or not far
                           or abs(self.songs[i]["energy_pct"] - goal) <= abs(cur_e - goal) - step)]
                if ok:
                    break
            if ok or forced is not None or toward is not None:
                break
        if forced is None:
            fit = lambda i: bases[i][3] + math.exp(-((self.songs[i]["energy_pct"] - goal) / 0.2) ** 2) \
                + (0 if toward is None else TOWARD_W * self.base(i, toward)[3])
            ok = sorted(ok, key=lambda i: (-fit(i), i))[:SHORTLIST]
        out = self._score(ok, bases, prev_w, goal, forced, strict=True) or self._score(ok, bases, prev_w, goal, forced)
        return sorted(out, reverse=True)[:BRANCH]

    def _score(self, ok, bases, prev_w, goal, forced, strict=False):
        out = []
        for i in ok:
            terms0, kind, why, match = bases[i]
            g = self.songs[i]["energy_pct"] if forced is not None else goal  # a request is never "off the curve"

            def best_of(windows):
                best = None
                for w in windows:
                    # A quiet edge (fading outro, silent break) makes a hole in the mix. A song's own start is exempt:
                    # a quiet intro is natural, and the engine lifts it to the outgoing level.
                    if w["last_rel"] < -QUIET_EDGE_DB or (not w["natural_start"] and w["first_rel"] < -QUIET_EDGE_DB):
                        continue
                    # A stop inside the 16 s that overlap the next song leaves a hole in the mix.
                    if w["out_low_rel"] < -DIP_EDGE_DB or (not w["natural_start"] and w["in_low_rel"] < -DIP_EDGE_DB):
                        continue
                    # An echo-out is a hard cut: if the incoming start is much quieter than the outgoing end it leaves a hole.
                    if strict and kind == "echo" and prev_w and prev_w["last_db"] is not None \
                            and w["first_db"] is not None and prev_w["last_db"] - w["first_db"] > ECHO_MAX_DROP_DB:
                        continue
                    terms = {**terms0, **dj.window_terms(self.songs[i], g, w, prev_w, swap_ok=kind == "blend"),
                             "build": 1.0 if w["natural_start"] else 0.0}
                    if self.mood is not None and forced is None:  # a request is never "off the mood"
                        terms["mood_pick"] = self.picked(i)
                    score = sum(WEIGHTS[k] * v for k, v in terms.items())
                    if best is None or score > best[0]:
                        best = (score, w, terms)
                return best

            best = best_of(self.win[i])
            if best is None or best[2]["join"] < SMOOTH_JOIN or best[2]["clean"] < SMOOTH_CLEAN:
                wide = best_of(self.all_windows(i))  # the usual few windows do not mix smoothly: look everywhere
                if wide and (best is None or wide[0] > best[0]):
                    best = wide
            if best:
                fresh = 0.0 if forced is not None else self._penalty.get(i, 0.0)  # a request is never held back for being old
                fresh += INTL_PENALTY if forced is None and self.intl(i) else 0.0
                out.append((best[0] - fresh, -i, i, best[1], best[2], kind, why, match))
        return out

    def _beam(self, init, goals, played, forced_slot=None, forced_song=None):
        states = [init]
        for k, goal in enumerate(goals, 1):
            forced = forced_song if k == forced_slot else None
            toward = forced_song if forced_slot and k == forced_slot - 1 else None
            children = []
            for st in states:
                for score, _, i, w, terms, kind, why, match in self._expand(st, goal, played, forced, toward):
                    if toward is not None:
                        score += TOWARD_W * self.base(i, toward)[3]
                    e = self.entry(i, w, terms, kind, why, match, score, request=forced is not None)
                    children.append({"chain": st["chain"] + [e], "total": st["total"] + score, "prev": i,
                                     "prev_w": w, "used": st["used"] | {i}})
            if not children:
                break
            children.sort(key=lambda s: (-s["total"], tuple(e["song"] for e in s["chain"])))
            states = children[:BEAM]
        return states

    # ------------------------------------------------------------ public

    def plan(self, prev, prev_w, played, level_e, request=None, slots=REQUEST_SLOTS, h=H, penalty=None):
        """Next `h` entries after song `prev` (playing window `prev_w`). `played` = song indexes to avoid."""
        self._level_e, self._penalty = level_e, penalty or {}
        init = {"chain": [], "total": 0.0, "prev": prev, "prev_w": prev_w, "used": frozenset()}
        cur_e = self.songs[prev]["energy_pct"]
        if request is None:
            best = self._beam(init, [ramp(cur_e, level_e, k, 1) for k in range(1, h + 1)], played)  # aim at the level from the next song on
            if not best or not best[0]["chain"]:  # no song left that may follow (a small library, all heard lately)
                return []
            if self.rng is not None:  # near-ties for the next song: a weighted pick, so nights do not repeat
                firsts, near = set(), []
                for st in best:
                    if st["chain"] and st["chain"][0]["song"] not in firsts and st["total"] >= best[0]["total"] - NEXT_MARGIN:
                        firsts.add(st["chain"][0]["song"])
                        near.append((st["total"], st))
                pick = weighted_pick(self.rng, near, NEXT_TEMP)
                best = [pick] + [st for st in best if st is not pick]
            chain, seen, alts = best[0]["chain"], {best[0]["chain"][0]["song"]}, []
            for st in best[1:]:  # runners-up for the next slot, for the "under the hood" view
                if st["chain"][0]["song"] not in seen:
                    seen.add(st["chain"][0]["song"])
                    alts.append({"song": st["chain"][0]["song"], "behind": round(best[0]["total"] - st["total"], 2)})
            chain[0]["alts"] = alts[:3]
            return chain

        r_e = self.songs[request]["energy_pct"]
        played = set(played) | {request}  # free slots must not use the requested song; the forced slot does
        options = []
        for p in slots:
            goals = [ramp(cur_e, r_e, k, p) for k in range(1, p + 1)]
            states = self._beam(init, goals, played, forced_slot=p, forced_song=request)
            if states and len(states[0]["chain"]) == p:
                options.append((states[0]["total"] / p + JOIN_W * states[0]["chain"][p - 1]["match"] - LATE_PENALTY * (p - 1), -p, states[0]))
        if not options:
            return self.plan(prev, prev_w, played, level_e, None, h=h, penalty=penalty)
        _, neg_p, st = max(options, key=lambda o: (o[0], o[1]))
        p = -neg_p
        rest = self._beam(st, [ramp(r_e, level_e, k, 2) for k in range(1, h - p + 1)], played)
        return (rest[0] if rest else st)["chain"]
