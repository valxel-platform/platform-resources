"""The module matches the platform catalog (sdlc-governance platform/aws-resources.yaml): every
type the SDLC can pick has a ClusterResourceType, an XRD and an AWS Composition, with the same
parameters and outputs, and stored data is never deleted with its binding.

The catalog comes from $VALXEL_CATALOG (a path), or from sdlc-governance on GitHub at
$VALXEL_CATALOG_REF (default main)."""

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
    path = os.environ.get("VALXEL_CATALOG")
    if path:
        return yaml.safe_load(Path(path).read_text())
    ref = os.environ.get("VALXEL_CATALOG_REF", "main")
    url = f"https://raw.githubusercontent.com/valxel-platform/sdlc-governance/{ref}/platform/aws-resources.yaml"
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
    # Kafka encrypts at rest with the cluster's key (MSK's setting), not per topic; identities hold no data.
    assert "kmsKeyArn" in comp or kind in ("workload-identity", "kafka-topic", "kafka-user"), \
        f"{kind}: not encrypted with the product key"


@pytest.mark.parametrize("kind", [k for k in TYPES if CATALOG["types"][k].get("access")])
def test_catalog_actions_belong_to_the_service(kind):
    prefix = {"dynamodb-table": "dynamodb", "kinesis-stream": "kinesis", "sqs-queue": "sqs", "s3-bucket": "s3",
              "sns-topic": "sns", "kafka-topic": "kafka-cluster"}[kind]
    for verb, actions in CATALOG["types"][kind]["access"].items():
        assert all(a.split(":", 1)[0] == prefix for a in actions), f"{kind}: {verb} grants {actions}"


ALARM = re.compile(r'\(dict "name" "(?P<name>[^"]+)"(?P<rest>.*?)"says" "(?P<says>(?:[^"\\]|\\.)*)"\)', re.S)


def composed_alarms(kind: str) -> dict[str, dict]:
    """The alarms a Composition builds: its $alarms list, field by field."""
    out = {}
    for m in ALARM.finditer((ROOT / f"aws/{kind}-composition.yaml").read_text()):
        fields = dict(re.findall(r'"(\w+)" ("[^"]*"|\d+|\([^)]*\))', m.group("rest")))
        out[m.group("name")] = {k: v.strip('"') for k, v in fields.items()} | {"says": m.group("says").replace('\\"', '"')}
    return out


@pytest.mark.parametrize("kind", TYPES)
def test_every_resource_gets_its_catalog_alarms(kind):
    t = CATALOG["types"][kind]
    want = t.get("alarms") or {}
    got = composed_alarms(kind)
    assert set(got) == set(want), f"{kind}: alarms {sorted(got)} built, the catalog has {sorted(want)}"
    for name, a in want.items():
        g = got[name]
        assert g["namespace"] == t["cloudwatch_namespace"] and g["metric"] == a["metric"], f"{kind}/{name}: metric"
        assert (g["statistic"], int(g["period"]), int(g["periods"]), float(g["above"]), g["severity"], g["says"]) == (
            a["statistic"], a["period_s"], a["periods"], float(a["above"]), a["severity"], a["says"]), f"{kind}/{name}"
        assert g["dimensions"].startswith("(dict "), f"{kind}/{name}: alarms need the resource's dimensions"
    comp = (ROOT / f"aws/{kind}-composition.yaml").read_text()
    if want:
        # Named with the data plane's prefix, which AlarmRouting's EventBridge rule matches, and
        # saying whose they are on the first line of the description, which the SDLC tool reads.
        assert "$alarmBase" in comp and "valxel product=%s feature=%s resource=%s env=%s consumers=%s" in comp
    else:
        assert "kind: MetricAlarm" not in comp


KAFKA = ("kafka-topic", "kafka-user")


@pytest.mark.parametrize("kind", KAFKA)
def test_kafka_types_build_on_strimzi_too(kind):
    """Outside production the data plane's Kafka is Strimzi: each Kafka type has a Composition and
    render fixtures there as well, for the same XRD."""
    for f in (f"strimzi/{kind}-composition.yaml", f"strimzi/render/{kind}/xr.yaml", f"strimzi/render/{kind}/observed.yaml"):
        assert (ROOT / f).is_file(), f"{kind}: {f} is missing"
    xrd = doc(ROOT / f"apis/{kind}/definition.yaml")
    for flavour in ("aws", "strimzi"):
        comp = doc(ROOT / f"{flavour}/{kind}-composition.yaml")
        assert comp["spec"]["compositeTypeRef"]["kind"] == xrd["spec"]["names"]["kind"]


def test_kafka_access_is_only_what_the_contract_gives():
    """MSK: one policy per component, its consumer groups only, the dead-letter topic only for
    readers. Strimzi: ACLs for the component's own SCRAM user, groups prefixed with its name."""
    msk = (ROOT / "aws/kafka-topic-composition.yaml").read_text()
    assert '"%s:group/%s/%s/%s*" $arn $k.clusterName $k.clusterUuid .component' in msk
    assert '(printf "%s.dlt" $topicArn)' in msk and 'has "read" $access' in msk
    assert 'managementPolicies: ["Observe", "Create", "Update", "LateInitialize"]' in msk
    strimzi = (ROOT / "strimzi/kafka-topic-composition.yaml").read_text()
    assert '"type" "Group" "name" $c.component "pattern" "Prefixed" "op" "Read"' in strimzi
    assert 'printf "User:%s%s-%s" $env.namePrefix $c.component $nsHash' in strimzi
    user = (ROOT / "strimzi/kafka-user-composition.yaml").read_text()
    assert 'printf "%s%s-%s" $env.namePrefix $xr.spec.component $nsHash' in user and "SCRAM-SHA-512" in user
    assert "provider-kafka:" in (ROOT / "aws/provider.yaml").read_text()


def test_alarm_routing_sends_the_prefixed_alarms_to_the_tool():
    comp = (ROOT / "aws/alarm-routing-composition.yaml").read_text()
    assert '"alarmName" (list (dict "prefix" $env.namePrefix))' in comp
    assert '"CloudWatch Alarm State Change"' in comp and "events:InvokeApiDestination" in comp
    xrd = doc(ROOT / "apis/alarm-routing/definition.yaml")
    assert xrd["spec"]["names"]["kind"] == "AlarmRouting"
    for f in ("aws/render/alarm-routing/xr.yaml", "aws/render/alarm-routing/observed.yaml"):
        assert (ROOT / f).is_file()
    providers = (ROOT / "aws/provider.yaml").read_text()
    assert "provider-aws-cloudwatchevents:" in providers
