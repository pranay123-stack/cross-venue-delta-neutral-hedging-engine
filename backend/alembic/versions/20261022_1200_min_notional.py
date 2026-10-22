"""Instrument minimum order value.

Hyperliquid rejects any order worth less than $10, independent of the size
lattice, so the minimum is part of the contract specification.

Revision ID: 5d2b8e01c7f4
Revises: ac34f66750a6
Create Date: 2026-10-22 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import hedgelab.db.base  # noqa: F401

revision: str = "5d2b8e01c7f4"
down_revision: str | None = "ac34f66750a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("min_notional", hedgelab.db.base.Money(precision=38, scale=18),
                  nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("instruments", "min_notional")
