"""Install this module's AWS catalog into a local Kubernetes cluster (k3d), with every AWS call going
to moto, a local AWS emulator running as a Docker container beside it. Used by the CI job
local-aws (.github/workflows/local-aws.yml) and by the SDLC tool's `valxel dev cluster up --aws`.

    python3 local/aws/install.py --context k3d-valxel [--moto valxel-moto] [--crossplane]
                                 [--resource-types]

It connects the moto container to the cluster's Docker network and names it in the cluster
(moto-service.yaml); seeds what the settings name (seed.py, run inside the moto container);
installs the functions and the AWS providers (without EKS and Kafka, which have no local stand-in)
and points them at moto (provider-config.yaml); then the settings, the XRDs and the AWS
Compositions, with workload identity in its local form. --crossplane installs Crossplane first
(the platform's version). --openchoreo, for a cluster with OpenChoreo in it, also installs
OpenChoreo's ClusterResourceTypes, so a Resource bound to an environment goes OpenChoreo ->
Crossplane -> moto, and embeds the valxel-local-aws trait (component-trait.yaml) in every
ClusterComponentType, so components' pods reach moto too. Needs docker, kubectl and, with
--crossplane, helm. Safe to run again."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CROSSPLANE_VERSION = "2.4.3"  # platform-foundation gitops/infrastructure/crossplane/release.yaml
# No local stand-in: EKS Pod Identity (workload identity uses local/aws's Composition instead) and
# MSK (kafka-topic, kafka-user).
SKIP_PROVIDERS = {"provider-aws-eks", "provider-kafka"}
SKIP_COMPOSITIONS = {"kafka-topic", "kafka-user", "workload-identity"}


def run(cmd: list[str], *, input: str | None = None, check: bool = True) -> str:
    print("+", " ".join(cmd), flush=True)
    got = subprocess.run(cmd, input=input, text=True, capture_output=True)
    if check and got.returncode:
        sys.exit(f"{' '.join(cmd[:3])} failed:\n{got.stderr or got.stdout}")
    return got.stdout


def apply(ctx: str, docs: list[dict] | str) -> None:
    text = docs if isinstance(docs, str) else yaml.safe_dump_all(docs, sort_keys=False)
    run(["kubectl", "--context", ctx, "apply", "--server-side", "--force-conflicts", "-f", "-"], input=text)


def load(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def moto_address(moto: str, network: str) -> str:
    joined = json.loads(run(["docker", "inspect", moto]))[0]["NetworkSettings"]["Networks"]
    if network not in joined:
        run(["docker", "network", "connect", network, moto])
        joined = json.loads(run(["docker", "inspect", moto]))[0]["NetworkSettings"]["Networks"]
    return joined[network]["IPAddress"]


TRAIT = {"kind": "ClusterTrait", "name": "valxel-local-aws", "instanceName": "valxel-local-aws"}


def with_local_aws(types: list[dict]) -> list[dict]:
    """The ClusterComponentTypes, each with the valxel-local-aws trait embedded once."""
    out = []
    for t in types:
        traits = t.setdefault("spec", {}).setdefault("traits", [])
        if not any(x.get("name") == TRAIT["name"] for x in traits):
            traits.append(dict(TRAIT))
        meta = t["metadata"]
        out.append({"apiVersion": t["apiVersion"], "kind": t["kind"], "spec": t["spec"],
                    "metadata": {"name": meta["name"], "labels": meta.get("labels") or {},
                                 "annotations": meta.get("annotations") or {}}})
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--context", required=True, help="the cluster's kubectl context, e.g. k3d-valxel")
    p.add_argument("--moto", default="valxel-moto", help="the moto container's name")
    p.add_argument("--network", default="", help="the cluster's Docker network (default: the k3d context's)")
    p.add_argument("--crossplane", action="store_true", help="install Crossplane first")
    p.add_argument("--openchoreo", "--resource-types", dest="openchoreo", action="store_true",
                   help="OpenChoreo is in the cluster: its ClusterResourceTypes, and components' pods pointed at moto")
    a = p.parse_args(argv)
    ctx, network = a.context, a.network or a.context  # k3d's network is named like its context

    ip = moto_address(a.moto, network)
    run(["docker", "exec", "-i", a.moto, "python3", "-"], input=(HERE / "seed.py").read_text())

    if a.crossplane:
        run(["helm", "--kube-context", ctx, "upgrade", "--install", "crossplane", "crossplane",
             "--repo", "https://charts.crossplane.io/stable", "--version", CROSSPLANE_VERSION,
             "--namespace", "crossplane-system", "--create-namespace", "--wait"])

    apply(ctx, load(ROOT / "functions.yaml"))
    providers = [d for d in load(ROOT / "aws" / "provider.yaml") if d["metadata"]["name"] not in SKIP_PROVIDERS]
    apply(ctx, providers)
    for kind in ("functions.pkg.crossplane.io", "providers.pkg.crossplane.io"):
        run(["kubectl", "--context", ctx, "wait", kind, "--all", "--for=condition=Healthy", "--timeout=15m"])

    apply(ctx, (HERE / "moto-service.yaml").read_text().replace("${MOTO_IP}", ip))
    apply(ctx, load(HERE / "provider-config.yaml") + load(HERE / "environment-config.yaml"))

    xrds = [d for f in sorted((ROOT / "apis").glob("*/definition.yaml")) for d in load(f)]
    apply(ctx, xrds)
    run(["kubectl", "--context", ctx, "wait", "compositeresourcedefinitions", "--all",
         "--for=condition=Established", "--timeout=5m"])
    comps = [d for f in sorted((ROOT / "aws").glob("*-composition.yaml"))
             if f.name.removesuffix("-composition.yaml") not in SKIP_COMPOSITIONS for d in load(f)]
    apply(ctx, comps + load(HERE / "workload-identity-composition.yaml"))

    if a.openchoreo:
        apply(ctx, [d for f in sorted((ROOT / "apis").glob("*/resource-type.yaml")) for d in load(f)])
        apply(ctx, load(HERE / "component-trait.yaml"))
        apply(ctx, with_local_aws(json.loads(run(["kubectl", "--context", ctx, "get", "clustercomponenttypes",
                                                  "-o", "json"]))["items"]))
    print(f"AWS catalog installed in {ctx}; AWS calls go to moto at {ip}:5000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
