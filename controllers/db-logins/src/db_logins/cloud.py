"""Signing in without stored passwords, and finding the identity a login is for.

The controller signs in to a database as the platform's own identity, which the database's
Composition made its admin: a Cloud SQL IAM user with cloudsqlsuperuser (GCP), or the server's
Microsoft Entra administrator (Azure). Its password is a short-lived access token from the
controller's workload identity, asked for at each connection.

What a database offers is in its ConfigMap <database>-sql-admin, made by its Composition (no
secret in it): host, port, database, auth, adminUser, and on GCP the instance (project:instance).

A workload signs in as its own workload identity (the component's WorkloadIdentity, which the
platform already makes per component):
  - GCP: the service account's IAM database user, named its email without .gserviceaccount.com.
    The controller makes it through the Cloud SQL Admin API (users.insert, CLOUD_IAM_SERVICE_ACCOUNT).
  - Azure: the user-assigned identity's name, made a principal by the Entra admin with its object
    (principal) id.
`password` (ConfigMap auth, and a login's principal) is for the end-to-end test only: the
controller refuses it unless DB_LOGINS_ALLOW_PASSWORD=1, which no environment sets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

GCP_SUFFIX = ".gserviceaccount.com"
GCP_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"
AZURE_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
SQLADMIN = "https://sqladmin.googleapis.com/v1/projects/{project}/instances/{instance}/users"


class NotReady(Exception):
    """Something the login needs isn't there yet (the identity, its status); retried later."""


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    database: str
    auth: str  # gcp-iam | azure-entra | password
    admin_user: str
    instance: str = ""  # GCP: project:instance
    password_secret: str = ""  # password only (end-to-end test)


def password_allowed() -> bool:
    return os.environ.get("DB_LOGINS_ALLOW_PASSWORD") == "1"


def server_from_configmap(data: dict[str, str]) -> Server:
    auth = data.get("auth", "")
    if auth not in ("gcp-iam", "azure-entra", "password"):
        raise NotReady(f"the database's ConfigMap names no known auth ({auth!r})")
    if auth == "password" and not password_allowed():
        raise NotReady("password sign-in is for the end-to-end test only (VX-20)")
    return Server(host=data["host"], port=int(data.get("port", "5432")), database=data["database"], auth=auth,
                  admin_user=data["adminUser"], instance=data.get("instance", ""),
                  password_secret=data.get("passwordSecret", ""))


def token(auth: str) -> str:
    """A short-lived access token for the controller's own workload identity."""
    if auth == "gcp-iam":
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=[GCP_SCOPE])
        creds.refresh(google.auth.transport.requests.Request())
        return str(creds.token)
    if auth == "azure-entra":
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential().get_token(AZURE_SCOPE).token
    raise ValueError(auth)


def connect(server: Server, password: str = "") -> Any:
    import psycopg

    secret = password if server.auth == "password" else token(server.auth)
    return psycopg.connect(host=server.host, port=server.port, dbname=server.database, user=server.admin_user,
                           password=secret, sslmode="disable" if server.auth == "password" else "require",
                           autocommit=True, connect_timeout=10, application_name="valxel-db-logins")


@dataclass(frozen=True)
class Principal:
    name: str  # the database role
    kind: str  # gcp-iam | azure-entra | password
    azure_object_id: str = ""
    email: str = ""  # GCP: the service account


def principal_from_identity(server: Server, identity: dict[str, Any]) -> Principal:
    """The database role a component's WorkloadIdentity signs in as."""
    status = identity.get("status") or {}
    if server.auth == "gcp-iam":
        email = str(status.get("roleArn") or "")
        if not email.endswith(GCP_SUFFIX):
            raise NotReady("the component's service account isn't ready yet")
        return Principal(name=email.removesuffix(GCP_SUFFIX), kind="gcp-iam", email=email)
    if server.auth == "azure-entra":
        name, oid = str(status.get("roleName") or ""), str(status.get("principalId") or "")
        if not (name and oid):
            raise NotReady("the component's managed identity isn't ready yet")
        return Principal(name=name, kind="azure-entra", azure_object_id=oid)
    raise NotReady(f"a component identity can't sign in to a {server.auth} database")


def ensure_gcp_user(server: Server, principal: Principal) -> None:
    """The Cloud SQL IAM database user for a service account (no database roles: grants come after)."""
    import google.auth
    import google.auth.transport.requests

    project, _, instance = server.instance.partition(":")
    if not (project and instance):
        raise NotReady("the database's ConfigMap names no Cloud SQL instance")
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/sqlservice.admin"])
    session = google.auth.transport.requests.AuthorizedSession(creds)
    url = SQLADMIN.format(project=project, instance=instance)
    resp = session.post(url, json={"name": principal.name, "type": "CLOUD_IAM_SERVICE_ACCOUNT"}, timeout=30)
    if resp.status_code not in (200, 409):  # 409: it exists
        raise NotReady(f"Cloud SQL didn't make the IAM user {principal.name}: {resp.status_code} {resp.text[:200]}")
