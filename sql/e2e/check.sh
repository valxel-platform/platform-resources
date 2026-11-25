#!/usr/bin/env bash
# postgres-login end to end (tracker CP-127): Crossplane, functions and provider-sql in a cluster,
# a real Postgres, and a transform's login made from an XR as OpenChoreo renders it. Passes when the
# login can do exactly what the design approves and nothing else:
#   - SELECT on the columns it was given, and not on another column of the same table
#   - no SELECT on a table it wasn't given, no writes to the raw table
#   - CREATE for schemas of its own, where it can make and fill tables
#   - none of superuser, createdb, createrole, replication or bypassrls
#   - it administers <login>_break_glass, so it can grant that role to a person and take it back
#   - its tokenize key is 48 characters and stays the same when the XR is reconciled again
set -euo pipefail
ctx=${1:?kubectl context}
ns=e2e-sql
k() { kubectl --context "$ctx" "$@"; }

k apply -f sql/e2e/postgres.yaml
k -n "$ns" rollout status deploy/postgres --timeout=5m
pod=$(k -n "$ns" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')
admin() { k -n "$ns" exec -i "$pod" -- psql -v ON_ERROR_STOP=1 -U app -d emr -Atq "$@"; }
admin <<'SQL'
create table if not exists public.notes (note_id text primary key, patient_id text, signed_at timestamptz,
  clinic_id text, body text, _updated_at timestamptz default now());
insert into public.notes values ('n1', 'p1', now(), 'c1', 'free text that stays out of reach', now())
  on conflict do nothing;
create table if not exists public.billing (id text primary key, amount numeric);
create role reviewer login password 'reviewer-e2e';
SQL

k apply -f apis/postgres-login/definition.yaml
k wait --for=condition=Established crd/postgreslogins.platform.valxel.dev --timeout=2m
k apply -f sql/postgres-login-composition.yaml
sed "s/namespace: dp-acme-emr-staging-x7k2/namespace: $ns/" sql/render/postgres-login/xr.yaml | k apply -f -
xr=emr-transform-login
for i in $(seq 1 90); do
  secret=$(k -n "$ns" get postgreslogin "$xr" -o jsonpath='{.status.secret}' 2>/dev/null || true)
  [ -n "$secret" ] && k -n "$ns" get postgreslogin "$xr" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -q True && break
  sleep 10
done
[ -n "${secret:-}" ] || { echo "the login never became ready"; k -n "$ns" get managed -o wide || true; exit 1; }
key_secret=$(k -n "$ns" get postgreslogin "$xr" -o jsonpath='{.status.tokenizeSecret}')
field() { k -n "$ns" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }
user=$(field "$secret" username); pass=$(field "$secret" password)
[ "$user" = emr_transform ] || { echo "username is $user"; exit 1; }
[ "$pass" != admin-only-e2e-password ] && [ ${#pass} -ge 16 ] || { echo "the login's password isn't its own"; exit 1; }
as_login() { k -n "$ns" exec -i "$pod" -- env PGPASSWORD="$pass" PGSSLMODE=require \
  psql -h localhost -U "$user" -d emr -Atq -v ON_ERROR_STOP=1 "$@"; }
refused() { if as_login -c "$1" >/dev/null 2>&1; then echo "allowed, but shouldn't be: $1"; exit 1; fi; echo "refused: $1"; }
allowed() { as_login -c "$1" >/dev/null || { echo "refused, but should be allowed: $1"; exit 1; }; echo "allowed: $1"; }

allowed "select note_id, patient_id, signed_at, clinic_id, _updated_at from public.notes"
refused "select body from public.notes"
refused "select * from public.notes"
refused "select id from public.billing"
refused "insert into public.notes (note_id) values ('x')"
refused "delete from public.notes"
allowed "create schema if not exists transform_build; create table transform_build.t as select note_id from public.notes"
allowed "grant emr_transform_break_glass to reviewer; revoke emr_transform_break_glass from reviewer"
attrs=$(admin -c "select rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin from pg_roles where rolname = 'emr_transform'")
[ "$attrs" = "f|f|f|f|f|t" ] || { echo "role attributes: $attrs"; exit 1; }
echo "attributes: login only"

key=$(field "$key_secret" tokenize_key)
[ ${#key} -eq 48 ] || { echo "tokenize key is ${#key} characters"; exit 1; }
k -n "$ns" annotate postgreslogin "$xr" e2e/reconcile="$(date +%s)" --overwrite >/dev/null
sleep 30
[ "$(field "$key_secret" tokenize_key)" = "$key" ] || { echo "the tokenize key changed on a reconcile"; exit 1; }
echo "tokenize key: 48 characters, kept"
echo "postgres-login: the login can do what the design approves, and nothing else"
