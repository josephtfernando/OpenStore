"""basket details for door-10 merchant requirements

Revision ID: c3d8f1a44b07
Revises: b1f4c7a92e30
Create Date: 2026-09-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8f1a44b07"
down_revision: str | None = "b1f4c7a92e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nullable so pre-A1 rows read back as empty. A one-way door by policy:
    the Ledger is append-only and downgrades that drop columns are refused on
    deploys that took orders (see docs/Deployment.md) — but a basket is working
    state, not evidence, so the downgrade below exists for clean rollbacks of
    this release only."""
    op.add_column("baskets", sa.Column("details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("baskets", "details")
