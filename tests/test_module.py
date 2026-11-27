"""The module matches the platform catalog (sdlc-governance platform/catalog.yaml, version 2): every
type the SDLC can pick has a ClusterResourceType, an XRD and a Composition for each cloud the
catalog offers it on, with the same parameters and outputs, the catalog's access, encryption with
the product key and the catalog's alarms; stored data is never deleted with its binding.

The version 1 AWS types (dynamodb-table, sqs-queue...) stay for the resources made under them: each
is an alias of its neutral type in the catalog, and scripts/neutral.py writes the neutral type's AWS
side from it.

The catalog comes from $VALXEL_CATALOG (a path), or from sdlc-governance on GitHub at
$VALXEL_CATALOG_REF (default main)."""

import importlib.util
import os
import re
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
KAFKA = ("kafka-topic", "kafka-user")
SQL = ("postgres-login",)  # made inside the database by the db-logins controller: one Composition for every cloud


def load_catalog() -> dict:
    path = os.environ.get("VALXEL_CATALOG")
    if path:
        return yaml.safe_load(Path(path).read_text())
    ref = os.environ.get("VALXEL_CATALOG_REF", "main")
    url = f"https://raw.githubusercontent.com/valxel-platform/sdlc-governance/{ref}/platform/catalog.yaml"
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https URL
        return yaml.safe_load(r.read())


CATALOG = load_catalog()
assert CATALOG.get("version") == 2, "this module reads version 2 of the catalog (platform/catalog.yaml)"



def built_here(kind: str, cloud: str) -> bool:
    """A cloud's block naming another module's ClusterResourceType (`resource_type`, OpenChoreo's
    postgres-crossplane on AWS) isn't built here."""
    return CATALOG["types"][kind]["clouds"][cloud].get("resource_type", kind) == kind


ON = [(k, c) for k in sorted(CATALOG["types"]) for c in sorted(CATALOG["types"][k]["clouds"]) if built_here(k, c)]
TYPES = sorted({k for k, _ in ON})
# Aliases of types built here on AWS: one whose AWS ClusterResourceType is another module's
# (postgres-crossplane) is that module's.
ALIASES = sorted((a, k) for k in TYPES if "aws" in CATALOG["types"][k]["clouds"] and built_here(k, "aws")
                 for a in CATALOG["types"][k].get("aliases") or [])


def doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def flavour(kind: str, cloud: str) -> str:
    """The directory of a type's Composition on a cloud: Kafka on Google Cloud and Azure is Strimzi;
    a login inside a database is sql/ everywhere."""
    if kind in SQL:
        return "sql"
    return "strimzi" if cloud in ("gcp", "azure") and kind in KAFKA else cloud


def block(kind: str, cloud: str) -> dict:
    return CATALOG["types"][kind]["clouds"][cloud]


def outputs(kind: str, cloud: str) -> dict:
    return block(kind, cloud).get("outputs") or CATALOG["types"][kind].get("outputs") or {}


@pytest.mark.parametrize(("kind", "cloud"), ON)
def test_every_catalog_type_is_built(kind, cloud):
    f = flavour(kind, cloud)
    for path in (f"apis/{kind}/definition.yaml", f"apis/{kind}/resource-type.yaml", f"{f}/{kind}-composition.yaml",
                 f"{f}/render/{kind}/xr.yaml", f"{f}/render/{kind}/observed.yaml"):
        assert (ROOT / path).is_file(), f"{kind} on {cloud}: {path} is missing"
    comp = doc(ROOT / f"{f}/{kind}-composition.yaml")
    xrd = doc(ROOT / f"apis/{kind}/definition.yaml")
    assert comp["spec"]["compositeTypeRef"] == {"apiVersion": f"{xrd['spec']['group']}/v1alpha1",
                                                 "kind": xrd["spec"]["names"]["kind"]}


