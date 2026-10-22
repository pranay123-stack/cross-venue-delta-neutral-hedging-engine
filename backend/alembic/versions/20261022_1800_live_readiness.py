"""Persisted control state and venue instrument ids.

``control_state`` holds what must survive a crash -- the kill switch and the
API heartbeat the watchdog reads.  ``instruments.venue_instrument_id`` is the
venue's own id (Delta ``product_id``, Hyperliquid ``meta.universe`` index),
which orders are addressed by.

Revision ID: c7e19b5a4d23
Revises: 8a41c3d9e2b7
Create Date: 2026-10-22 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import hedgelab.db.base  # noqa: F401

revision: str = "c7e19b5a4d23"
down_revision: str | None = "8a41c3d9e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()),
                                                  "postgresql"), nullable=False),
        sa.Column("updated_at", hedgelab.db.base.UTCDateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "instruments",
        sa.Column("venue_instrument_id", sa.String(length=64), nullable=False,
                  server_default=""),
    )


def downgrade() -> None:
    op.drop_column("instruments", "venue_instrument_id")
    op.drop_table("control_state")
