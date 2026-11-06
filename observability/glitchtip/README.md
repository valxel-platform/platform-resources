# GlitchTip: self-hosted error tracking

Where a product's error reports must stay in its own account and region (sdlc-governance
`platform/error-tracking.yaml`: payments, and data in India), they go to the product's own GlitchTip
instead of Sentry's SaaS. GlitchTip accepts the Sentry SDKs the templates already use, so a
component only needs a different `SENTRY_DSN`.

`render.py` writes the OpenChoreo resources for one product, in its own project, so GlitchTip runs
in each environment's own AWS account (product.yaml `runtime.aws`):

| | |
| --- | --- |
| `Resource glitchtip-db` | PostgreSQL (`postgres-crossplane`). GlitchTip 6 keeps events, issues, its task queue and cache there (`VALKEY_URL` empty), so it is the only dependency. Kept when unbound; backed up like any product database |
| `Component glitchtip` | The UI and the event intake (the DSN host), port 8000, `external`: mobile apps report to it from the internet |
| `Component glitchtip-worker` | Event processing, alerts and clean-up (`SERVER_ROLE=worker`) |

Both run `glitchtip/glitchtip` (pinned in `render.py`) through `start.sh`, which builds GlitchTip's
`DATABASE_URL` from the database's outputs (TLS required) and migrates before the web role starts.

## Set up a product (once)

1. Secrets, in the product's namespace, from the observability plane's secret store:

   ```bash
   occ secret create generic glitchtip --namespace default --target-plane ClusterObservabilityPlane/default \
     --from-literal=secret-key="$(openssl rand -hex 32)" \
     --from-literal=email-url='smtp://user:password@smtp.example.com:587'
   ```

2. Render and apply, then promote the components to staging and production like any other:

   ```bash
   python3 observability/glitchtip/render.py --project apx --domain errors.apx.vrixel.dev \
     --from-email errors@apx.vrixel.dev --environments dev,staging,production | kubectl apply -f -
   ```

3. Point the domain at the `glitchtip` endpoint, and create the first admin (sign-up is off):
   `./manage.py createsuperuser` in the `glitchtip` container.

4. In GlitchTip: an organization for the product, and one project per component, named after the
   component (`apx-ledger-api`): the SDLC tool maps issues to components by project name. Give each
   component its project's DSN as `SENTRY_DSN` (a SecretReference bound in its workload).

5. Alerts to the SDLC tool: in each project, an alert with a webhook recipient
   `https://<tool>/api/signals/glitchtip/<PRODUCT ID>?token=<token>` (the tool shows the token for
   the product: `vrixel glitchtip-token <PRODUCT ID>`). For the tool to read an issue's trace id and
   tie it to other components' failures, create an API token with `event:read` and give it to the
   tool (`VRIXEL_GLITCHTIP_TOKENS`, see the tool's README).

## Running it

- Retention: 90 days of events (`--retention-days`).
- Upgrades: raise `IMAGE` in `render.py`, render and apply; the web role migrates on start.
- Size: the database is `small` outside production and `medium` in production; GlitchTip needs
  roughly 30 GB per million events a month.
