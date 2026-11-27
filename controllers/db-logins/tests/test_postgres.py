"""The login's SQL on its own, and on a real Postgres when DB_LOGINS_TEST_URL names one (a superuser
connection to a database these tests may change; CI runs them against its Postgres service)."""

import os

import pytest

from db_logins import cloud, postgres

URL = os.environ.get("DB_LOGINS_TEST_URL", "")
SPEC = {"login": "emr_transform", "database": "emr", "createSchemas": True, "breakGlass": True, "tokenize": True,
        "reads": [{"schema": "public", "table": "notes",
                   "columns": ["note_id", "patient_id", "signed_at", "clinic_id", "_updated_at"]}]}


def text(stmts, conn=None):
    return "\n".join(s.as_string(conn) for s in stmts)


def login(**kw):
    return postgres.login_from_spec({**SPEC, **kw.pop("spec", {})}, kw.pop("principal", "emr-transform@p.iam"),
                                    kw.pop("kind", "gcp-iam"), **kw)


def test_a_login_gets_its_columns_its_schemas_and_nothing_else():
    import psycopg

    with psycopg.connect(URL) if URL else _nullconn() as conn:
        sql = text(postgres.plan(login(spec={"tokenize": False})), conn if URL else None)
    assert 'grant connect, create on database "emr" to "emr-transform@p.iam"' in sql
    assert ('grant select ("note_id", "patient_id", "signed_at", "clinic_id", "_updated_at") on "public"."notes"'
            ' to "emr-transform@p.iam"') in sql
    assert 'grant "emr_transform_break_glass" to "emr-transform@p.iam" with admin option' in sql
    import re

    for word in ("superuser", "createrole", "createdb", "all", "insert", "update", "delete", "password"):
        assert not re.search(rf"\b{word}\b", sql.lower()), word


def test_without_create_schemas_create_is_taken_back():
    sql = text(postgres.plan(login(spec={"createSchemas": False, "breakGlass": False, "tokenize": False})))
    assert 'grant connect on database "emr"' in sql and 'revoke create on database "emr"' in sql
    assert "break_glass" not in sql and "tokenize" not in sql


def test_the_tokenize_key_is_made_in_the_database_and_only_its_keyholder_reads_it():
    sql = text(postgres.plan(login()))
    assert 'create schema if not exists "emr_transform_key" authorization "emr_transform_keyholder"' in sql
    assert "security definer" in sql and 'revoke all on "emr_transform_key".key from public' in sql
    assert 'grant execute on function "emr_transform_key".tokenize(text) to "emr-transform@p.iam"' in sql
    assert "select on" not in sql.split("emr_transform_key", 1)[1].lower()  # nobody is granted the key table


def test_azure_principals_are_made_by_their_object_id():
    sql = text(postgres.plan(login(kind="azure-entra", principal="vx-abc", azure_object_id="0000-oid")))
    assert "pgaadauth_create_principal_with_oid('vx-abc', '0000-oid', 'service', false, false)" in sql


def test_a_narrowed_contract_revokes_columns_it_no_longer_gives():
    held = [("public", "notes", "note_id"), ("public", "notes", "body"), ("public", "billing", "amount")]
    sql = text(postgres.stale_columns(login(), held))
    assert 'revoke select ("body") on "public"."notes"' in sql and '"public"."billing"' in sql
    assert '"note_id"' not in sql


@pytest.mark.parametrize("spec,says", [({"login": "Bad-Name"}, "must match"),
                                       ({"reads": [{"table": "t", "columns": []}]}, "lists no columns")])
def test_bad_specs_are_refused(spec, says):
    with pytest.raises(postgres.LoginError, match=says):
        login(spec=spec)


def test_password_sign_in_is_refused_outside_the_test(monkeypatch):
    monkeypatch.delenv("DB_LOGINS_ALLOW_PASSWORD", raising=False)
    with pytest.raises(cloud.NotReady, match="end-to-end test only"):
        cloud.server_from_configmap({"auth": "password", "host": "h", "database": "d", "adminUser": "a"})


