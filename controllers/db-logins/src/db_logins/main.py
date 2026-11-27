"""The controller: reconciles DatabaseLogin objects (platform.valxel.dev/v1alpha1) in every namespace.

A product's postgres-login composes one DatabaseLogin in its namespace. The controller runs in its
own platform namespace with the only database-admin identity, signs in with short-lived tokens, and
makes the login as `postgres.apply` says, every time the spec changes and every ten minutes (a grant
changed by hand is put back). It writes what the workload needs into the status: host, port,
database, username, and the names of its break-glass role and tokenize function; no secret, because
the workload signs in with its own identity too.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import kopf

from . import cloud, postgres

GROUP, VERSION, PLURAL = "platform.valxel.dev", "v1alpha1", "databaselogins"
IDENTITIES = ("aws.platform.valxel.dev", "v1alpha1", "workloadidentities")
log = logging.getLogger("db-logins")


def _api() -> Any:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client


def _server(namespace: str, spec: dict[str, Any]) -> tuple[cloud.Server, str]:
    client = _api()
    name = str(spec.get("server") or "")
    try:
        cm = client.CoreV1Api().read_namespaced_config_map(name, namespace)
    except client.ApiException as exc:
        raise cloud.NotReady(f"the database's ConfigMap {name} isn't there yet") from exc
    server = cloud.server_from_configmap(cm.data or {})
    password = ""
    if server.auth == "password":
        password = _secret(namespace, server.password_secret, "password")
    return server, password


def _secret(namespace: str, name: str, key: str) -> str:
    client = _api()
    data = client.CoreV1Api().read_namespaced_secret(name, namespace).data or {}
    return base64.b64decode(data[key]).decode()


def _principal(namespace: str, spec: dict[str, Any], server: cloud.Server) -> tuple[cloud.Principal, str]:
    who = spec.get("principal") or {}
    if "component" in who:
        client = _api()
        items = client.CustomObjectsApi().list_namespaced_custom_object(*IDENTITIES[:2], namespace, IDENTITIES[2])
        mine = [i for i in items.get("items", []) if (i.get("spec") or {}).get("component") == who["component"]]
        if len(mine) != 1:
            raise cloud.NotReady(f"{len(mine)} workload identities for component {who['component']}: waiting for one")
        return cloud.principal_from_identity(server, mine[0]), ""
    if "password" in who and cloud.password_allowed():
        ref = who["password"]
        return cloud.Principal(name=str(ref["user"]), kind="password"), _secret(namespace, ref["secret"], "password")
    raise kopf.PermanentError("principal must name a component (or, in the end-to-end test only, a password)")


def reconcile(spec: dict[str, Any], namespace: str) -> dict[str, Any]:
    server, admin_password = _server(namespace, spec)
    principal, login_password = _principal(namespace, spec, server)
    if principal.kind == "gcp-iam":
        cloud.ensure_gcp_user(server, principal)
    login = postgres.login_from_spec({**spec, "database": server.database}, principal.name, principal.kind,
                                     azure_object_id=principal.azure_object_id, password=login_password)
    with cloud.connect(server, admin_password) as conn:
        made = postgres.apply(conn, login)
    return {**made, "host": server.host, "port": str(server.port), "auth": principal.kind}


def _status(patch: kopf.Patch, ready: bool, reason: str, message: str, made: dict[str, Any] | None = None) -> None:
    patch.status["conditions"] = [{"type": "Ready", "status": "True" if ready else "False", "reason": reason,
                                   "message": message[:1000]}]
    if made:
        patch.status.update(made)


@kopf.on.resume(GROUP, VERSION, PLURAL)
@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
@kopf.timer(GROUP, VERSION, PLURAL, interval=600, initial_delay=60)
def converge(spec: dict[str, Any], namespace: str, patch: kopf.Patch, **_: Any) -> None:
    try:
        made = reconcile(dict(spec), namespace)
    except cloud.NotReady as exc:
        _status(patch, False, "Waiting", str(exc))
        raise kopf.TemporaryError(str(exc), delay=30) from exc
    except postgres.LoginError as exc:
        _status(patch, False, "Refused", str(exc))
        raise kopf.TemporaryError(str(exc), delay=300) from exc
    _status(patch, True, "Made", "the login has exactly its grants", made)


@kopf.on.delete(GROUP, VERSION, PLURAL)
def remove(spec: dict[str, Any], namespace: str, **_: Any) -> None:
    try:
        server, admin_password = _server(namespace, dict(spec))
        principal, _pw = _principal(namespace, dict(spec), server)
    except cloud.NotReady:
        return  # the database or the identity is gone already: nothing to take back
    login = postgres.login_from_spec({**spec, "database": server.database}, principal.name, principal.kind,
                                     azure_object_id=principal.azure_object_id)
    with cloud.connect(server, admin_password) as conn:
        kept = postgres.remove(conn, login)
    if kept:
        log.warning("login %s/%s: %s", namespace, spec.get("login"), kept)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.persistence.finalizer = "platform.valxel.dev/db-logins"
    settings.posting.level = logging.WARNING
    settings.watching.server_timeout = 300


def run() -> None:
    kopf.run(clusterwide=True, standalone=True)
