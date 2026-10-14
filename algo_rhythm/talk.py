"""Talk to the DJ (text only): turn what the listener types, in English or Hinglish, into DJ actions.

    result = talk.interpret("thoda slow karo", state, songs)   # {"reply", "actions", "source"}
    done = talk.apply(result, set_level=..., request=..., skip=..., play=..., pause=...)

Gemini picks songs by their number in the list we send, never by a free-text name, so every request is a real file.
Only a shortlist goes out: songs named in the text plus the 20 closest in meaning (search.nearest); the whole library
when there are no vectors or the query cannot be embedded. When the gateway fails (quota, network, bad answer) simple
keyword rules take over.
"""
import difflib
import re

from algo_rhythm import dj, genai, search
from algo_rhythm.search import LEVEL_NAMES, song_level

PROMPT_VERSION = "talk-v3"
MOOD_NAMES = ["auto"] + list(dj.MOOD_WORDS)  # live_plan.MOODS
SHORTLIST = 20
COMMANDS = ["none", "skip", "play", "pause"]
MAX_REQUESTS = 3
SCHEMA = {"type": "OBJECT", "required": ["reply", "level", "songs", "command"], "properties": {
    "reply": {"type": "STRING"},
    "level": {"type": "STRING", "enum": ["none"] + LEVEL_NAMES},
    "mood": {"type": "STRING", "enum": ["none"] + MOOD_NAMES},
    "songs": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    "command": {"type": "STRING", "enum": COMMANDS}}}

PROMPT = """You are the assistant of a live Bollywood DJ. A listener typed a message (English, Hindi or Hinglish).
Turn it into DJ actions:
 level: the energy to move to, or "none" to keep it. low = chill, slow, sad; medium = groove, romantic; high = party, dance;
   max = peak-hour banger. "thoda slow karo" / "slow down" = one level lower than now; "aur tez" / "speed up" = one higher.
 mood: the crowd mood to hold, or "none" to keep it: party, romantic, sad, devotional (bhakti, sufi), chill; "auto" =
   stop holding a mood and follow the room. "kuch romantic bajao" = romantic; "sad songs" = sad; "party mood" = party.
 songs: numbers from the library list below for songs the listener asked for (at most 3), best first. Use only numbers
   from the list. Asked for a kind of song ("kuch romantic bajao") = pick up to 3 that fit. A song described by its
   film, actor or scene counts too. Nothing asked = [].
 command: "skip" (next song), "pause", "play" (resume), or "none".
 reply: one short friendly sentence for the screen saying what you will do, in the listener's language style.
Now: level {level}, mood {mood}, status {status}, playing {now}, up next {next}.
Library{order} (number|song|energy level|style|moment):
{library}
Listener: {text}
"""


