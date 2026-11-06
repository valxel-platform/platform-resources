"""The GlitchTip package renders what a product needs, in its own project and environments."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "observability" / "glitchtip"))

import render  # noqa: E402


def docs():
    return render.render("emr", "errors.emr.vrixel.dev", "errors@emr.vrixel.dev", ["dev", "staging", "production"])


def test_one_glitchtip_per_product_with_its_database_kept():
    d = docs()
    kinds = [(x["kind"], x["metadata"]["name"]) for x in d]
    assert kinds == [("Resource", "glitchtip-db"), ("ResourceReleaseBinding", "glitchtip-db-dev"),
                     ("ResourceReleaseBinding", "glitchtip-db-staging"), ("ResourceReleaseBinding", "glitchtip-db-production"),
                     ("Component", "glitchtip"), ("Workload", "glitchtip"),
                     ("Component", "glitchtip-worker"), ("Workload", "glitchtip-worker")]
    assert all(x["spec"]["owner"]["projectName"] == "emr" for x in d)
    bindings = [x for x in d if x["kind"] == "ResourceReleaseBinding"]
    assert {b["spec"]["retainPolicy"] for b in bindings} == {"Retain"}
    assert [b["spec"]["resourceTypeEnvironmentConfigs"]["size"] for b in bindings] == ["small", "small", "medium"]
    assert d[0]["spec"]["type"]["name"] == "postgres-crossplane"


def test_web_and_worker_share_settings_and_secrets_stay_out():
    web, worker = (x for x in docs() if x["kind"] == "Workload")
    env = {e["key"]: e for e in web["spec"]["container"]["env"]}
    assert env["SERVER_ROLE"]["value"] == "web" and env["GLITCHTIP_DOMAIN"]["value"] == "https://errors.emr.vrixel.dev"
    assert env["ENABLE_USER_REGISTRATION"]["value"] == "false"
    assert env["SECRET_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "glitchtip", "key": "secret-key"}
    assert "value" not in env["EMAIL_URL"]
    assert web["spec"]["endpoints"]["http"] == {"type": "HTTP", "port": 8000, "visibility": ["external"]}
    assert "endpoints" not in worker["spec"]
    assert {e["key"]: e.get("value") for e in worker["spec"]["container"]["env"]}["SERVER_ROLE"] == "worker"
    for w in (web, worker):
        c = w["spec"]["container"]
        assert c["image"].startswith("glitchtip/glitchtip:v6.") and c["command"] == ["sh", "/vrixel/start.sh"]
        assert c["files"][0]["mountPath"] == "/vrixel" and "exec ./bin/start.sh" in c["files"][0]["value"]
        assert w["spec"]["dependencies"]["resources"][0]["envBindings"]["password"] == "DB_PASSWORD"


@pytest.mark.parametrize("bad", [{"project": "EMR"}, {"domain": "not a host"}])
def test_bad_names_are_refused(bad):
    args = {"project": "emr", "domain": "errors.emr.vrixel.dev"} | bad
    with pytest.raises(ValueError):
        render.render(args["project"], args["domain"], "e@x.dev", ["production"])


def test_the_start_script_builds_the_database_url(tmp_path):
    """Runs start.sh with fakes for GlitchTip's own scripts: it encodes the password, requires TLS,
    empties VALKEY_URL, migrates only for the web role, then hands over to bin/start.sh."""
    code = tmp_path / "code"
    (code / "bin").mkdir(parents=True)
    (code / "manage.py").write_text("#!/bin/sh\necho migrate \"$@\" >> \"$LOG\"\n")
    (code / "bin" / "start.sh").write_text("#!/bin/sh\necho \"start $SERVER_ROLE $DATABASE_URL [$VALKEY_URL]\" >> \"$LOG\"\n")
    for f in (code / "manage.py", code / "bin" / "start.sh"):
        f.chmod(0o755)
    script = (ROOT / "observability" / "glitchtip" / "start.sh").read_text().replace("cd /code", f"cd {code}")
    (tmp_path / "start.sh").write_text(script)
    env = {"PATH": "/usr/bin:/bin", "LOG": str(tmp_path / "log"), "DB_HOST": "db.internal", "DB_PORT": "5432",
           "DB_NAME": "glitchtip", "DB_USER": "gt", "DB_PASSWORD": "p@ss/w:rd"}
    for role in ("web", "worker"):
        subprocess.run(["sh", str(tmp_path / "start.sh")], env={**env, "SERVER_ROLE": role}, check=True)
    log = (tmp_path / "log").read_text().splitlines()
    assert log == ["migrate migrate --noinput",
                   "start web postgres://gt:p%40ss%2Fw%3Ard@db.internal:5432/glitchtip?sslmode=require []",
                   "start worker postgres://gt:p%40ss%2Fw%3Ard@db.internal:5432/glitchtip?sslmode=require []"]


def test_the_command_line_writes_yaml():
    out = subprocess.run([sys.executable, str(ROOT / "observability" / "glitchtip" / "render.py"), "--project", "apx",
                          "--domain", "errors.apx.vrixel.dev", "--from-email", "e@apx.vrixel.dev",
                          "--environments", "production"], check=True, capture_output=True, text=True).stdout
    assert [d["kind"] for d in yaml.safe_load_all(out)].count("Workload") == 2


def _crd_schemas() -> dict:
    """OpenChoreo's CRD schemas, from $OPENCHOREO_CRDS (a directory) or GitHub at $OPENCHOREO_REF."""
    import os
    import urllib.request

    names = ["resources", "resourcereleasebindings", "components", "workloads"]
    local = os.environ.get("OPENCHOREO_CRDS")
    ref = os.environ.get("OPENCHOREO_REF", "main")
    out = {}
    for n in names:
        if local:
            text = (Path(local) / f"openchoreo.dev_{n}.yaml").read_text()
        else:
            url = ("https://raw.githubusercontent.com/openchoreo/openchoreo/"
                   f"{ref}/install/helm/openchoreo-control-plane/crds/openchoreo.dev_{n}.yaml")
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https URL
                text = r.read().decode()
        crd = yaml.safe_load(text)
        version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
        out[crd["spec"]["names"]["kind"]] = version["schema"]["openAPIV3Schema"]
    return out


def _plain(schema):
    if isinstance(schema, dict):
        return {k: _plain(v) for k, v in schema.items() if not k.startswith("x-kubernetes")}
    return [_plain(x) for x in schema] if isinstance(schema, list) else schema


def test_everything_validates_against_openchoreos_schemas():
    jsonschema = pytest.importorskip("jsonschema")
    schemas = _crd_schemas()
    for doc in docs():
        jsonschema.validate(doc, _plain(schemas[doc["kind"]]))
