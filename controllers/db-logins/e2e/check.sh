#!/usr/bin/env bash
# The db-logins controller end to end (VX-20): its image in a k3d cluster, a real Postgres, and a
# DatabaseLogin. Passes when the login can do exactly what it was given and nothing else, its tokenize
# key stays in the database, a narrowed request narrows it, and deleting it keeps the data it owns.
set -euo pipefail
ctx=${1:?kubectl context}
image=${2:?controller image}
ns=e2e-logins
here=$(cd "$(dirname "$0")" && pwd)
k() { kubectl --context "$ctx" "$@"; }

k apply -f "$here/../deploy/crd.yaml"
k wait --for=condition=Established crd/databaselogins.platform.valxel.dev --timeout=1m
k apply -f "$here/../deploy/controller.yaml"
k -n valxel-db-logins set image deploy/db-logins controller="$image"
k -n valxel-db-logins patch deploy/db-logins --type=json -p '[
  {"op":"add","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Never"},
  {"op":"add","path":"/spec/template/spec/containers/0/env","value":[{"name":"DB_LOGINS_ALLOW_PASSWORD","value":"1"}]}]'
sed '/^kind: DatabaseLogin/,$d' "$here/fixtures.yaml" | sed '$d' | k apply -f -
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
k -n valxel-db-logins rollout status deploy/db-logins --timeout=3m
k apply -f "$here/fixtures.yaml"
login=emr-transform-login
ready() { k -n "$ns" get databaselogin "$login" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'; }
for i in $(seq 1 60); do [ "$(ready)" = True ] && break; sleep 5; done
[ "$(ready)" = True ] || { echo "the login never became ready"; k -n "$ns" get databaselogin "$login" -o yaml; exit 1; }
status() { k -n "$ns" get databaselogin "$login" -o jsonpath="{.status.$1}"; }
[ "$(status username)" = emr_transform ] && [ "$(status tokenizeFunction)" = emr_transform_key.tokenize ] \
  && [ "$(status breakGlassRole)" = emr_transform_break_glass ] || { k -n "$ns" get databaselogin "$login" -o yaml; exit 1; }

as_login() { k -n "$ns" exec -i "$pod" -- env PGPASSWORD=login-only-e2e-password \
  psql -h localhost -U emr_transform -d emr -Atq -v ON_ERROR_STOP=1 "$@"; }
refused() { if as_login -c "$1" >/dev/null 2>&1; then echo "allowed, but shouldn't be: $1"; exit 1; fi; echo "refused: $1"; }
allowed() { as_login -c "$1" >/dev/null || { echo "refused, but should be allowed: $1"; exit 1; }; echo "allowed: $1"; }

allowed "select note_id, patient_id, signed_at, clinic_id, _updated_at from public.notes"
refused "select body from public.notes"
refused "select * from public.notes"
refused "select id from public.billing"
refused "insert into public.notes (note_id) values ('x')"
refused "delete from public.notes"
refused "select k from emr_transform_key.key"
allowed "create schema if not exists transform; create table transform.t as select note_id from public.notes"
allowed "grant emr_transform_break_glass to reviewer; revoke emr_transform_break_glass from reviewer"
token=$(as_login -c "select emr_transform_key.tokenize('patient-1')")
key=$(admin -c "select k from emr_transform_key.key")
want=$(python3 -c "import hmac,hashlib,sys; print(hmac.new(sys.argv[1].encode(), b'patient-1', hashlib.sha256).hexdigest())" "$key")
[ "$token" = "$want" ] || { echo "tokenize isn't HMAC-SHA256 with the database's key"; exit 1; }
echo "tokenize: HMAC-SHA256, its key only in the database"
attrs=$(admin -c "select rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls from pg_roles where rolname = 'emr_transform'")
[ "$attrs" = "f|f|f|f|f" ] || { echo "role attributes: $attrs"; exit 1; }

# A grant made by hand is taken back at the next reconcile (a spec change triggers one now).
admin -c "grant select (body) on public.notes to emr_transform"
k -n "$ns" patch databaselogin "$login" --type=merge -p \
  '{"spec":{"reads":[{"schema":"public","table":"notes","columns":["note_id","_updated_at"]}]}}'
for i in $(seq 1 30); do if as_login -c "select patient_id from public.notes" >/dev/null 2>&1; then sleep 3; else break; fi; done
refused "select patient_id from public.notes"
refused "select body from public.notes"
allowed "select note_id from public.notes"
[ "$(admin -c "select k from emr_transform_key.key")" = "$key" ] || { echo "the key changed"; exit 1; }

# Deleting the login takes its grants away and keeps what it owns: product data, a person's to drop.
k -n "$ns" delete databaselogin "$login" --timeout=2m
refused "select note_id from public.notes"
[ "$(admin -c "select count(*) from pg_namespace where nspname = 'transform'")" = 1 ] || { echo "its schema went"; exit 1; }
echo "db-logins: the login can do what it was given and nothing else, with no password the platform holds"
