"""Database tables: PostgreSQL in production (Cloud SQL), SQLite for local runs and tests.

No migrations: create_all() on start makes any missing table. Add Alembic once the prod schema starts changing.
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect,
                        text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

Json = JSON().with_variant(JSONB, "postgresql")
try:  # pgvector's column type on PostgreSQL when the package is installed; JSON lists everywhere else (SQLite)
    from pgvector.sqlalchemy import Vector
    VectorType = JSON().with_variant(Vector(), "postgresql")
except ImportError:
    Vector, VectorType = None, Json


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Song(Base):
    __tablename__ = "songs"
    id: Mapped[int] = mapped_column(primary_key=True)
    file: Mapped[str] = mapped_column(Text, unique=True)  # path relative to the library, e.g. phase3-dance/x.mp3
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SongAnalysis(Base):
    __tablename__ = "song_analysis"
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id"), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    tag: Mapped[dict] = mapped_column(Json)  # the whole tag file (TAGGING_CONTRACT.md)


class SongAI(Base):
    __tablename__ = "song_ai"
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id"), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    answer: Mapped[dict] = mapped_column(Json)  # what enrich.py saves in LIBRARY/enrich/<key>.json
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SongFeature(Base):
    __tablename__ = "song_features"
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # rhythm_feel | grid_slip | vocal_segments
    data: Mapped[object] = mapped_column(Json, nullable=True)  # null is a real value: no vocal stem


class Party(Base):
    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Play(Base):
    __tablename__ = "plays"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    position: Mapped[int] = mapped_column(Integer)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id"))
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    transition: Mapped[dict | None] = mapped_column(Json, nullable=True)
    why: Mapped[dict | None] = mapped_column(Json, nullable=True)
    clip: Mapped[str | None] = mapped_column(Text, nullable=True)  # the join's FLAC, relative to the library
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SongRequest(Base):
    __tablename__ = "song_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    text: Mapped[str] = mapped_column(Text)
    song_id: Mapped[int | None] = mapped_column(ForeignKey("songs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Feedback(Base):
    """A guest's vote on a play: the song ("song") or the join into it ("transition"). One per guest, target and play."""
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("party_id", "position", "guest", "target"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    position: Mapped[int] = mapped_column(Integer)  # plays.position in this party
    guest: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(16))  # song | transition
    value: Mapped[str] = mapped_column(String(8))  # up | down
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TransitionReview(Base):
    """Gemini's judgement of one join's clip (algo_rhythm.review)."""
    __tablename__ = "transition_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    play_id: Mapped[int] = mapped_column(ForeignKey("plays.id"))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer)  # 1-5
    smooth: Mapped[bool]
    issues: Mapped[list] = mapped_column(Json)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SongEmbedding(Base):
    __tablename__ = "song_embeddings"
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(64))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list] = mapped_column(VectorType)  # search.py: the song's description, embedded


class AICache(Base):
    __tablename__ = "ai_cache"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    request: Mapped[dict] = mapped_column(Json)
    response: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AICall(Base):
    __tablename__ = "ai_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    cached: Mapped[bool]
    ok: Mapped[bool]
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(url):
    """postgresql:// URLs use psycopg 3 (the driver installed); sqlite:///path.db works as is."""
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url, pool_pre_ping=True)


def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)


def create_all(engine):
    """Missing tables. On PostgreSQL the vector column needs the pgvector extension; without it, it is JSONB."""
    if engine.dialect.name == "postgresql" and Vector:
        try:
            with engine.begin() as c:
                c.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:
            SongEmbedding.__table__.c.vector.type = JSONB()
    Base.metadata.create_all(engine)
    # ponytail: the one column added since the first release, by hand; switch to Alembic at the next schema change
    if "clip" not in {c["name"] for c in inspect(engine).get_columns("plays")}:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE plays ADD COLUMN clip TEXT"))
