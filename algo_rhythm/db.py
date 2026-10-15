"""Database tables: PostgreSQL in production (Cloud SQL), SQLite for local runs and tests.

No migrations: create_all() on start makes any missing table. Add Alembic once the prod schema starts changing.
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect,
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
    """One night. The web player keeps the whole session here (state, algo_rhythm.party), so any server instance can
    answer any request: the server holds nothing between requests but caches of the library."""
    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    code: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)  # what guests type or open
    host_key: Mapped[str | None] = mapped_column(String(64), nullable=True)  # sha256 of the host's secret
    state: Mapped[dict | None] = mapped_column(Json, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # bumped on every change of state
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # users.id of the host who started it


class User(Base):
    """A host account (algo_rhythm.accounts). Signups start inactive until someone approves them by hand."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginSession(Base):
    """A signed-in browser: the sha256 of its cookie's token."""
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthEvent(Base):
    """A failed sign-in or a signup, by address or name: counted for the rate limits."""
    __tablename__ = "auth_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class MCLine(Base):
    """One spoken MC line and its voice, kept for reuse (algo_rhythm.mc_lines). scope: generic (any party), location
    (parties at `place`, e.g. "bengaluru"), personal (a dedication: its own party only, never offered again)."""
    __tablename__ = "mc_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(16), index=True)
    place: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16))
    mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    build: Mapped[bool] = mapped_column(Boolean, default=False)
    voice: Mapped[str] = mapped_column(String(120), index=True)
    audio: Mapped[str | None] = mapped_column(Text, nullable=True)   # the asset's path in the library (bucket)
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16))                  # gemini | templates
    party_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # personal lines: the party they were for
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPlay(Base):
    """A party heard an MC line: lines are not offered again to the same party, or to its host for a week."""
    __tablename__ = "mc_plays"
    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("mc_lines.id"), index=True)
    party_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


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


class Reaction(Base):
    """A guest's emoji, shown rising on every guest and presenter screen for a few seconds."""
    __tablename__ = "reactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), index=True)
    guest: Mapped[str] = mapped_column(String(64))
    emoji: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class GuestAction(Base):
    """One rate-limited action by a guest (a vote): counted over the last minute, kept for the analytics."""
    __tablename__ = "guest_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), index=True)
    guest: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Asset(Base):
    """Audio the server made for the browser (an MC line, a drum fill), stored under STORAGE_URL (or the library)."""
    __tablename__ = "assets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of what made it: the same line is made once
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)  # relative to the library / bucket prefix
    seconds: Mapped[float] = mapped_column(Float)
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
    add_missing_columns(engine)


def add_missing_columns(engine):
    """Columns added to an existing table since it was created (all nullable): ALTER TABLE ADD COLUMN for each.
    ponytail: enough while columns are only ever added; switch to Alembic when one has to change or go."""
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                kind = col.type.compile(dialect=engine.dialect)
                with engine.begin() as c:
                    c.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {kind}'))
                if col.unique:
                    with engine.begin() as c:
                        c.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS ix_{table.name}_{col.name} ON {table.name} ({col.name})'))
