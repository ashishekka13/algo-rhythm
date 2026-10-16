"""Seed a finished party for working on the report page (algo_rhythm.report).

    DATABASE_URL=sqlite:///$PWD/report_demo.db python -m tools.seed_report
    DATABASE_URL=sqlite:///$PWD/report_demo.db python -m uvicorn "algo_rhythm.server:create_app" --factory --port 8799
    # open http://127.0.0.1:8799/p/DEMO23/report (in that browser first: localStorage["algo-host-DEMO23"] = "demo-host-key")

A 14-song party with climbing energy, 7 guests, 5 requests (2 unmatched), votes, one planted disagreement (a join the
planner was sure of that the room voted down), Gemini reviews on half the joins (on the harsh side), reactions clustered
at the peak, and 4 trivia rounds. Run it again to start over: the old DEMO23 goes first.

Party codes have no 0/O/1/I (DEMO01 is rejected; DEMO23 is fine), songs.sha256 is NOT NULL, and the host key is
stored hashed (party.hash_key), or the host check never matches.
"""
import datetime
import hashlib
import random

from sqlalchemy import delete, select

from algo_rhythm import db, party, repository

CODE, KEY = "DEMO23", "demo-host-key"
NAMES = ["Kesariya", "Tum Hi Ho", "Raataan Lambiyan", "Kala Chashma", "Badtameez Dil", "London Thumakda", "Gallan Goodiyaan",
         "Nachde Ne Saare", "Kar Gayi Chull", "Balam Pichkari", "Dil Dhadakne Do", "Malhari", "Ghungroo", "Tum Se Hi"]


def main():
    rng = random.Random(23)
    with repository.session() as s:
        old = s.scalar(select(db.Party.id).where(db.Party.code == CODE))
        if old:
            plays = list(s.scalars(select(db.Play.id).where(db.Play.party_id == old)))
            s.execute(delete(db.TransitionReview).where(db.TransitionReview.play_id.in_(plays or [-1])))
            for t in (db.Play, db.Feedback, db.Reaction, db.SongRequest, db.TriviaAnswer, db.GuestAction):
                s.execute(delete(t).where(t.party_id == old))
            s.execute(delete(db.Party).where(db.Party.id == old))
            s.commit()

        songs = list(s.scalars(select(db.Song).where(db.Song.status == "active").order_by(db.Song.id).limit(14)))
        for name in NAMES[len(songs):]:
            song = db.Song(file=f"demo/{name}.mp3", sha256=hashlib.sha256(name.encode()).hexdigest(), name=name, duration_s=210.0)
            s.add(song)
            songs.append(song)
        s.flush()

        start = db.now() - datetime.timedelta(minutes=72)
        st = party.new_state("high")
        st.update(status="ended", ended_at=(start + datetime.timedelta(minutes=70)).timestamp(), location={"city": "Pune", "country": "IN"})
        p = db.Party(level="high", code=CODE, host_key=party.hash_key(KEY), state=st, version=0, updated_at=db.now(),
                     started_at=start - datetime.timedelta(minutes=5))
        s.add(p)
        s.flush()

        guests = [f"demo-guest-{k}" for k in range(7)]
        at = 0.0
        play_rows = []
        for k, song in enumerate(songs):
            seconds = 260.0 + rng.uniform(-40, 50)
            energy = min(1.0, 0.18 + k * 0.058 + rng.uniform(-0.04, 0.04))
            kind = None if k == 0 else ("echo-out" if k in (4, 8, 12) else "blend")
            match = None if k == 0 else round(rng.uniform(0.55, 0.92), 2)
            if k == 9:
                match = 0.84                                     # the planted disagreement
            row = db.Play(party_id=p.id, position=k + 1, song_id=song.id, start_s=20.0, end_s=20.0 + seconds,
                          transition=None if k == 0 else {"type": kind, "overlap_s": 14.0 if kind == "blend" else 6.0},
                          why={"why": "opening track" if k == 0 else f"tempo gap {rng.randint(0, 5)}%, keys close",
                               "match": 1.0 if k == 0 else match, "level": "high" if k > 4 else "medium", "energy": energy},
                          played_at=start + datetime.timedelta(seconds=at))
            s.add(row)
            play_rows.append((row, seconds))
            at += seconds - (0 if k == 0 else (14.0 if kind == "blend" else 6.0))
        s.flush()

        for k, (row, seconds) in enumerate(play_rows):
            pos = k + 1
            if k and k % 2 == 1:                                 # Gemini on half the joins, harsher than the room
                score = 2 if k in (3, 5, 7) else 4 if k != 9 else 5
                s.add(db.TransitionReview(play_id=row.id, model="gemini-demo", prompt_version="demo", score=score, smooth=score >= 4,
                                          issues=[] if score >= 4 else ["vocals clash"], summary="A demo review."))
            voters = rng.sample(guests, rng.randint(3, 6)) if k >= 2 else rng.sample(guests, 2)
            for g in voters:
                up = rng.random() < (0.35 if k in (2, 6) else 0.8)
                s.add(db.Feedback(party_id=p.id, position=pos, guest=g, target="song", value="up" if up else "down"))
            if k:
                for j, g in enumerate(rng.sample(guests, rng.randint(3, 5))):
                    bad = k == 9 and j > 0 or (k in (3, 7) and j == 0)
                    s.add(db.Feedback(party_id=p.id, position=pos, guest=g, target="transition", value="down" if bad else "up"))
            n = int(2 + 22 * max(0.0, 1 - abs(k - 10.5) / 5)) + rng.randint(0, 2)   # the peak: songs 9 to 13
            for _ in range(n):
                s.add(db.Reaction(party_id=p.id, guest=rng.choice(guests), emoji=rng.choice(["🔥", "❤️", "🙌", "💃", "🎉"]),
                                  created_at=row.played_at + datetime.timedelta(seconds=rng.uniform(5, seconds - 5))))

        for text, k in (("kesariya please", 0), ("kala chashma", 3), ("malhari!!", 11), ("Jamal Kudu", None), ("some old kishore", None)):
            s.add(db.SongRequest(party_id=p.id, text=text, song_id=songs[k].id if k is not None else None,
                                 status="played" if k is not None else "unmatched", created_at=start))

        names = ["Priya", "Rahul", "Ishan", "Meera", "Kabir", "Zoya", "Arjun"]
        for rnd in (2, 4, 6, 8):
            for g, name in zip(guests, names):
                if rng.random() < 0.75:
                    right = rng.random() < 0.6
                    secs = round(rng.uniform(1.5, 15), 1)
                    s.add(db.TriviaAnswer(party_id=p.id, round=rnd, trivia_id=0, guest=g, name=name, choice=0, correct=right,
                                          coins=(10 + (5 if secs < 5 else 0)) if right else 0, seconds=secs))
        s.commit()
    print(f"seeded party {CODE}: host key {KEY}")


if __name__ == "__main__":
    main()