@pytest.mark.parametrize("kind", TYPES)
def test_parameters_and_outputs_match_the_catalog(kind):
    t = CATALOG["types"][kind]
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    assert rt["kind"] == "ClusterResourceType" and rt["metadata"]["name"] == kind
    params = rt["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    missing = set(t.get("parameters") or {}) - set(params)
    assert not missing, f"{kind}: the ResourceType lacks catalog parameters {missing}"
    if any(c.get("access") for c in t["clouds"].values()):
        assert {"consumers", "tags"} <= set(params), f"{kind}: needs consumers and tags"
    names = [o["name"] for o in rt["spec"]["outputs"]]
    for cloud in t["clouds"]:
        if built_here(kind, cloud):
            assert names == list(outputs(kind, cloud)), f"{kind}: outputs differ from the catalog's on {cloud}"
    for p, spec in (t.get("parameters") or {}).items():
        if spec.get("required"):
            assert p in rt["spec"]["parameters"]["openAPIV3Schema"].get("required", []), f"{kind}: {p} is required"


@pytest.mark.parametrize(("alias", "kind"), ALIASES)
def test_version_1_types_stay_for_their_resources(alias, kind):
    """An alias keeps its ClusterResourceType, XRD and AWS Composition, so the resources made under
    it never change type; its parameters are the neutral type's."""
    for path in (f"apis/{alias}/definition.yaml", f"apis/{alias}/resource-type.yaml", f"aws/{alias}-composition.yaml"):
        assert (ROOT / path).is_file(), f"{alias}: {path} is missing"
    old = doc(ROOT / f"apis/{alias}/resource-type.yaml")["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    new = doc(ROOT / f"apis/{kind}/resource-type.yaml")["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    assert set(old) == set(new), f"{alias} and {kind} take different parameters"


def test_the_neutral_types_aws_side_is_what_the_script_writes():
    spec = importlib.util.spec_from_file_location("neutral", ROOT / "scripts/neutral.py")
    neutral = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(neutral)
    for path, text in neutral.files().items():
        assert path.is_file() and path.read_text() == text, \
            f"{path.relative_to(ROOT)} differs from its version 1 type: run python3 scripts/neutral.py"


@pytest.mark.parametrize("kind", TYPES)
def test_the_resource_type_renders_its_xrd(kind):
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    xrd = doc(ROOT / f"apis/{kind}/definition.yaml")
    spec = xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    params = rt["spec"]["parameters"]["openAPIV3Schema"]["properties"]
    sized = {"size"} if "size" in spec else set()  # a login has no sizes
    assert set(spec) == set(params) | sized, f"{kind}: XRD spec and ResourceType parameters differ"
    template = rt["spec"]["resources"][0]["template"]
    assert template["kind"] == xrd["spec"]["names"]["kind"]
    assert template["apiVersion"] == f"{xrd['spec']['group']}/v1alpha1"
    assert set(template["spec"]) == set(spec)
    status = xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
    for o in rt["spec"]["outputs"]:
        # A value, or a Secret's key: the Secret's name is in the status, the value never is.
        field = re.search(r"status\.(\w+)", o.get("value") or o["secretKeyRef"]["name"]).group(1)
        assert field in status, f"{kind}: output {o['name']} reads status.{field}, which the XRD lacks"


@pytest.mark.parametrize(("kind", "cloud"), ON)
def test_stored_data_is_never_deleted_with_its_binding(kind, cloud):
    rt = doc(ROOT / f"apis/{kind}/resource-type.yaml")
    comp = (ROOT / f"{flavour(kind, cloud)}/{kind}-composition.yaml").read_text()
    if CATALOG["types"][kind].get("stateful"):
        assert rt["spec"]["retainPolicy"] == "Retain"
        # Crossplane v2 namespaced resources: no Delete in managementPolicies keeps the cloud resource.
        assert 'managementPolicies: ["Observe", "Create", "Update", "LateInitialize"]' in comp
    # Kafka encrypts at rest with the cluster's key, not per topic; identities hold no data. On Azure,
    # Event Hubs and Service Bus Standard take no customer-managed key (the catalog says so).
    key = {"aws": "kmsKeyArn", "gcp": "kmsKeyName", "azure": "keyVaultKeyId"}[cloud]
    exempt = ("workload-identity", *KAFKA, *SQL) + (("event-stream", "queue", "topic") if cloud == "azure" else ())
    if kind in exempt and cloud == "azure":
        assert "Microsoft-managed" in str(block(kind, cloud).get("built_in")) or kind in ("workload-identity", *KAFKA,
                                                                                           *SQL)
    assert key in comp or kind in exempt, f"{kind} on {cloud}: not encrypted with the product key"


@pytest.mark.parametrize(("kind", "cloud"), [(k, c) for k, c in ON if block(k, c).get("access")])
def test_catalog_access_belongs_to_the_service(kind, cloud):
    if cloud == "gcp":
        prefix = {"key-value-table": "roles/datastore.", "event-stream": "roles/pubsub.", "queue": "roles/pubsub.",
                  "object-bucket": "roles/storage.", "topic": "roles/pubsub."}[kind]
        ok = lambda a: a.startswith(prefix)  # noqa: E731
    elif cloud == "azure":
        prefix = {"key-value-table": "cosmos:Cosmos DB Built-in Data ", "event-stream": "role:Azure Event Hubs Data ",
                  "queue": "role:Azure Service Bus Data ", "object-bucket": "role:Storage Blob Data ",
                  "topic": "role:Azure Service Bus Data "}[kind]
        ok = lambda a: a.startswith(prefix)  # noqa: E731
    else:
        prefix = {"key-value-table": "dynamodb", "event-stream": "kinesis", "queue": "sqs", "object-bucket": "s3",
                  "topic": "sns", "kafka-topic": "kafka-cluster"}[kind]
        ok = lambda a: a.split(":", 1)[0] == prefix  # noqa: E731
    for verb, actions in block(kind, cloud)["access"].items():
        assert all(ok(a) for a in actions), f"{kind} on {cloud}: {verb} grants {actions}"
    pattern = doc(ROOT / f"apis/{kind}/definition.yaml")["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"]["spec"]["properties"]["consumers"]["items"]["properties"]["actions"]["items"]["pattern"]
    assert all(re.match(pattern, a) for actions in block(kind, cloud)["access"].values() for a in actions)


ALARM = re.compile(r'\(dict "name" "(?P<name>[^"]+)"(?P<rest>.*?)"says" "(?P<says>(?:[^"\\]|\\.)*)"\)', re.S)


def composed_alarms(path: str) -> dict[str, dict]:
    """The alarms a Composition builds: its $alarms list, field by field."""
    out = {}
    for m in ALARM.finditer((ROOT / path).read_text()):
        fields = dict(re.findall(r'"(\w+)" ("[^"]*"|\d+|\$\w+|\([^)]*\))', m.group("rest")))
        out[m.group("name")] = {k: v.strip('"') for k, v in fields.items()} | {"says": m.group("says").replace('\\"', '"')}
    return out


@pytest.mark.parametrize(("kind", "cloud"), ON)
def test_every_resource_gets_its_catalog_alarms(kind, cloud):
    b = block(kind, cloud)
    want = b.get("alarms") or {}
    path = f"{flavour(kind, cloud)}/{kind}-composition.yaml"
    got = composed_alarms(path)
    assert set(got) == set(want), f"{kind} on {cloud}: alarms {sorted(got)} built, the catalog has {sorted(want)}"
    comp = (ROOT / path).read_text()
    for name, a in want.items():
        g = got[name]
        assert g["metric"] == a["metric"], f"{kind}/{name}: metric"
        assert (int(g["period"]), int(g["periods"]), float(g["above"]), g["severity"], g["says"]) == (
            a["period_s"], a["periods"], float(a["above"]), a["severity"], a["says"]), f"{kind}/{name} on {cloud}"
        if cloud == "aws":
            assert g["namespace"] == b["cloudwatch_namespace"] and g["statistic"] == a["statistic"], f"{kind}/{name}"
            assert g["dimensions"].startswith("(dict "), f"{kind}/{name}: alarms need the resource's dimensions"
        elif cloud == "azure":
            assert (g["aggregation"], g.get("where", "")) == (a["aggregation"], a.get("where", "")), f"{kind}/{name}"
            assert f"metricNamespace: {b['monitor_namespace']}" in comp, f"{kind}/{name}: metric namespace"
        else:
            assert g["aligner"] == a["aligner"], f"{kind}/{name}: aligner"
            assert "filter" in g, f"{kind}/{name}: alert policies need the resource's filter"
    # Saying whose they are, which the SDLC tool reads: on AWS named with the data plane's prefix,
    # which AlarmRouting's EventBridge rule matches; on Google Cloud sent to AlarmRouting's channel.
    if want:
        assert "valxel product=%s feature=%s resource=%s env=%s consumers=%s" in comp
        assert {"aws": "$alarmBase", "gcp": "$env.alarmChannel", "azure": "$env.alarmActionGroup"}[cloud] in comp
    else:
        assert not any(f"kind: {k}" in comp for k in ("MetricAlarm", "AlertPolicy", "MonitorMetricAlert"))


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


def test_alarm_routing_on_google_cloud_sends_to_the_tool():
    comp = (ROOT / "gcp/alarm-routing-composition.yaml").read_text()
    assert "type: webhook_basicauth" in comp and '"/signals/cloud-monitoring"' in comp and "passwordSecretRef" in comp
    for f in ("gcp/render/alarm-routing/xr.yaml", "gcp/render/alarm-routing/observed.yaml"):
        assert (ROOT / f).is_file()
    xrd = doc(ROOT / "apis/alarm-routing/definition.yaml")
    assert "channel" in xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
    providers = (ROOT / "gcp/provider.yaml").read_text()
    assert "provider-gcp-monitoring:" in providers


def test_google_cloud_access_goes_to_each_component_s_own_service_account():
    """Every gcp/ Composition grants to the account workload-identity makes for the component."""
    wi = (ROOT / "gcp/workload-identity-composition.yaml").read_text()
    account = 'printf "%s/%s" $xr.metadata.namespace $xr.spec.component | sha256sum | trunc 24'
    assert account in wi and "roles/iam.workloadIdentityUser" in wi and ".svc.id.goog[%s/%s]" in wi
    for kind in ("key-value-table", "event-stream", "queue", "object-bucket", "topic"):
        comp = (ROOT / f"gcp/{kind}-composition.yaml").read_text()
        assert 'printf "%s/%s" $xr.metadata.namespace .component | sha256sum | trunc 24' in comp, kind
        assert "index $member $c.component" in comp, kind


def test_azure_access_goes_to_each_component_s_own_identity():
    """Every azure/ Composition assigns roles to the managed identity workload-identity makes for the
    component, which it finds by the labels workload-identity puts on it."""
    wi = (ROOT / "azure/workload-identity-composition.yaml").read_text()
    assert "valxel.dev/component: {{ $xr.spec.component | quote }}" in wi
    assert "valxel.dev/namespace-hash: {{ $nsHash | quote }}" in wi and "api://AzureADTokenExchange" in wi
    for kind in ("key-value-table", "event-stream", "queue", "object-bucket", "topic"):
        comp = (ROOT / f"azure/{kind}-composition.yaml").read_text()
        assert "kind: ExtraResources" in comp and "valxel.dev/component: {{ $c.component | quote }}" in comp, kind
        assert "principalId: {{ $pid | quote }}" in comp, kind
    routing = (ROOT / "azure/alarm-routing-composition.yaml").read_text()
    assert "useCommonAlertSchema: true" in routing and '"/signals/azure-monitor"' in routing
    assert "provider-azure-insights:" in (ROOT / "azure/provider.yaml").read_text()


def test_openchoreos_agent_may_manage_every_group_the_resource_types_render():
    """The data-plane agent applies what a ClusterResourceType renders; a group missing here is
    refused when bound (platform.valxel.dev was, after the neutral rename: found by agentic-sdlc's
    laptop-aws workflow)."""
    rendered = set()
    for f in (ROOT / "apis").glob("*/resource-type.yaml"):
        for d in yaml.safe_load_all(f.read_text()):
            for r in (d or {}).get("spec", {}).get("resources") or []:
                rendered.add(r["template"]["apiVersion"].split("/")[0])
    rbac = [d for d in yaml.safe_load_all((ROOT / "cluster-agent-rbac.yaml").read_text()) if d["kind"] == "ClusterRole"]
    allowed = {g for rule in rbac[0]["rules"] for g in rule["apiGroups"]}
    assert rendered and rendered <= allowed, rendered - allowed


LOGIN = ROOT / "sql/postgres-login-composition.yaml"


CRD = ROOT / "controllers/db-logins/deploy/crd.yaml"


def test_a_login_is_a_request_to_the_controller_with_no_secret_anywhere():
    text = "\n".join(line for line in LOGIN.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "kind: DatabaseLogin" in text and "component: {{ $xr.spec.component | quote }}" in text
    for word in ("Secret", "password", "provider-sql", "connectionSecretRef", "randAlphaNum"):
        assert word not in text, word
    rt = doc(ROOT / "apis/postgres-login/resource-type.yaml")
    outputs = {o["name"]: o for o in rt["spec"]["outputs"]}
    assert set(outputs) == {"host", "port", "database", "username", "auth", "role", "break_glass_role",
                            "tokenize_function"}
    assert all("value" in o and "secretKeyRef" not in o for o in outputs.values())
    assert rt["spec"]["retainPolicy"] == "Delete"


def test_the_login_composes_what_the_controller_accepts():
    import jsonschema

    crd = doc(CRD)
    xrd = doc(ROOT / "apis/postgres-login/definition.yaml")
    login_spec = xrd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    crd_spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert set(login_spec) - {"database", "component", "tags"} <= set(crd_spec["properties"])
    sample = {"server": "emr-sql-admin", "login": "emr_transform", "principal": {"component": "emr-transform"},
              "reads": [{"schema": "public", "table": "notes", "columns": ["note_id"]}], "createSchemas": True,
              "breakGlass": True, "tokenize": True}
    jsonschema.validate(sample, crd_spec)


def test_a_database_makes_the_controller_its_admin_and_says_where_it_is():
    for cloud, auth, admin in (("gcp", "gcp-iam", "kind: User"),
                               ("azure", "azure-entra", "kind: FlexibleServerActiveDirectoryAdministrator")):
        text = (ROOT / f"{cloud}/postgres-composition.yaml").read_text()
        assert 'name: {{ printf "%s-sql-admin" ($xr.spec.database | replace "_" "-")' in text
        assert "kind: ConfigMap" in text and f"auth: {auth}" in text and admin in text
        assert "ProviderConfig" not in text and "provider-sql" not in text
        assert "autoGeneratePassword" not in text and "kind: Secret" not in text and "randAlphaNum" not in text
    gcp = (ROOT / "gcp/postgres-composition.yaml").read_text()
    assert "type: CLOUD_IAM_SERVICE_ACCOUNT" in gcp and "databaseRoles: [cloudsqlsuperuser]" in gcp
    assert "name: cloudsql.iam_authentication" in gcp
    azure = (ROOT / "azure/postgres-composition.yaml").read_text()
    assert "activeDirectoryAuthEnabled: true" in azure and "passwordAuthEnabled: false" in azure
    assert "administratorLogin" not in azure
    rt = doc(ROOT / "apis/postgres/resource-type.yaml")
    assert {o["name"] for o in rt["spec"]["outputs"]} == {"host", "port", "database"}  # no credentials
