#!/bin/sh
# Starts GlitchTip in an OpenChoreo component. Mounted at /valxel/start.sh by the Workload.
#
# The database Resource gives host, port, name, user and password as separate variables (DB_*);
# GlitchTip reads one DATABASE_URL, so it is built here, with the user and password URL-encoded and
# TLS required. VALKEY_URL is empty: GlitchTip 6 then keeps its task queue and cache in Postgres,
# so the database is the only thing it depends on.
#   SERVER_ROLE=web     migrate, then serve the UI and the event intake (port 8000)
#   SERVER_ROLE=worker  process events, alerts and clean-up
set -eu
cd /code

DATABASE_URL=$(python3 - <<'PY'
import os
from urllib.parse import quote

e = os.environ
print(f"postgres://{quote(e['DB_USER'], safe='')}:{quote(e['DB_PASSWORD'], safe='')}"
      f"@{e['DB_HOST']}:{e.get('DB_PORT') or '5432'}/{e['DB_NAME']}?sslmode=require")
PY
)
export DATABASE_URL
export VALKEY_URL=""

if [ "${SERVER_ROLE:-web}" = "web" ]; then
  ./manage.py migrate --noinput
fi
exec ./bin/start.sh
