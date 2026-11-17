"""PostgreSQL (Supabase) support: placeholder translation, connection strings, schema and data copy.

The translation tests always run. The others need a server: set TARSHISH_TEST_DATABASE_URL
(CI runs them against a Postgres service; the rest of the suite then runs on PostgreSQL too).
"""

from __future__ import annotations

import pytest

from core.database import (
    SCHEMA_VERSION,
    TABLES,
    Database,
    copy_database,
    is_postgres_url,
    postgres_conninfo,
    to_pyformat,
)
from storefront_api.seed import seed_storefront


# -- always ------------------------------------------------------------------------------- #
def test_placeholders_are_translated_outside_literals_and_comments():
    assert to_pyformat("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert to_pyformat("INSERT INTO t (a) VALUES (:a) ON CONFLICT (a) DO UPDATE SET a = excluded.a") == (
        "INSERT INTO t (a) VALUES (%(a)s) ON CONFLICT (a) DO UPDATE SET a = excluded.a"
    )
    # Literals keep their ?, : and quotes; % is always doubled; casts survive.
    assert to_pyformat("SELECT 'what?', 'a:b', 'it''s', x::text FROM t WHERE s LIKE ? ESCAPE '!'") == (
        "SELECT 'what?', 'a:b', 'it''s', x::text FROM t WHERE s LIKE %s ESCAPE '!'"
    )
    assert to_pyformat("SELECT '100%' AS p, 5 % 2 -- 50% of ?\nFROM t WHERE id = ?") == (
        "SELECT '100%%' AS p, 5 %% 2 -- 50%% of ?\nFROM t WHERE id = %s"
    )
    assert to_pyformat('SELECT "odd?name" FROM t WHERE created_at >= :week') == (
        'SELECT "odd?name" FROM t WHERE created_at >= %(week)s'
    )


def test_connection_strings_are_recognised_and_cleaned():
    assert is_postgres_url("postgresql://u:p@host:6543/postgres")
    assert is_postgres_url("postgres://u:p@host/postgres")
    assert not is_postgres_url("tarshish_engine.db") and not is_postgres_url(None)
    # Supabase's Vercel integration appends `supa=…`, which libpq rejects; sslmode stays.
    cleaned = postgres_conninfo("postgres://u:p@host:6543/postgres?sslmode=require&supa=base-pooler.x")
    assert cleaned == "postgres://u:p@host:6543/postgres?sslmode=require"
    assert Database("postgresql://u:secret@db.example.com:6543/postgres").describe() == (
        "PostgreSQL at db.example.com:6543"
    )


def test_settings_accept_a_database_url_and_reject_other_schemes(make_settings):
    from pydantic import ValidationError

    from core.config import Settings
    from core.resilience import redact

    settings = Settings(database_url="postgresql://u:Sup3rSecretPw@h:6543/postgres", database_path="x.db")
    assert Database.from_settings(settings).is_postgres
    assert "Sup3rSecretPw" not in redact("connecting to postgresql://u:Sup3rSecretPw@h:6543/postgres")
    with pytest.raises(ValidationError, match="PostgreSQL connection string"):
        Settings(database_url="mysql://u:p@h/db")
    assert not Database.from_settings(Settings(database_path="x.db")).is_postgres


# -- with a server -------------------------------------------------------------------------- #
@pytest.fixture
def pg(postgres_url):
    if postgres_url is None:
        pytest.skip("set TARSHISH_TEST_DATABASE_URL to run the PostgreSQL tests")
    db = Database(postgres_url)
    db.initialize()
    return db


def test_schema_has_every_table_with_row_level_security_on(pg):
    tables = pg.query(
        "SELECT c.relname AS name, c.relrowsecurity AS rls FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r'"
    )
    by_name = {row["name"]: row["rls"] for row in tables}
    assert set(TABLES) | {"tarshish_meta"} <= set(by_name)
    assert all(by_name.values()), "every table must have RLS on (Supabase's anon API must see nothing)"
    assert pg.query_one("SELECT value FROM tarshish_meta WHERE key = 'schema_version'") == {"value": str(SCHEMA_VERSION)}
    # Both databases expose the same product columns, so every query runs on either.
    product_columns = {row["name"] for row in pg.table_columns("products")}
    assert {"id", "category", "raw_url", "title_he", "image_urls_json", "is_flash_sale"} <= product_columns
    pg.initialize()  # idempotent: the fast path sees the current version and does nothing


def test_a_sqlite_database_is_copied_into_postgres_with_its_ids(pg, tmp_path):
    local = Database(tmp_path / "local.db")
    local.initialize()
    seeded = seed_storefront(local)
    local.record_click_event(3, category="sports", source="aliexpress_mock", language="he")

    copied = copy_database(local, pg)
    assert copied["products"] == seeded and copied["click_events"] == 1
    assert pg.query("SELECT id, title FROM products ORDER BY id") == local.query("SELECT id, title FROM products ORDER BY id")
    # The id sequence moved past the copied rows: the next insert gets a fresh id.
    assert pg.record_click_event(3, category="sports", source="aliexpress_mock", language="en") == 2
    # Copying again changes nothing (existing keys are skipped).
    copy_database(local, pg)
    assert pg.query_one("SELECT COUNT(*) AS n FROM products") == {"n": seeded}
