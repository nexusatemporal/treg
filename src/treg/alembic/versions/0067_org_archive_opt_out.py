"""org.archive_opt_out_at / archive_purged_at: a team's archive opt-out and its erasure mark

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-09

Two nullable timestamps on `org`. The first is the moment an admin opted the team out of the
archive (docs/context/architecture/archive.md, "Opting out"); the second is when the erasure
sweep finished removing what the team had stored. NULL/NULL is every team today: in the archive,
nothing to erase.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("archive_opt_out_at", sa.DateTime(), nullable=True))
    op.add_column("org", sa.Column("archive_purged_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("archive_purged_at")
        batch.drop_column("archive_opt_out_at")
