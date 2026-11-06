"""The module matches the platform catalog (sdlc-governance platform/aws-resources.yaml): every
type the SDLC can pick has a ClusterResourceType, an XRD and an AWS Composition, with the same
parameters and outputs, and stored data is never deleted with its binding.

The catalog comes from $VRIXEL_CATALOG (a path), or from sdlc-governance on GitHub at
$VRIXEL_CATALOG_REF (default main)."""

import os
import re
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# Provided by OpenChoreo's provisioner-crossplane module, not here.
OPENCHOREO_TYPES = {"postgres-crossplane", "redis-crossplane"}


def load_catalog() -> dict:
    path = os.environ.get("VRIXEL_CATALOG")
    if path:
        return yaml.safe_load(Path(path).read_text())
    ref = os.environ.get("VRIXEL_CATALOG_REF", "main")
    url = f"https://raw.githubusercontent.com/vrixel-tools/sdlc-governance/{ref}/platform/aws-resources.yaml"
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https URL
        return yaml.safe_load(r.read())


CATALOG = load_catalog()
TYPES = sorted(t for t in CATALOG["types"] if t not in OPENCHOREO_TYPES)


def doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("kind", TYPES)
def test_every_catalog_type_is_built(kind):
    for f in (f"apis/{kind}/definition.yaml", f"apis/{kind}/resource-type.yaml", f"aws/{kind}-composition.yaml",
              f"aws/render/{kind}/xr.yaml", f"aws/render/{kind}/observed.yaml"):
        assert (ROOT / f).is_file(), f"{kind}: {f} is missing"


@pytest.mark.parametrize("kind", TYPES)
def test_parameters_and_outputs_match_the_catalog(kind):
    t = CATALOG["types"][kind]
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    assert rt["kind"] == "ClusterResourceType" and rt["metadata"]["name"] == kind
    params = rt["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    missing = set(t.get("parameters") or {}) - set(params)
    assert not missing, f"{kind}: the ResourceType lacks catalog parameters {missing}"
    if t.get("access"):
        assert {"consumers", "tags"} <= set(params), f"{kind}: needs consumers and tags"
    assert [o["name"] for o in rt["spec"]["outputs"]] == list(t.get("outputs") or {}), f"{kind}: outputs differ"
    for p, spec in (t.get("parameters") or {}).items():
        if spec.get("required"):
            assert p in rt["spec"]["parameters"]["openAPIV3Schema"].get("required", []), f"{kind}: {p} is required"


@pytest.mark.parametrize("kind", TYPES)
def test_the_resource_type_renders_its_xrd(kind):
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    xrd = doc(ROOT / f"apis/{kind}/definition.yaml")
    spec = xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    params = rt["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    assert set(spec) == set(params) | {"size"}, f"{kind}: XRD spec and ResourceType parameters differ"
    template = rt["spec"]["resources"][0]["template"]
    assert template["kind"] == xrd["spec"]["names"]["kind"]
    assert template["apiVersion"] == f"{xrd['spec']['group']}/v1alpha1"
    assert set(template["spec"]) == set(spec)
    status = xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
    for o in rt["spec"]["outputs"]:
        field = re.search(r"status\.(\w+)", o["value"]).group(1)
        assert field in status, f"{kind}: output {o['name']} reads status.{field}, which the XRD lacks"


@pytest.mark.parametrize("kind", TYPES)
def test_stored_data_is_never_deleted_with_its_binding(kind):
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    comp = (ROOT / f"aws/{kind}-composition.yaml").read_text()
    if CATALOG["types"][kind].get("stateful"):
        assert rt["spec"]["retainPolicy"] == "Retain"
        # Crossplane v2 namespaced resources: no Delete in managementPolicies keeps the AWS resource.
        assert 'managementPolicies: ["Observe", "Create", "Update", "LateInitialize"]' in comp
    assert "kmsKeyArn" in comp or kind == "workload-identity", f"{kind}: not encrypted with the product key"


@pytest.mark.parametrize("kind", [k for k in TYPES if CATALOG["types"][k].get("access")])
def test_catalog_actions_belong_to_the_service(kind):
    prefix = {"dynamodb-table": "dynamodb", "kinesis-stream": "kinesis", "sqs-queue": "sqs", "s3-bucket": "s3",
              "sns-topic": "sns"}[kind]
    for verb, actions in CATALOG["types"][kind]["access"].items():
        assert all(a.split(":", 1)[0] == prefix for a in actions), f"{kind}: {verb} grants {actions}"
