"""Feature extension: product levels/baselines/checksums, sample dimensions and entry attributes,
per-series parameter state, generalised events, vector samples, timeline, requirements, reference
data, proposed outputs, reprocessing campaigns, API tokens.

Existing prototype data (all L1) is assigned to the nominal baseline, passed in by `imf.migrate`
via `config.attributes["nominal_baseline"]`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14
"""
from alembic import context, op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _nominal() -> str:
    return context.config.attributes.get("nominal_baseline", "B03")


def _rebuild(name: str, columns: list, constraints: list, indexes: list[tuple[str, list[str]]],
             old_indexes: list[str], copy_sql: str) -> None:
    """Recreate a table with a new definition and copy its rows (portable: SQLite and PostgreSQL)."""
    for ix in old_indexes:
        op.drop_index(ix, table_name=name)
    op.rename_table(name, f"_old_{name}")
    op.create_table(name, *columns, *constraints)
    for ix, cols in indexes:
        op.create_index(ix, name, cols)
    op.execute(sa.text(copy_sql).bindparams(nominal=_nominal()))
    op.drop_table(f"_old_{name}")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                           f"COALESCE((SELECT MAX(id) FROM {name}), 1))"))


def upgrade() -> None:
    nominal = _nominal()

    # ---- products: level, baseline, versions, checksum, header, campaign --------------------
    with op.batch_alter_table("products") as b:
        b.add_column(sa.Column("level", sa.String(8), nullable=False, server_default="L1"))
        b.add_column(sa.Column("baseline", sa.String(16), nullable=False, server_default=""))
        b.add_column(sa.Column("processor_version", sa.String(16), nullable=True))
        b.add_column(sa.Column("product_version", sa.String(16), nullable=True))
        b.add_column(sa.Column("checksum", sa.String(64), nullable=True))
        b.add_column(sa.Column("header", sa.Text(), nullable=True))
        b.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
    op.create_index("ix_products_campaign_id", "products", ["campaign_id"])
    op.create_index("ix_products_type_orbit", "products", ["product_type", "orbit"])
    op.execute(sa.text("UPDATE products SET baseline = :nominal").bindparams(nominal=nominal))

    # ---- parameters: kind, dims, origin ------------------------------------------------------
    with op.batch_alter_table("parameters") as b:
        b.add_column(sa.Column("kind", sa.String(8), nullable=False, server_default="scalar"))
        b.add_column(sa.Column("dims", sa.String(128), nullable=False, server_default=""))
        b.add_column(sa.Column("origin", sa.String(16), nullable=False, server_default="config"))

    # ---- samples: nullable product, dims, baseline, entry attributes -------------------------
    _rebuild(
        "samples",
        [sa.Column("id", sa.Integer(), primary_key=True),
         sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
         sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
         sa.Column("time", sa.DateTime(), nullable=False),
         sa.Column("value", sa.Float(), nullable=False),
         sa.Column("level", sa.Integer(), nullable=False),
         sa.Column("dims", sa.String(64), nullable=False),
         sa.Column("baseline", sa.String(16), nullable=False),
         sa.Column("entry_type", sa.String(16), nullable=False),
         sa.Column("source", sa.String(128), nullable=False),
         sa.Column("entry_time", sa.DateTime(), nullable=False)],
        [sa.UniqueConstraint("parameter_id", "product_id", "dims", name="uq_samples_param_product_dims")],
        [("ix_samples_param_time", ["parameter_id", "time"]), ("ix_samples_product", ["product_id"])],
        ["ix_samples_param_time"],
        "INSERT INTO samples (id, parameter_id, product_id, time, value, level, dims, baseline, entry_type, "
        "source, entry_time) SELECT o.id, o.parameter_id, o.product_id, o.time, o.value, o.level, '', :nominal, "
        "'derived', '', COALESCE((SELECT p.ingested_at FROM products p WHERE p.id = o.product_id), o.time) "
        "FROM _old_samples o",
    )

    # ---- parameter_state: one row per series (parameter, baseline, dims) ---------------------
    _rebuild(
        "parameter_state",
        [sa.Column("id", sa.Integer(), primary_key=True),
         sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
         sa.Column("baseline", sa.String(16), nullable=False),
         sa.Column("series", sa.String(64), nullable=False),
         sa.Column("level", sa.Integer(), nullable=False),
         sa.Column("candidate_level", sa.Integer(), nullable=False),
         sa.Column("candidate_count", sa.Integer(), nullable=False),
         sa.Column("last_value", sa.Float(), nullable=True),
         sa.Column("last_time", sa.DateTime(), nullable=True)],
        [sa.UniqueConstraint("parameter_id", "baseline", "series", name="uq_parameter_state_series")],
        [], [],
        "INSERT INTO parameter_state (parameter_id, baseline, series, level, candidate_level, candidate_count, "
        "last_value, last_time) SELECT parameter_id, :nominal, '', level, candidate_level, candidate_count, "
        "last_value, last_time FROM _old_parameter_state",
    )

    # ---- events: optional parameter/value, baseline, dims, category, rule, action -------------
    _rebuild(
        "events",
        [sa.Column("id", sa.Integer(), primary_key=True),
         sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=True),
         sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
         sa.Column("severity", sa.String(16), nullable=False),
         sa.Column("time", sa.DateTime(), nullable=False),
         sa.Column("value", sa.Float(), nullable=True),
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
         sa.Column("baseline", sa.String(16), nullable=False),
         sa.Column("dims", sa.String(64), nullable=False),
         sa.Column("category", sa.String(16), nullable=False),
         sa.Column("rule", sa.String(32), nullable=True),
         sa.Column("title", sa.String(200), nullable=False),
         sa.Column("recommended_action", sa.Text(), nullable=True)],
        [],
        [("ix_events_parameter_id", ["parameter_id"]), ("ix_events_time", ["time"])],
        ["ix_events_parameter_id", "ix_events_time"],
        "INSERT INTO events (id, parameter_id, product_id, severity, time, value, limit_text, last_time, last_value, "
        "worst_value, n_violations, cleared_at, status, raised_at, ack_by, ack_at, comment, baseline, dims, "
        "category, rule, title, recommended_action) SELECT id, parameter_id, product_id, severity, time, value, "
        "limit_text, last_time, last_value, worst_value, n_violations, cleared_at, status, raised_at, ack_by, "
        "ack_at, comment, :nominal, '', 'limit', NULL, '', NULL FROM _old_events",
    )

    # ---- new tables -------------------------------------------------------------------------
    op.create_table(
        "product_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.UniqueConstraint("source_id", "target_id", name="uq_product_links_pair"),
    )
    op.create_index("ix_product_links_source_id", "product_links", ["source_id"])
    op.create_index("ix_product_links_target_id", "product_links", ["target_id"])

    op.create_table(
        "vector_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("dims", sa.String(64), nullable=False),
        sa.Column("baseline", sa.String(16), nullable=False),
        sa.Column("axis", sa.Text(), nullable=False),
        sa.Column("values", sa.Text(), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("entry_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("parameter_id", "product_id", "dims", name="uq_vector_samples_param_product_dims"),
    )
    op.create_index("ix_vector_samples_param_time", "vector_samples", ["parameter_id", "time"])
    op.create_index("ix_vector_samples_product", "vector_samples", ["product_id"])

    op.create_table(
        "requirements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_id", sa.Integer(), sa.ForeignKey("parameters.id"), nullable=False),
        sa.Column("lo", sa.Float(), nullable=True),
        sa.Column("hi", sa.Float(), nullable=True),
        sa.Column("text", sa.String(200), nullable=False),
        sa.Column("reference", sa.String(200), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=False),
    )
    op.create_index("ix_requirements_parameter_id", "requirements", ["parameter_id"])

    op.create_table(
        "proposed_outputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("rule", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
    )
    op.create_index("ix_proposed_outputs_event_id", "proposed_outputs", ["event_id"])

    op.create_table(
        "timeline_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("start", sa.DateTime(), nullable=False),
        sa.Column("stop", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("rule", sa.String(32), nullable=True),
        sa.Column("attributes", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("baseline", sa.String(16), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("entry_time", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_timeline_entries_type", "timeline_entries", ["type"])
    op.create_index("ix_timeline_entries_start", "timeline_entries", ["start"])

    op.create_table(
        "reference_data",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("dims", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("axis", sa.Text(), nullable=False),
        sa.Column("values", sa.Text(), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("entry_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", "dims", "version", name="uq_reference_data_version"),
    )
    op.create_index("ix_reference_data_name", "reference_data", ["name"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("baseline", sa.String(16), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("start", sa.DateTime(), nullable=True),
        sa.Column("end", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("n_found", sa.Integer(), nullable=False),
        sa.Column("n_processed", sa.Integer(), nullable=False),
        sa.Column("n_failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("name", name="uq_campaigns_name"),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("name", name="uq_api_tokens_name"),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )

    # The prototype used a single watermark for its only product type.
    op.execute(sa.text("UPDATE system_state SET key = 'poll.watermark.FTS_L1B' WHERE key = 'poll.watermark'"))


def downgrade() -> None:
    raise NotImplementedError("Downgrade below 0002 is not supported - restore a backup instead.")
