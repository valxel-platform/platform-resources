"""Writes the OpenChoreo resources for one product's GlitchTip (self-hosted error tracking).

    python3 observability/glitchtip/render.py --project emr --domain errors.emr.vrixel.dev \
        --from-email errors@emr.vrixel.dev --environments dev,staging,production > glitchtip-emr.yaml

One GlitchTip per product, in the product's own OpenChoreo project, so it runs in each
environment's own AWS account and region, next to the data its error reports describe
(sdlc-governance platform/error-tracking.yaml). It is:

    Resource glitchtip-db          PostgreSQL (postgres-crossplane): events, issues, the task queue
                                   and the cache; kept when unbound
    Component glitchtip            the UI and the event intake (the DSN host), port 8000, external:
                                   mobile apps report straight to it
    Component glitchtip-worker     event processing, alerts and clean-up

Secrets come from OpenChoreo SecretReferences the platform team creates first (README).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

IMAGE = "glitchtip/glitchtip:v6.0.10"
HERE = Path(__file__).resolve().parent
SIZES = {"production": "medium"}  # everything else small


def render(project: str, domain: str, from_email: str, environments: list[str], namespace: str = "default",
           retention_days: int = 90) -> list[dict]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project):
        raise ValueError(f"project {project!r} is not an OpenChoreo name")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        raise ValueError(f"domain {domain!r} is not a host name")
    owner = {"projectName": project}
    docs: list[dict] = [{
        "apiVersion": "openchoreo.dev/v1alpha1", "kind": "Resource",
        "metadata": {"name": "glitchtip-db", "namespace": namespace},
        "spec": {"owner": owner, "type": {"kind": "ClusterResourceType", "name": "postgres-crossplane"},
                 "parameters": {"database": "glitchtip", "version": "17"}},
    }]
    for env in environments:
        docs.append({
            "apiVersion": "openchoreo.dev/v1alpha1", "kind": "ResourceReleaseBinding",
            "metadata": {"name": f"glitchtip-db-{env}", "namespace": namespace},
            "spec": {"owner": {**owner, "resourceName": "glitchtip-db"}, "environment": env,
                     # Error history outlives a re-deploy: never deleted with its binding.
                     "retainPolicy": "Retain",
                     "resourceTypeEnvironmentConfigs": {"size": SIZES.get(env, "small")}},
        })
    start = (HERE / "start.sh").read_text()
    for name, role, endpoint in (("glitchtip", "web", True), ("glitchtip-worker", "worker", False)):
        docs.append({
            "apiVersion": "openchoreo.dev/v1alpha1", "kind": "Component",
            "metadata": {"name": name, "namespace": namespace,
                         "annotations": {"openchoreo.dev/description":
                                         "Self-hosted GlitchTip error tracking (Vrixel platform)"}},
            "spec": {"owner": owner, "componentType": {"kind": "ClusterComponentType", "name": "deployment/service"},
                     "autoDeploy": True},
        })
        env_vars = [
            {"key": "SERVER_ROLE", "value": role},
            {"key": "PORT", "value": "8000"},
            {"key": "GLITCHTIP_DOMAIN", "value": f"https://{domain}"},
            {"key": "CSRF_TRUSTED_ORIGINS", "value": f"https://{domain}"},
            {"key": "DEFAULT_FROM_EMAIL", "value": from_email},
            {"key": "ENABLE_USER_REGISTRATION", "value": "false"},
            {"key": "ENABLE_ORGANIZATION_CREATION", "value": "false"},
            {"key": "GLITCHTIP_MAX_EVENT_LIFE_DAYS", "value": str(retention_days)},
            {"key": "DEBUG", "value": "false"},
            {"key": "SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "glitchtip", "key": "secret-key"}}},
            {"key": "EMAIL_URL", "valueFrom": {"secretKeyRef": {"name": "glitchtip", "key": "email-url"}}},
        ]
        workload = {
            "apiVersion": "openchoreo.dev/v1alpha1", "kind": "Workload",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "owner": {**owner, "componentName": name},
                "dependencies": {"resources": [{"ref": "glitchtip-db", "envBindings": {
                    "host": "DB_HOST", "port": "DB_PORT", "database": "DB_NAME", "username": "DB_USER",
                    "password": "DB_PASSWORD"}}]},
                "container": {"image": IMAGE, "command": ["sh", "/vrixel/start.sh"], "env": env_vars,
                              "files": [{"key": "start.sh", "mountPath": "/vrixel", "value": start}]},
            },
        }
        if endpoint:
            # The DSN host: mobile apps on the internet report here, as do the web app (through its
            # own /sentry/ path) and the backends. Sign-in to the UI is GlitchTip's own (no sign-up).
            workload["spec"]["endpoints"] = {"http": {"type": "HTTP", "port": 8000, "visibility": ["external"]}}
        docs.append(workload)
    return docs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True, help="the product's OpenChoreo project")
    p.add_argument("--domain", required=True, help="GlitchTip's public host, e.g. errors.emr.vrixel.dev")
    p.add_argument("--from-email", required=True)
    p.add_argument("--environments", default="dev,staging,production")
    p.add_argument("--namespace", default="default")
    p.add_argument("--retention-days", type=int, default=90)
    a = p.parse_args(argv)
    docs = render(a.project, a.domain, a.from_email, [e for e in a.environments.split(",") if e], a.namespace,
                  a.retention_days)
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
