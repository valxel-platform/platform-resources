"""A product login in Postgres, made by the controller with no password anywhere (constitution VX-20).

`plan` is the SQL, as a pure function of the login's spec, so it is tested on its own; `apply` runs
it on an admin connection, inside one transaction, and checks the result.

What a login gets, and nothing else:
  - CONNECT on its database, and CREATE when it builds its own schemas (a transform);
  - USAGE on each schema it reads, and SELECT on exactly the columns it was given. Column grants it
    holds and was not given any more are revoked, so a narrowed contract narrows the login;
  - with break_glass, a NOLOGIN role <login>_break_glass it administers (it grants it to a person
    and takes it back);
  - with tokenize, EXECUTE on <login>_key.tokenize(text): HMAC-SHA256 with a key made inside the
    database, readable only by the NOLOGIN role <login>_keyholder that owns the function. The key
    never leaves the database: not to the controller, not to the pod.

The principal (the role the workload signs in as) is made by the cloud's own mechanism: a Cloud SQL
IAM user (made through the Cloud SQL API before this runs) or an Entra principal
(pgaadauth_create_principal_with_oid). `password` principals exist only for the end-to-end test.
A principal with any of superuser, createdb, createrole, replication or bypassrls is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")  # leaves room for _break_glass and _keyholder
UNSAFE = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")


class LoginError(Exception):
    """The spec can't be applied as asked; the message says why, for the login's status."""


@dataclass(frozen=True)
class Read:
    schema: str
    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Login:
    name: str  # the login's own name: <name>_break_glass, <name>_key, <name>_keyholder
    principal: str  # the role the workload signs in as
    kind: str  # gcp-iam | azure-entra | password
    database: str
    reads: tuple[Read, ...] = ()
    create_schemas: bool = False
    break_glass: bool = False
    tokenize: bool = False
    azure_object_id: str = ""
    password: str = field(default="", repr=False)  # kind password only (end-to-end test)

    @property
    def break_glass_role(self) -> str:
        return f"{self.name}_break_glass"

    @property
    def keyholder(self) -> str:
        return f"{self.name}_keyholder"

    @property
    def key_schema(self) -> str:
        return f"{self.name}_key"


def login_from_spec(spec: dict[str, Any], principal: str, kind: str, *, azure_object_id: str = "",
                    password: str = "") -> Login:
    name = str(spec.get("login", ""))
    if not NAME.match(name):
        raise LoginError(f"login {name!r} must match {NAME.pattern}")
    if kind not in ("gcp-iam", "azure-entra", "password"):
        raise LoginError(f"unknown principal kind {kind!r}")
    reads = []
    for r in spec.get("reads") or []:
        cols = tuple(str(c) for c in r.get("columns") or [])
        if not cols:
            raise LoginError(f"read {r.get('schema')}.{r.get('table')} lists no columns")
        reads.append(Read(str(r.get("schema") or "public"), str(r["table"]), cols))
    return Login(name=name, principal=principal, kind=kind, database=str(spec["database"]), reads=tuple(reads),
                 create_schemas=bool(spec.get("createSchemas")), break_glass=bool(spec.get("breakGlass")),
                 tokenize=bool(spec.get("tokenize")), azure_object_id=azure_object_id, password=password)


def _role(name: str, attrs: str) -> sql.Composed:
    """CREATE ROLE when it doesn't exist (Postgres has no IF NOT EXISTS for roles)."""
    return sql.SQL(
        "do $$ begin if not exists (select 1 from pg_roles where rolname = {}) then create role {} {}; end if; end $$"
    ).format(sql.Literal(name), sql.Identifier(name), sql.SQL(attrs))


