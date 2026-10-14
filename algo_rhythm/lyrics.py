"""Lyric word links: the next song's first sung line shares a key word with the last line of the song on air
("Mirchi" into a song called Mirchi, "dil" into "dil"), so the DJ joins them right there.

Reads LIBRARY/lyrics/<key>.json written by tools/lyrics.py (Whisper word timestamps). No heavy imports.
Hindi/Punjabi words arrive in Devanagari; English and song titles in Latin. Every word is reduced to one rough
romanised key so that दिल, "dil" and "Dil" all meet, and "pyaar"/"pyar"/प्यार too.
"""
import collections
import json
import re
from pathlib import Path

WINDOW_S = 20.0  # how far before A's exit / after B's entry a word may be sung to count

_CONS = dict(zip("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहळ",
                 "k kh g gh n ch chh j jh n t th d dh n t th d dh n p ph b bh m y r l v sh sh s h l".split()))
_VOW = dict(zip("अआइईउऊऋएऐओऔ", "a aa i ii u uu ri e ai o au".split()))
_MATRA = dict(zip("ािीुूृेैोौॅॉ", "aa i ii u uu ri e ai o au e o".split()))
_NASAL, _VIRAMA, _NUKTA = "ंँ", "्", "़"


def deva_to_latin(word):
    """Rough Hindi romanisation: inherent 'a' after a bare consonant, dropped at the end of the word."""
    out, pending = [], False  # pending: the last consonant still carries its inherent 'a'
    for ch in word:
        if ch == _NUKTA:
            continue
        if ch in _CONS:
            if pending:
                out.append("a")
            out.append(_CONS[ch])
            pending = True
        elif ch in _MATRA or ch == _VIRAMA:
            out.append(_MATRA.get(ch, ""))
            pending = False
        else:
            if pending:
                out.append("a")
            pending = False
            out.append(_VOW.get(ch, "n" if ch in _NASAL else "h" if ch == "ः" else ch))
    return "".join(out)  # a still-pending final 'a' is dropped (schwa deletion): दिल -> dil


_MERGE = (("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"), ("ai", "e"), ("au", "o"),
          ("w", "v"), ("z", "j"), ("ph", "f"), ("q", "k"), ("chh", "ch"), ("sh", "s"))


def norm(word):
    """One key per word, spelling variants merged: 'Pyaar', 'pyar', 'प्यार' -> 'pyar'."""
    w = re.sub(r"[^a-z]", "", deva_to_latin(word.lower()))
    for a, b in _MERGE:
        w = w.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", w)  # dilli -> dili


STOP = {norm(w) for w in """
hai hain ho hoon hu hun mein main me mera meri mere tera teri tere tu tum tumhe tumko tujhe tujhko mujhe mujhko hum
ham humko ka ki ke ko se ne na naa nahi nahin ye yeh wo woh vo jo to bhi hi aur ab kya kyun kyon kaise koi kuch sab
ek is us iss uss jab tab kar karo kare karu karna karke raha rahi rahe gaya gayi gaye tha thi the de do di le lo la
ja jaa aa re o oh haan ha han par pe yahan wahan apna apni apne jaise aisa aise kahin kab kaun hua hui hoga hogi
jae jaye jaaye jaega jayega jaegi jayegi jana jaana aana ana aya aaya ayi aayi hota hote hoti hona vala vali vale
wala wali wale kiya kiye kiyaa diya diye liya liye lijiye lijie dijiye dijie kaha kahe kehna sun suno dekho dekh
bas phir fir tak liye lagta lage lagi laga
kabhi sare sari sara saare saari jane mene maine tune bana ban bar jao jaon aane ane hone kah kahe lie liye karke
karake apana yun yu chahe mile pata har kam bade use unhe inhe usse jise jinhe kis kisi kitna itna itana aaj kal
lekin abhi abhie baki baaki aake ake gae gai apane bich beech jad jado ala vaala tera teri yaar wahi vahi yahi sabhi
da nu tenu menu mainu vich ni nal naal te ve je ae si oye ji tainu sanu ton tan
a an and or you your yours i im me my mine we our us it its is are was were be been to of in on at for with that this
so oh ooh yeah yea hey baby na la da just dont can cant all up down now like get got know no not but if when what
do does did go come out one say said let lets gonna wanna make way
""".split()}


# Whisper's stock output over a silent fade-out (the last line of most Hindi songs): never a real link.
HALLUCINATED = {norm(w) for w in ["झाल", "thank", "thanks", "watching", "subscribe"]}  # English: "Thank you."
MAX_KEY = 14  # a longer key is a decoding loop run together (आएजाएजाए...), not a word


def keys_of(text):
    """The content-word keys of a line or title (stop words, hallucinations, 1-2 letter keys and loops dropped)."""
    return [k for k in (norm(w) for w in text.split())
            if 3 <= len(k) <= MAX_KEY and k not in STOP and k not in HALLUCINATED]


def song_name(title):
    """The song's own name from a title or file name: 'Chal_Ghar_Chalen_From_Malang_-_Unleash..._feat._Arijit [id]'
    -> 'Chal Ghar Chalen', 'Kesariya - Brahmastra' -> 'Kesariya' (the film, singer and video id are not sung)."""
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", title.replace("_", " "))
    return re.split(r" - |\||\b(?:from|feat|ft|official|movie)\b", t, maxsplit=1, flags=re.I)[0]


def load(path):
    d = json.loads(Path(path).read_text())
    d["keyed"] = [(w[0], k) for w in d["words"] for k in keys_of(w[2])]
    d["title_keys"] = set(keys_of(song_name(d.get("title", ""))))
    return d


def load_all(library):
    """{song key: lyrics} for every lyrics file in LIBRARY/lyrics."""
    return {p.stem: load(p) for p in sorted((Path(library) / "lyrics").glob("*.json"))}


def doc_freq(lyrics):
    """How many songs sing each key: a link on a word every song sings (dil, pyar) is worth less."""
    return collections.Counter(k for d in lyrics.values() for k in {k for _, k in d["keyed"]})


def word_link(a, b, a_exit=None, b_entry=None, df=None, n=0, window=WINDOW_S):
    """(score 0..1, shared word, time in A of its last use before A's exit, time in B of its first use after B's
    entry). A and B are loaded lyrics; exit/entry default to A's last and B's first sung word. A word of B's title
    counts as sung at B's entry (the "Mirchi" lyric into a song called Mirchi). (0.0, None, None, None) if none."""
    if not a["keyed"] or not (b["keyed"] or b["title_keys"]):
        return 0.0, None, None, None
    a_exit = a["words"][-1][1] if a_exit is None else a_exit
    b_entry = (b["words"][0][0] if b["words"] else 0.0) if b_entry is None else b_entry
    last_a = {k: t for t, k in a["keyed"] if a_exit - window <= t <= a_exit}  # later uses overwrite: the last one
    first_b = {}
    for t, k in b["keyed"]:
        if b_entry <= t <= b_entry + window:
            first_b.setdefault(k, t)
    best = (0.0, None, None, None)
    for k, ta in last_a.items():
        if k not in first_b and k not in b["title_keys"]:
            continue
        rare = 1 - df[k] / n if df and n else 0.5
        # B's title word beats a word that is merely sung in both; a word most songs sing (dil) is worth less either way
        score = round(0.6 + 0.4 * rare if k in b["title_keys"] else 0.3 + 0.5 * rare, 3)
        if score > best[0]:
            best = (score, k, ta, first_b.get(k, b_entry))
    return best
