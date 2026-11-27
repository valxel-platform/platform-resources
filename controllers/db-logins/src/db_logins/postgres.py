"""A product login in Postgres, made by the controller with no password anywhere (constitution VX-20).

`plan` is the SQL, as a pure function of the login's spec, so it is tested on its own; `apply` runs
it on an admin connection, inside one transaction, and checks the result.

Every login has an access (spec.access):
  - owner: it acts as the database's owner role, <database>_owner (NOLOGIN): the workload signs in
    as itself and SETs ROLE <database>_owner, so the tables belong to the role, not to whoever
    created them. The admin is a member of that role, so it can grant on those tables (a transform's
    columns). Every table the owner makes is readable to <database>_reader. With grantRoles, NOLOGIN
    roles the owner may grant to others (reporting's reporting_reader, for report logins);
  - reader: it is a member of <database>_reader: SELECT on the owner's tables, nothing else;
  - columns (a transform): exactly what spec.reads lists, as below.

What a columns login gets, and nothing else:
  - CONNECT on its database, and CREATE when it builds its own schemas (a transform);
  - USAGE on each schema it reads, and SELECT on exactly the columns it was given. Column grants it
    holds and was not given any more are revoked, so a narrowed contract narrows the login;
  - with break_glass, a NOLOGIN role <login>_break_glass it administers (it grants it to a person
    and takes it back);
  - with tokenize, EXECUTE on <login>_key.tokenize(text) and <login>_key.key_version(): HMAC-SHA256
    with a key made inside the database, readable only by the NOLOGIN role <login>_keyholder that
    owns the functions. The key never leaves the database: not to the controller, not to the pod.
    Keys are versioned and every token says which made it ("v2:3d81..."). The active key is
    replaced when it is older than its cryptoperiod (rotate_every_days, a year by default) or when a
    new rotation is requested; the replaced key is destroyed retire_after_days later (90 by default),
    or at once when it is compromised. Nothing translates old tokens to new ones.

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
    access: str = "columns"  # owner | reader | columns
    grant_roles: tuple[str, ...] = ()  # owner only: NOLOGIN roles it may grant (reporting_reader)
    reads: tuple[Read, ...] = ()
    create_schemas: bool = False
    break_glass: bool = False
    tokenize: bool = False
    rotate_every_days: int = 365
    retire_after_days: int = 90
    rotate_request: str = ""  # a new value replaces the active key now
    compromised: bool = False  # with a rotation request: destroy the replaced key at once
    azure_object_id: str = ""
    password: str = field(default="", repr=False)  # kind password only (end-to-end test)

    @property
    def owner_role(self) -> str:
        return f"{self.database}_owner"

    @property
    def reader_role(self) -> str:
        return f"{self.database}_reader"

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
    access = str(spec.get("access") or "columns")
    if access not in ("owner", "reader", "columns"):
        raise LoginError(f"access {access!r} must be owner, reader or columns")
    grant_roles = tuple(str(r) for r in spec.get("grantRoles") or [])
    for r in grant_roles:
        if not NAME.match(r):
            raise LoginError(f"grantRoles: {r!r} must match {NAME.pattern}")
    if grant_roles and access != "owner":
        raise LoginError("grantRoles is for an owner login only")
    if access != "columns" and reads:
        raise LoginError("reads is for a columns login (a transform); an owner or reader gets the whole store")
    if len(str(spec["database"])) > 56:
        raise LoginError("the database's name must be at most 56 characters (for <database>_owner)")
    key = spec.get("tokenizeKey") or {}
    rotate, retire = int(key.get("rotateEveryDays", 365)), int(key.get("retireAfterDays", 90))
    if not (1 <= rotate <= 3650 and 0 <= retire <= 3650):
        raise LoginError("tokenizeKey: rotateEveryDays must be 1 to 3650 and retireAfterDays 0 to 3650")
    return Login(name=name, principal=principal, kind=kind, database=str(spec["database"]), access=access,
                 grant_roles=grant_roles, reads=tuple(reads),
                 create_schemas=bool(spec.get("createSchemas")), break_glass=bool(spec.get("breakGlass")),
                 tokenize=bool(spec.get("tokenize")), rotate_every_days=rotate, retire_after_days=retire,
                 rotate_request=str(key.get("rotateRequest") or ""), compromised=bool(key.get("compromised")),
                 azure_object_id=azure_object_id, password=password)


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
    if login.access == "owner":
        out += _owner(login)
    elif login.access == "reader":
        out += _reader(login)
    if login.reads:  # the admin acts for the store's owner role, if there is one, to grant on its tables
        out.append(sql.SQL(
            "do $$ begin if exists (select 1 from pg_roles where rolname = {}) then execute {}; end if; end $$"
        ).format(sql.Literal(login.owner_role),
                 sql.Literal(sql.SQL("grant {} to current_user").format(sql.Identifier(login.owner_role)).as_string(None))))
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


def _store_roles(login: Login) -> list[sql.Composable]:
    owner, reader = sql.Identifier(login.owner_role), sql.Identifier(login.reader_role)
    return [
        _role(login.owner_role, "nologin"),
        _role(login.reader_role, "nologin"),
        # The admin acts for the owner (grants on its tables), never signs in as it.
        sql.SQL("grant {} to current_user").format(owner),
        sql.SQL("grant usage, create on schema public to {}").format(owner),
        sql.SQL("grant usage on schema public to {}").format(reader),
        sql.SQL("grant select on all tables in schema public to {}").format(reader),
        sql.SQL("alter default privileges for role {} in schema public grant select on tables to {}").format(
            owner, reader),
    ]


def _owner(login: Login) -> list[sql.Composable]:
    p, owner = sql.Identifier(login.principal), sql.Identifier(login.owner_role)
    out = _store_roles(login) + [sql.SQL("grant {} to {}").format(owner, p)]
    for r in login.grant_roles:
        out += [_role(r, "nologin"), sql.SQL("grant {} to {} with admin option").format(sql.Identifier(r), owner)]
    return out


def _reader(login: Login) -> list[sql.Composable]:
    return _store_roles(login) + [sql.SQL("grant {} to {}").format(
        sql.Identifier(login.reader_role), sql.Identifier(login.principal))]


NEW_KEY = "encode(sha256(convert_to(gen_random_uuid()::text || gen_random_uuid()::text, 'UTF8')), 'hex')"
ACTIVE = "retiring_since is null and destroyed_at is null"


def _tokenize(login: Login) -> list[sql.Composable]:
    kh, ks, p = sql.Identifier(login.keyholder), sql.Identifier(login.key_schema), sql.Identifier(login.principal)
    return [
        _role(login.keyholder, "nologin"),
        # The admin creates the key's objects as their owner, then never reads a key itself.
        sql.SQL("grant {} to current_user").format(kh),
        sql.SQL("create schema if not exists {} authorization {}").format(ks, kh),
        sql.SQL("revoke all on schema {} from public").format(ks),
        sql.SQL(
            "create table if not exists {}.keys (version int primary key, k text, request text not null,"
            " created_at timestamptz not null default now(), retiring_since timestamptz, destroyed_at timestamptz)"
        ).format(ks),
        sql.SQL("alter table {}.keys owner to {}").format(ks, kh),
        sql.SQL("revoke all on {}.keys from public").format(ks),
        _rotate(login),
        sql.SQL(
            """create or replace function {ks}.tokenize(v text) returns text language plpgsql stable security definer
        set search_path = pg_catalog as $f$
        declare ver int; k bytea; ipad bytea; opad bytea;
        begin
          if v is null then return null; end if;
          select t.version, convert_to(t.k, 'UTF8') into strict ver, k from {ks}.keys t
            where t.retiring_since is null and t.destroyed_at is null order by t.version desc limit 1;
          if length(k) > 64 then k := sha256(k); end if;
          k := k || decode(repeat('00', 64 - length(k)), 'hex');
          ipad := k; opad := k;
          for i in 0..63 loop
            ipad := set_byte(ipad, i, get_byte(k, i) # 54);
            opad := set_byte(opad, i, get_byte(k, i) # 92);
          end loop;
          return 'v' || ver || ':' || encode(sha256(opad || sha256(ipad || convert_to(v, 'UTF8'))), 'hex');
        end $f$"""
        ).format(ks=ks),
        sql.SQL(
            """create or replace function {ks}.key_version() returns int language sql stable security definer
        set search_path = pg_catalog as $f$ select max(version) from {ks}.keys where {active} $f$"""
        ).format(ks=ks, active=sql.SQL(ACTIVE)),
        sql.SQL("alter function {}.tokenize(text) owner to {}").format(ks, kh),
        sql.SQL("alter function {}.key_version() owner to {}").format(ks, kh),
        sql.SQL("revoke all on function {}.tokenize(text) from public").format(ks),
        sql.SQL("revoke all on function {}.key_version() from public").format(ks),
        sql.SQL("grant usage on schema {} to {}").format(ks, p),
        sql.SQL("grant execute on function {}.tokenize(text) to {}").format(ks, p),
        sql.SQL("grant execute on function {}.key_version() to {}").format(ks, p),
        # Last: the admin keeps no way to the keys (apply reads the version just before).
        sql.SQL("revoke {} from current_user").format(kh),
    ]


def _rotate(login: Login) -> sql.Composable:
    """The first key; a new one when the active key is past its cryptoperiod or a new rotation is
    requested; and destroying replaced keys after the retire window (at once when compromised)."""
    return sql.SQL(
        """do $r$
        declare cur record; req text := {req};
        begin
          if not exists (select 1 from {ks}.keys where {active}) then
            insert into {ks}.keys (version, k, request)
              select coalesce(max(version), 0) + 1, {new}, 'first' from {ks}.keys;
            return;
          end if;
          select * into cur from {ks}.keys where {active} order by version desc limit 1;
          if cur.created_at < now() - make_interval(days => {rotate})
             or (req <> '' and not exists (select 1 from {ks}.keys where request = req)) then
            update {ks}.keys set retiring_since = now() where version = cur.version;
            insert into {ks}.keys (version, k, request)
              values (cur.version + 1, {new}, coalesce(nullif(req, ''), 'cryptoperiod'));
            if {compromised} and req <> '' then
              update {ks}.keys set k = null, destroyed_at = now() where version = cur.version;
            end if;
          end if;
          update {ks}.keys set k = null, destroyed_at = now()
            where destroyed_at is null and retiring_since < now() - make_interval(days => {retire});
        end $r$"""
    ).format(ks=sql.Identifier(login.key_schema), active=sql.SQL(ACTIVE), new=sql.SQL(NEW_KEY),
             req=sql.Literal(login.rotate_request), rotate=sql.Literal(login.rotate_every_days),
             retire=sql.Literal(login.retire_after_days), compromised=sql.SQL("true" if login.compromised else "false"))


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
        steps = plan(login)
        for stmt in steps[:-1] if login.tokenize else steps:
            cur.execute(stmt)
        version = None
        if login.tokenize:
            cur.execute(sql.SQL("select max(version) from {}.keys where " + ACTIVE).format(
                sql.Identifier(login.key_schema)))
            version = cur.fetchone()[0]
            cur.execute(steps[-1])
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
        "role": login.owner_role if login.access == "owner" else "",
        "database": login.database,
        "breakGlassRole": login.break_glass_role if login.break_glass else "",
        "tokenizeFunction": f"{login.key_schema}.tokenize" if login.tokenize else "",
        "tokenizeKeyVersion": version or 0,
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
        for role in (login.owner_role, login.reader_role):
            if login.access in ("owner", "reader") and _exists(cur, role):
                cur.execute(sql.SQL("revoke {} from {}").format(sql.Identifier(role), p))
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
