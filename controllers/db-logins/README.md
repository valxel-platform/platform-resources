# db-logins

Valxel's database login controller (constitution VX-20). It reconciles `DatabaseLogin` objects
(`deploy/crd.yaml`) in every namespace, and makes each one in its database, signing in as the
platform's own identity with short-lived tokens. No password is stored anywhere.

- `src/db_logins/postgres.py`: the SQL, a pure function of the login's spec (`plan`), and applying it
  in one transaction with its checks (`apply`, `remove`).
- `src/db_logins/cloud.py`: signing in (Cloud SQL IAM, Microsoft Entra), and the database role a
  component's WorkloadIdentity signs in as.
- `src/db_logins/main.py`: the kopf handlers: on every change, every ten minutes, and on delete.

Installing it (platform-foundation): `deploy/crd.yaml` and `deploy/controller.yaml`, with the image
pinned by digest, and the controller's ServiceAccount bound to the installation's db-logins identity
(GCP: `iam.gke.io/gcp-service-account`; Azure: `azure.workload.identity/client-id`). That identity is
what each database's Composition makes its admin (`dbLoginsServiceAccount`, `dbLoginsIdentity` in the
EnvironmentConfig).

Tests: `DB_LOGINS_TEST_URL=<superuser conninfo> python -m pytest tests`; end to end, `e2e/check.sh`.