def clean_name(name):
    """'Abrar_S_Entry_Jamal_Kudu [0Vw9n1Y1iMA]' -> 'Abrar S Entry Jamal Kudu' (YouTube file names)."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", name).replace("_", " ").strip()


def library_lines(songs, idx=None):
    """One line per song; the number is its index in `songs`, also when only the shortlist `idx` is sent."""
    rows = []
    for i in range(len(songs)) if idx is None else idx:
        s = songs[i]
        llm = s.get("llm") or {}
        rows.append(f"{i}|{clean_name(s['name'])}|{song_level(s)}|{llm.get('style', '-')}|{llm.get('moment', '-')}")
    return "\n".join(rows)


def shortlist(text, songs, embed=None):
    """Indices of the songs to send: titles named in the text, then the SHORTLIST closest in meaning.
    None (send the whole library) when there are no vectors or the text cannot be embedded."""
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    named = [i for i, s in enumerate(songs)
             if len(title := clean_name(s["name"]).split(" - ")[0].lower()) >= 4 and f" {title} " in t]
    try:
        near = search.nearest(text, songs, SHORTLIST, embed=embed)
    except genai.GenAIError:
        near = []
    return (named + [i for i in near if i not in named]) if near else None


def interpret(text, state, songs, *, generate=genai.generate, cache=None, on_call=None, embed=None):
    """What the listener wants -> {"reply", "actions", "source": "gemini"|"rules"}. Never raises on gateway failure.
    The state is part of the prompt, so it is part of the cache key: the same words in another state ask again."""
    idx = shortlist(text.strip()[:300], songs, embed)
    prompt = PROMPT.format(level=state.get("level"), mood=state.get("mood") or "auto", status=state.get("status"), now=state.get("now") or "nothing",
                           next=", ".join(state.get("next") or []) or "nothing", library=library_lines(songs, idx),
                           order="" if idx is None else ", closest to the listener's words first",
                           text=text.strip()[:300])
    try:
        a = generate("talk", PROMPT_VERSION, [{"text": prompt}], SCHEMA, cache=cache,
                     **({"on_call": on_call} if on_call else {}))
    except genai.GenAIError:
        return rules(text, state, songs)
    actions = []
    if a["level"] != "none":
        actions.append({"type": "set_level", "level": a["level"]})
    if a.get("mood", "none") != "none":
        actions.append({"type": "set_mood", "mood": a["mood"]})
    picked = []
    sent = range(len(songs)) if idx is None else set(idx)
    for i in a["songs"]:
        if i in sent and i not in picked:  # numbers we did not send are dropped
            picked.append(i)
    actions += [{"type": "request", "file": songs[i]["file"]} for i in picked[:MAX_REQUESTS]]
    if a["command"] != "none":
        actions.append({"type": a["command"]})
    return {"reply": a["reply"].strip()[:200] or "Okay.", "actions": actions, "source": "gemini"}


# -- rules fallback: keywords, checked on whole words of the lowercased text
PAUSE = r"\b(pause|stop|ruko|band karo|rok do)\b"
SKIP = r"\b(skip|next|agla|change karo|badlo)\b"
PLAY_VERB = r"\b(play|resume|bajao|chalao|lagao|start)\b"
DOWN = r"\b(slow down|thoda slow|dheere|dheema|calm down|tone it down)\b"
UP = r"\b(speed up|aur tez|tez karo|faster|pump it up|turn it up)\b"
KEYWORDS = [(r"\b(peak|banger|dhamaka|max|maximum)\b", "max"), (r"\b(party|dance|nacho|naach)\b", "high"),
            (r"\b(groove|groovy|romantic|romance)\b", "medium"), (r"\b(chill|slow|sad|soft|sukoon)\b", "low")]
MOOD_KEYWORDS = [(r"\b(romantic|romance|pyaar|pyar|love)\b", "romantic"), (r"\b(sad|dard|breakup|heartbreak)\b", "sad"),
                 (r"\b(bhakti|devotional|sufi|qawwali)\b", "devotional"), (r"\b(party|dance|nacho|naach|dhamaka)\b", "party"),
                 (r"\b(chill|sukoon|relax|calm)\b", "chill")]
FILLER = r"\b(play|bajao|chalao|lagao|please|pls|kuch|koi|song|gaana|gana|wala|the|a|some|something|me|for|karo)\b"


def match_song(query, songs, cutoff=0.65):  # 0.6 let "romantic" match "Om Shanti Om"
    """Index of the song whose name best matches `query` (difflib), or None."""
    q = re.sub(r"\s+", " ", re.sub(FILLER, " ", query)).strip()
    if len(q) < 3:
        return None
    best, best_i = 0.0, None
    for i, s in enumerate(songs):
        name = clean_name(s["name"]).lower()
        parts = [name] + [p.strip() for p in name.split(" - ")]
        score = 1.0 if q in name else max(difflib.SequenceMatcher(None, q, p).ratio() for p in parts)
        if score > best:
            best, best_i = score, i
    return best_i if best >= cutoff else None


def rules(text, state, songs):
    """Keyword fallback when the AI is unavailable."""
    t = " " + text.lower().strip() + " "
    level, did, actions = state.get("level") or "medium", [], []
    cur = LEVEL_NAMES.index(level) if level in LEVEL_NAMES else 1
    if re.search(DOWN, t):
        new = LEVEL_NAMES[max(0, cur - 1)]
    elif re.search(UP, t):
        new = LEVEL_NAMES[min(3, cur + 1)]
    else:
        new = next((lv for pat, lv in KEYWORDS if re.search(pat, t)), None)
    if new and new != level:
        actions.append({"type": "set_level", "level": new})
        did.append(f"vibe {new}")
    mood = next((m for pat, m in MOOD_KEYWORDS if re.search(pat, t)), None)
    if mood and mood != state.get("mood"):
        actions.append({"type": "set_mood", "mood": mood})
        did.append(f"mood {mood}")
    if re.search(PAUSE, t):
        actions.append({"type": "pause"})
        did.append("pause")
    elif re.search(SKIP, t):
        actions.append({"type": "skip"})
        did.append("skip")
    elif re.search(PLAY_VERB, t):
        i = match_song(re.sub(PLAY_VERB, " ", t), songs)
        if i is not None:
            actions.append({"type": "request", "file": songs[i]["file"]})
            did.append(f"request {clean_name(songs[i]['name'])}")
        elif state.get("status") != "playing":
            actions.append({"type": "play"})
            did.append("play")
    what = ", ".join(did) if did else "nothing I could understand"
    return {"reply": f"The AI is unavailable, so I used simple rules: {what}.", "actions": actions, "source": "rules"}


def apply(result, *, set_level, request, skip, play, pause, set_mood=None):
    """Run the actions through the given callables, in order. Returns what was done (one line per action)."""
    done = []
    for a in result["actions"]:
        try:
            if a["type"] == "set_level":
                set_level(a["level"])
                done.append(f"level {a['level']}")
            elif a["type"] == "set_mood" and set_mood:
                set_mood(a["mood"])
                done.append(f"mood {a['mood']}")
            elif a["type"] == "request":
                request(a["file"])
                done.append(f"requested {a['file']}")
            elif a["type"] in ("skip", "play", "pause"):
                {"skip": skip, "play": play, "pause": pause}[a["type"]]()
                done.append(a["type"])
        except Exception as e:  # one bad action (e.g. an unknown song) must not stop the rest
            done.append(f"{a['type']} failed: {e}")
    return done
