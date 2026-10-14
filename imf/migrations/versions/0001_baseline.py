"""Baseline: schema of the original IMF prototype.

Databases created by the prototype (via `create_all`) are stamped with this revision at start-up.

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("full_name", sa.String(128), nullable=False),
        sa.Column("pw_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pdgs_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("orbit", sa.Integer(), nullable=True),
        sa.Column("sensing_start", sa.DateTime(), nullable=False),
        sa.Column("sensing_stop", sa.DateTime(), nullable=False),
        sa.Column("publication_date", sa.DateTime(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("processing_ms", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("pdgs_id", name="uq_products_pdgs_id"),
    )
    op.create_index("ix_products_sensing_start", "products", ["sensing_start"])
    op.create_table(
        "parameters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("group", sa.String(64), nullable=False),
        sa.Column("algorithm", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("code", name="uq_parameters_code"),
    )
    op.create_table(
        "samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.UniqueConstraint("parameter_id", "product_id", name="uq_samples_parameter_id"),
    )
    op.create_index("ix_samples_param_time", "samples", ["parameter_id", "time"])
    op.create_table(
        "limits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("warn_lo", sa.Float(), nullable=True),
        sa.Column("warn_hi", sa.Float(), nullable=True),
        sa.Column("alarm_lo", sa.Float(), nullable=True),
        sa.Column("alarm_hi", sa.Float(), nullable=True),
        sa.Column("persistence", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
    )
    op.create_table(
        "parameter_state",
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("candidate_level", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_time", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("limit_text", sa.String(128), nullable=False),
        sa.Column("last_time", sa.DateTime(), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("worst_value", sa.Float(), nullable=True),
        sa.Column("n_violations", sa.Integer(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("raised_at", sa.DateTime(), nullable=False),
        sa.Column("ack_by", sa.String(64), nullable=True),
        sa.Column("ack_at", sa.DateTime(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
    )
    op.create_index("ix_events_parameter_id", "events", ["parameter_id"])
    op.create_index("ix_events_time", "events", ["time"])
    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_annotations_parameter_id", "annotations", ["parameter_id"])
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("html_path", sa.String(512), nullable=False),
        sa.Column("pdf_path", sa.String(512), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("n_products", sa.Integer(), nullable=False),
        sa.Column("n_events", sa.Integer(), nullable=False),
        sa.Column("overall_status", sa.String(16), nullable=False),
    )
    op.create_table(
        "poll_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("found", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(128), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
    )
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    for t in ("system_state", "audit_log", "poll_runs", "reports", "annotations", "events", "parameter_state",
              "limits", "samples", "parameters", "products", "users"):
        op.drop_table(t)
