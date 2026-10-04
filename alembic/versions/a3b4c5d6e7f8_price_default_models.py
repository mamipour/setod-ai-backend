"""price default models; remap agents on unpriced models

Revision ID: a3b4c5d6e7f8
Revises: z2a3b4c5d6e7
Create Date: 2026-10-03

DEFAULT_MODELS points at gpt-5.4-mini / claude-haiku-4-5 but neither had a row in
model_prices, so managed runs on the default were unpriced (free).  Prices below are
the published list prices as of 2026-10-03:

  gpt-5.4-mini      $0.75 in / $4.50 out per MTok  (developers.openai.com/api/docs/models/gpt-5.4-mini)
  claude-haiku-4-5  $1.00 in / $5.00 out per MTok  (platform.claude.com/docs/en/about-claude/pricing)

Agents whose saved model (draft or published) has no active price row are remapped so
they keep running: OpenAI-family slugs -> gpt-5.4-mini, claude-* slugs -> claude-haiku-4-5.
"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = 'a3b4c5d6e7f8'
down_revision = 'z2a3b4c5d6e7'
branch_labels = None
depends_on = None

PRICES = [
    # provider,    slug,               in,   out
    ("openai",    "gpt-5.4-mini",     0.75, 4.50),
    ("anthropic", "claude-haiku-4-5", 1.00, 5.00),
]


def upgrade() -> None:
    conn = op.get_bind()

    for provider, slug, inp, out in PRICES:
        conn.execute(
            sa.text(
                "INSERT INTO model_prices (id, provider, model_slug, input_per_m, output_per_m, managed_markup)"
                " VALUES (:id, :provider, :slug, :inp, :out, 1.5)"
                " ON CONFLICT ON CONSTRAINT uq_model_prices_slot DO NOTHING"
            ),
            {"id": str(uuid4()), "provider": provider, "slug": slug, "inp": inp, "out": out},
        )

    # Remap draft model column.
    conn.execute(sa.text("""
        UPDATE agents
        SET model = CASE WHEN model LIKE 'claude%' THEN 'claude-haiku-4-5' ELSE 'gpt-5.4-mini' END
        WHERE model <> ''
          AND model NOT IN (SELECT model_slug FROM model_prices WHERE active)
    """))

    # Remap published_config.model the same way so live runs match.
    conn.execute(sa.text("""
        UPDATE agents
        SET published_config = jsonb_set(
            published_config,
            '{model}',
            to_jsonb(CASE WHEN published_config->>'model' LIKE 'claude%'
                          THEN 'claude-haiku-4-5' ELSE 'gpt-5.4-mini' END)
        )
        WHERE published_config ? 'model'
          AND COALESCE(published_config->>'model', '') <> ''
          AND published_config->>'model' NOT IN (SELECT model_slug FROM model_prices WHERE active)
    """))


def downgrade() -> None:
    # Agent remaps are not reversed (original slugs were unpriced/retired anyway).
    conn = op.get_bind()
    for provider, slug, _, _ in PRICES:
        conn.execute(
            sa.text("DELETE FROM model_prices WHERE provider = :p AND model_slug = :s"),
            {"p": provider, "s": slug},
        )
