"""Database schema (SQLAlchemy Core). Postgres in production (Neon), SQLite allowed for local dev.

Every user-owned table carries user_id (FK users.id ON DELETE CASCADE) and every query in app/db.py filters by
the current request's user (app/db.py: current_user_id()). Deleting a users row removes all of that user's data.
Schema changes go through Alembic (backend/migrations); keep this file and the newest migration in sync.
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, MetaData,
                        PrimaryKeyConstraint, String, Table, Text, text)

metadata = MetaData()


def _uid(nullable: bool = False) -> Column:
    return Column("user_id", String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable, index=True)


# ------------------------------------------------------------------ accounts / auth
users = Table(
    "users", metadata,
    Column("id", String(40), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),       # stored lower-cased
    Column("email_verified_at", DateTime(timezone=True)),
    Column("name", String(200)),
    Column("first_name", String(100)),
    Column("password_hash", Text),                                   # argon2id; NULL = Google-only account
    Column("token_version", Integer, nullable=False, server_default="0"),  # bump = revoke all sessions
    Column("failed_logins", Integer, nullable=False, server_default="0"),
    Column("locked_until", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

oauth_accounts = Table(
    "oauth_accounts", metadata,
    Column("provider", String(40), nullable=False),
    Column("provider_account_id", String(255), nullable=False),
    _uid(),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("provider", "provider_account_id"),
)

auth_tokens = Table(  # single-use email verification / password reset tokens (only the sha256 is stored)
    "auth_tokens", metadata,
    Column("token_hash", String(64), primary_key=True),
    _uid(),
    Column("purpose", String(20), nullable=False),                   # verify_email | reset_password
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

rate_events = Table(  # sliding-window rate limiting shared by all serverless instances
    "rate_events", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("key", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Index("idx_rate_events_key_ts", "key", "created_at"),
)

# ------------------------------------------------------------------ closet data (all per user)
settings = Table(
    "settings", metadata,
    Column("user_id", String(40), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("data", Text, nullable=False),
    Column("updated_at", Text),
)

photos = Table(
    "photos", metadata,
    Column("id", String(40), primary_key=True),
    _uid(),
    Column("path", Text, nullable=False),                            # relative to MEDIA_DIR: u/<user>/originals/x.jpg
    Column("purpose", String(20)),
    Column("width", Integer), Column("height", Integer),
    Column("source", String(40)),
    Column("created_at", Text, nullable=False),
)

items = Table(
    "items", metadata,
    Column("id", String(40), primary_key=True),
    _uid(),
    Column("status", String(20), nullable=False),                    # closet | candidate | detected | suggestion
    Column("category", String(40)),
    Column("attributes", Text, nullable=False, server_default="{}"),
    Column("photo_id", String(40), ForeignKey("photos.id", ondelete="SET NULL")),
    Column("bbox", Text),
    Column("label", Text),
    Column("crop_path", Text), Column("cutout_path", Text), Column("white_path", Text), Column("context_path", Text),
    Column("source", String(40)),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text),
    Index("idx_items_user_status", "user_id", "status", "category"),
)

item_embeddings = Table(
    "item_embeddings", metadata,
    Column("item_id", String(40), ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(20), nullable=False),                      # fclip | compat
    _uid(),
    Column("model", Text, nullable=False),
    Column("dim", Integer, nullable=False),
    Column("vector", LargeBinary, nullable=False),                   # float32 bytes; FAISS is rebuilt from these
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("item_id", "kind"),
)

compat_edges = Table(
    "compat_edges", metadata,
    _uid(),
    Column("outfit_key", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("item_a", String(40), nullable=False), Column("item_b", String(40)), Column("item_c", String(40)),
    Column("n_items", Integer, nullable=False),
    Column("score", Float, nullable=False),
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("user_id", "outfit_key", "model"),
)

evaluations = Table(
    "evaluations", metadata,
    Column("id", String(40), primary_key=True),
    _uid(),
    Column("candidate_item_id", String(40), ForeignKey("items.id", ondelete="CASCADE")),
    Column("price", Float),
    Column("verdict", String(20)),
    Column("results", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

outfits = Table(
    "outfits", metadata,
    Column("id", String(40), primary_key=True),
    _uid(),
    Column("evaluation_id", String(40), ForeignKey("evaluations.id", ondelete="CASCADE")),
    Column("template", Text, nullable=False),
    Column("score", Float),
    Column("created_at", Text, nullable=False),
)

outfit_items = Table(
    "outfit_items", metadata,
    Column("outfit_id", String(40), ForeignKey("outfits.id", ondelete="CASCADE"), nullable=False),
    Column("item_id", String(40), ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
    _uid(),
    Column("position", Integer),
    PrimaryKeyConstraint("outfit_id", "item_id"),
)

suggestions = Table(  # "Should I buy?" shopping suggestions cache, per evaluation
    "suggestions", metadata,
    Column("evaluation_id", String(40), ForeignKey("evaluations.id", ondelete="CASCADE"), primary_key=True),
    _uid(),
    Column("mode", String(20), nullable=False),
    Column("results", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

item_renders = Table(
    "item_renders", metadata,
    Column("item_id", String(40), ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    _uid(),
    Column("status", String(20), nullable=False),
    Column("method", String(20)), Column("clean_path", Text), Column("model", Text), Column("checks", Text),
    Column("embed_source", String(20)), Column("error", Text),
    Column("created_at", Text, nullable=False), Column("updated_at", Text, nullable=False),
)

item_render_details = Table(
    "item_render_details", metadata,
    Column("item_id", String(40), ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    _uid(),
    Column("details", Text, nullable=False),
    Column("frame", String(10), nullable=False),
    Column("model", Text),
    Column("created_at", Text, nullable=False),
)

wardrobe_suggestions = Table(  # "Worth a look" cache, one row per user (keyed by closet contents)
    "wardrobe_suggestions", metadata,
    _uid(),
    Column("closet_key", String(64), nullable=False),
    Column("results", Text, nullable=False),
    Column("created_at", Text),
    PrimaryKeyConstraint("user_id", "closet_key"),
)

USER_DATA_TABLES = (wardrobe_suggestions, item_render_details, item_renders, suggestions, outfit_items, outfits,
                    evaluations, compat_edges, item_embeddings, items, photos, settings)