def plan(login: Login) -> list[sql.Composable]:
    """The statements that make the login exactly as specified. Idempotent: run on every reconcile."""
    p = sql.Identifier(login.principal)
    out: list[sql.Composable] = []
    if login.kind == "azure-entra":
        out.append(sql.SQL(
            "do $$ begin if not exists (select 1 from pg_roles where rolname = {}) then"
            " perform pgaadauth_create_principal_with_oid({}, {}, 'service', false, false); end if; end $$"
        ).format(sql.Literal(login.principal), sql.Literal(login.principal), sql.Literal(login.azure_object_id)))
    elif login.kind == "password":
        out.append(_role(login.principal, "login"))
        out.append(sql.SQL("alter role {} with login password {}").format(p, sql.Literal(login.password)))
    db = sql.Identifier(login.database)
    privileges = "connect, create" if login.create_schemas else "connect"
    out.append(sql.SQL("grant {} on database {} to {}").format(sql.SQL(privileges), db, p))
    if not login.create_schemas:
        out.append(sql.SQL("revoke create on database {} from {}").format(db, p))
    for schema in sorted({r.schema for r in login.reads}):
        out.append(sql.SQL("grant usage on schema {} to {}").format(sql.Identifier(schema), p))
    for r in login.reads:
        out.append(sql.SQL("grant select ({}) on {}.{} to {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in r.columns), sql.Identifier(r.schema),
            sql.Identifier(r.table), p))
    if login.break_glass:
        out.append(_role(login.break_glass_role, "nologin"))
        out.append(sql.SQL("grant {} to {} with admin option").format(sql.Identifier(login.break_glass_role), p))
    if login.tokenize:
        out += _tokenize(login)
    return out


def _tokenize(login: Login) -> list[sql.Composable]:
    kh, ks, p = sql.Identifier(login.keyholder), sql.Identifier(login.key_schema), sql.Identifier(login.principal)
    return [
        _role(login.keyholder, "nologin"),
        # The admin creates the key's objects as their owner, then never reads the key itself.
        sql.SQL("grant {} to current_user").format(kh),
        sql.SQL("create schema if not exists {} authorization {}").format(ks, kh),
        sql.SQL("revoke all on schema {} from public").format(ks),
        sql.SQL("create table if not exists {}.key (k text not null)").format(ks),
        sql.SQL("alter table {}.key owner to {}").format(ks, kh),
        sql.SQL("revoke all on {}.key from public").format(ks),
        # Made once, inside the database: 244 random bits from two version 4 UUIDs.
        sql.SQL(
            "insert into {}.key (k) select encode(sha256(convert_to(gen_random_uuid()::text || gen_random_uuid()::text,"
            " 'UTF8')), 'hex') where not exists (select 1 from {}.key)"
        ).format(ks, ks),
        sql.SQL(
            """create or replace function {}.tokenize(v text) returns text language plpgsql stable security definer
        set search_path = pg_catalog as $f$
        declare k bytea; ipad bytea; opad bytea;
        begin
          if v is null then return null; end if;
          select convert_to(t.k, 'UTF8') into strict k from {}.key t;
          if length(k) > 64 then k := sha256(k); end if;
          k := k || decode(repeat('00', 64 - length(k)), 'hex');
          ipad := k; opad := k;
          for i in 0..63 loop
            ipad := set_byte(ipad, i, get_byte(k, i) # 54);
            opad := set_byte(opad, i, get_byte(k, i) # 92);
          end loop;
          return encode(sha256(opad || sha256(ipad || convert_to(v, 'UTF8'))), 'hex');
        end $f$"""
        ).format(ks, ks),
        sql.SQL("alter function {}.tokenize(text) owner to {}").format(ks, kh),
        sql.SQL("revoke all on function {}.tokenize(text) from public").format(ks),
        sql.SQL("grant usage on schema {} to {}").format(ks, p),
        sql.SQL("grant execute on function {}.tokenize(text) to {}").format(ks, p),
        sql.SQL("revoke {} from current_user").format(kh),
    ]


def stale_columns(login: Login, held: list[tuple[str, str, str]]) -> list[sql.Composable]:
    """REVOKEs for the column SELECTs the principal holds outside its own schemas but wasn't given."""
    want = {(r.schema, r.table, c) for r in login.reads for c in r.columns}
    out: list[sql.Composable] = []
    for schema, table, column in sorted(set(held) - want):
        out.append(sql.SQL("revoke select ({}) on {}.{} from {}").format(
            sql.Identifier(column), sql.Identifier(schema), sql.Identifier(table), sql.Identifier(login.principal)))
    return out