def test_identities_become_database_roles():
    gcp = cloud.Server("h", 5432, "emr", "gcp-iam", "valxel-db-logins@p.iam", instance="p:i")
    p = cloud.principal_from_identity(gcp, {"status": {"roleArn": "vx-1a2b@p.iam.gserviceaccount.com"}})
    assert (p.name, p.kind) == ("vx-1a2b@p.iam", "gcp-iam")
    az = cloud.Server("h", 5432, "emr", "azure-entra", "valxel-db-logins")
    p = cloud.principal_from_identity(az, {"status": {"roleName": "vx-1a2b", "principalId": "oid-1"}})
    assert (p.name, p.azure_object_id) == ("vx-1a2b", "oid-1")
    with pytest.raises(cloud.NotReady):
        cloud.principal_from_identity(az, {"status": {"roleName": "vx-1a2b"}})


class _nullconn:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# On a real Postgres: what the login can and can't do.
pg = pytest.mark.skipif(not URL, reason="DB_LOGINS_TEST_URL not set")


@pytest.fixture
def db():
    import psycopg

    with psycopg.connect(URL, autocommit=True) as conn:
        for stmt in ("drop schema if exists dbl_t_key cascade", "drop schema if exists mine cascade",
                     "drop table if exists public.notes", "drop table if exists public.billing"):
            conn.execute(stmt)
        for role in ("dbl_t", "dbl_t_break_glass", "dbl_t_keyholder", "reviewer"):
            conn.execute(f"do $$ begin if exists (select 1 from pg_roles where rolname = '{role}') then "
                         f"execute 'drop owned by {role}'; execute 'drop role {role}'; end if; end $$")
        conn.execute("create table public.notes (note_id text, patient_id text, signed_at timestamptz, clinic_id text,"
                     " body text, _updated_at timestamptz default now())")
        conn.execute("insert into public.notes values ('n1', 'p1', now(), 'c1', 'free text', now())")
        conn.execute("create table public.billing (id text, amount numeric)")
        conn.execute("create role reviewer login password 'reviewer-pw'")
        yield conn


def as_login(sql, password="login-pw-123"):
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(make_conninfo(URL, user="dbl_t", password=password), autocommit=True) as c:
        return c.execute(sql).fetchall() if sql.lstrip().lower().startswith("select") else c.execute(sql)


@pg
def test_on_postgres_the_login_can_do_exactly_what_it_was_given(db, monkeypatch):
    the = login(kind="password", principal="dbl_t", password="login-pw-123",
                spec={"database": db.info.dbname, "login": "dbl_t"})
    made = postgres.apply(db, the)
    assert made["tokenizeFunction"] == "dbl_t_key.tokenize"
    assert as_login("select note_id, patient_id, signed_at, clinic_id, _updated_at from public.notes")
    for refused in ("select body from public.notes", "select * from public.billing",
                    "insert into public.notes (note_id) values ('x')", "select k from dbl_t_key.key"):
        with pytest.raises(Exception, match="permission denied"):
            as_login(refused)
    as_login("create schema mine")
    as_login("grant dbl_t_break_glass to reviewer")
    [(token,)] = as_login("select dbl_t_key.tokenize('patient-1')")
    import hashlib
    import hmac

    [(key,)] = db.execute("select k from dbl_t_key.key").fetchall()
    assert token == hmac.new(key.encode(), b"patient-1", hashlib.sha256).hexdigest()
    # Again: idempotent, and the key is kept.
    postgres.apply(db, the)
    assert db.execute("select k from dbl_t_key.key").fetchall() == [(key,)]
    # A narrowed contract takes the column away.
    narrowed = login(kind="password", principal="dbl_t", password="login-pw-123",
                     spec={"database": db.info.dbname, "login": "dbl_t", "reads": [
                         {"schema": "public", "table": "notes", "columns": ["note_id", "_updated_at"]}]})
    postgres.apply(db, narrowed)
    with pytest.raises(Exception, match="permission denied"):
        as_login("select patient_id from public.notes")
    # Removing it keeps what it owns (product data), and says so.
    kept = postgres.remove(db, narrowed)
    assert "still owns mine" in kept
    assert db.execute("select count(*) from pg_roles where rolname = 'dbl_t'").fetchone() == (1,)
    with pytest.raises(Exception, match="permission denied"):
        as_login("select note_id from public.notes")


@pg
def test_on_postgres_a_principal_with_unsafe_attributes_is_refused(db):
    db.execute("create role dbl_t login createrole password 'x'")
    with pytest.raises(postgres.LoginError, match="createrole"):
        postgres.apply(db, login(kind="password", principal="dbl_t", password="x",
                                 spec={"database": db.info.dbname, "login": "dbl_t"}))
