"""Size-based margin scaling.

Delta Exchange raises the margin rate as a position grows past
``max_leverage_notional``; the three columns carry that schedule.

Revision ID: 8a41c3d9e2b7
Revises: 5d2b8e01c7f4
Create Date: 2026-10-22 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import hedgelab.db.base  # noqa: F401

revision: str = "8a41c3d9e2b7"
down_revision: str | None = "5d2b8e01c7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("margin_scaling_notional", "initial_margin_scaling", "maintenance_margin_scaling")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column(
            "instruments",
            sa.Column(name, hedgelab.db.base.Money(precision=38, scale=18),
                      nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("instruments", name)