HELD = """select c.table_schema, c.table_name, c.column_name from information_schema.column_privileges c
  join pg_namespace n on n.nspname = c.table_schema
  where c.grantee = %s and c.privilege_type = 'SELECT' and pg_get_userbyid(n.nspowner) <> %s"""
TABLE_GRANTS = """select g.table_schema, g.table_name, g.privilege_type from information_schema.role_table_grants g
  join pg_namespace n on n.nspname = g.table_schema
  where g.grantee = %s and pg_get_userbyid(n.nspowner) <> %s"""


def apply(conn: Any, login: Login) -> dict[str, Any]:
    """Make the login on an admin connection, in one transaction; returns what the status reports."""
    with conn.transaction(), conn.cursor() as cur:
        if login.kind == "gcp-iam" and not _exists(cur, login.principal):
            raise LoginError(f"Cloud SQL IAM user {login.principal} doesn't exist yet")
        for stmt in plan(login):
            cur.execute(stmt)
        cur.execute(HELD, [login.principal, login.principal])
        for stmt in stale_columns(login, [tuple(r) for r in cur.fetchall()]):
            cur.execute(stmt)
        cur.execute(TABLE_GRANTS, [login.principal, login.principal])
        table_level = cur.fetchall()
        if table_level:
            for schema, table, privilege in table_level:
                cur.execute(sql.SQL("revoke {} on {}.{} from {}").format(
                    sql.SQL(privilege), sql.Identifier(schema), sql.Identifier(table), sql.Identifier(login.principal)))
        cur.execute(sql.SQL("select {} from pg_roles where rolname = %s").format(
            sql.SQL(", ").join(sql.Identifier(a) for a in UNSAFE)), [login.principal])
        row = cur.fetchone()
        if row is None:
            raise LoginError(f"role {login.principal} doesn't exist")
        bad = [a.removeprefix("rol") for a, v in zip(UNSAFE, row, strict=True) if v]
        if bad:
            raise LoginError(f"role {login.principal} has {', '.join(bad)}: a product login may have none of them")
    return {
        "username": login.principal,
        "database": login.database,
        "breakGlassRole": login.break_glass_role if login.break_glass else "",
        "tokenizeFunction": f"{login.key_schema}.tokenize" if login.tokenize else "",
    }


def _exists(cur: Any, role: str) -> bool:
    cur.execute("select 1 from pg_roles where rolname = %s", [role])
    return cur.fetchone() is not None


def remove(conn: Any, login: Login) -> str:
    """Take the login's grants away. Its own schemas hold product data, which only a person deletes
    (VX-19): while the principal owns objects it stays, without its grants, and the reason says so."""
    with conn.transaction(), conn.cursor() as cur:
        if not _exists(cur, login.principal):
            return ""
        p = sql.Identifier(login.principal)
        cur.execute(sql.SQL("revoke all on database {} from {}").format(sql.Identifier(login.database), p))
        for schema in sorted({r.schema for r in login.reads}):
            cur.execute(sql.SQL("revoke all on all tables in schema {} from {}").format(sql.Identifier(schema), p))
            cur.execute(sql.SQL("revoke usage on schema {} from {}").format(sql.Identifier(schema), p))
        if login.tokenize:
            cur.execute(sql.SQL("revoke all on schema {} from {}").format(sql.Identifier(login.key_schema), p))
        if login.break_glass:
            cur.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(login.break_glass_role)))
        cur.execute("select n.nspname from pg_namespace n join pg_roles r on r.oid = n.nspowner where r.rolname = %s",
                    [login.principal])
        owned = sorted(r[0] for r in cur.fetchall())
        if owned:
            # The tokenize key stays too: the marts' tokens were made with it.
            return (f"{login.principal} still owns {', '.join(owned)}: product data, which only a person drops "
                    "or reassigns; its grants are gone")
        if login.tokenize and _exists(cur, login.keyholder):
            cur.execute(sql.SQL("grant {} to current_user").format(sql.Identifier(login.keyholder)))
            cur.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(login.key_schema)))
            cur.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(login.keyholder)))
        if login.kind in ("password", "azure-entra"):
            cur.execute(sql.SQL("drop role {}").format(p))
    return ""
