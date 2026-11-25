"""Writes the neutral catalog types' AWS side from the version 1 AWS types (sdlc-governance
platform/catalog.yaml, agentic-sdlc ADR 0010).

Version 2 of the catalog names types for what they are (key-value-table, queue...). On AWS each one
is built exactly as its version 1 type (dynamodb-table, sqs-queue...), which stays for the resources
already made under it: an OpenChoreo Resource never changes type, since that would replace it and its
data. So for each pair this writes, from the AWS type's files:

    apis/<type>/definition.yaml      the neutral XRD (group platform.valxel.dev), status by neutral names
    apis/<type>/resource-type.yaml   the ClusterResourceType, its outputs as the catalog names them
    aws/<type>-composition.yaml      the same Composition, for the neutral XRD
    aws/render/<type>/               the render fixtures

Run `python3 scripts/neutral.py` after changing a version 1 AWS type; tests/test_module.py fails
when a written file differs from what this would write. The Google Cloud side (gcp/) is written
by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUP = "platform.valxel.dev"
ACTIONS = '{type: string, pattern: "^[a-z0-9-]+:[A-Za-z*]+$"}'
ACTIONS_ANY = '{type: string, pattern: "^([a-z0-9-]+:[A-Za-z*]+|roles/[A-Za-z0-9_.]+|(role|cosmos):[A-Za-z -]+)$"}'

# old type: (new type, old kind, new kind, old plural, new plural, {old status: [new status...]},
#            {old output: new output})
PAIRS = {
    "dynamodb-table": ("key-value-table", "DynamoTable", "KeyValueTable", "dynamotables", "keyvaluetables",
                       {"tableName": ["table"], "arn": ["id"]}, {"table": "table", "arn": "id"}),
    "kinesis-stream": ("event-stream", "KinesisStream", "EventStream", "kinesisstreams", "eventstreams",
                       {"streamName": ["stream"], "arn": ["id"]}, {"stream": "stream", "arn": "id"}),
    "sqs-queue": ("queue", "SqsQueue", "Queue", "sqsqueues", "queues",
                  {"url": ["sendTo", "receiveFrom"], "arn": ["id"], "deadLetterArn": ["deadLetterId"]},
                  {"url": "send_to", "arn": "id"}),
    "s3-bucket": ("object-bucket", "S3Bucket", "ObjectBucket", "s3buckets", "objectbuckets",
                  {"bucketName": ["bucket"], "arn": ["id"], "endpoint": ["endpoint"]},
                  {"bucket": "bucket", "arn": "id", "endpoint": "endpoint"}),
    "sns-topic": ("topic", "SnsTopic", "Topic", "snstopics", "topics", {"arn": ["topic"]}, {"arn": "topic"}),
}
HEADER = "# Written by scripts/neutral.py from {src}: change that file and run the script, not this one.\n"


def _status_fields(text: str, mapping: dict[str, list[str]]) -> str:
    """The XRD's status properties renamed (`name:` lines under status.properties)."""
    head, _, status = text.partition("            status:\n")
    for old, new in mapping.items():
        block = f"                {old}:\n                  type: string\n"
        assert block in status, f"status {old} not in the XRD"
        status = status.replace(block, "".join(f"                {n}:\n                  type: string\n" for n in new))
    return head + "            status:\n" + status


def xrd(old: str) -> str:
    new, ok, nk, op, np, status, _ = PAIRS[old]
    t = (ROOT / f"apis/{old}/definition.yaml").read_text()
    t = t.replace(f"name: {op}.aws.platform.valxel.dev", f"name: {np}.{GROUP}")
    t = t.replace("group: aws.platform.valxel.dev", f"group: {GROUP}")
    t = t.replace(f"kind: {ok}\n", f"kind: {nk}\n").replace(f"plural: {op}\n", f"plural: {np}\n")
    t = t.replace(f"aws/{old}-composition.yaml builds it", f"aws/, gcp/ and azure/{new}-composition.yaml build it")
    t = t.replace(f"The {ok} API:", f"The {nk} API (the {new} type, built as {old} on AWS):")
    t = t.replace(f"The {old} ClusterResourceType", f"The {new} ClusterResourceType")
    t = t.replace(ACTIONS, ACTIONS_ANY).replace("IAM actions each may take", "access each is given (IAM actions on AWS, roles on Google Cloud and Azure)")
    return HEADER.format(src=f"apis/{old}/definition.yaml") + _status_fields(t, status)


def resource_type(old: str) -> str:
    new, ok, nk, _, _, status, outputs = PAIRS[old]
    t = (ROOT / f"apis/{old}/resource-type.yaml").read_text()
    t = t.replace(f"  name: {old}\n", f"  name: {new}\n")
    t = t.replace("apiVersion: aws.platform.valxel.dev/v1alpha1", f"apiVersion: {GROUP}/v1alpha1")
    t = t.replace(f"kind: {ok}\n", f"kind: {nk}\n")
    t = t.replace(ACTIONS, ACTIONS_ANY).replace("IAM actions each may take", "access each is given (IAM actions on AWS, roles on Google Cloud and Azure)")
    # Outputs and readiness by the neutral status names; a queue sends to and receives from its URL.
    out_head, _, rest = t.partition("  outputs:\n")
    outs, _, rest = rest.partition("\n  resources:\n")
    lines = []
    for old_out, new_out in outputs.items():
        old_field = {"table": "tableName", "stream": "streamName", "bucket": "bucketName"}.get(old_out, old_out)
        lines.append(f"    - name: {new_out}\n      value: ${{applied.instance.status.{status[old_field][0]}}}")
        if old == "sqs-queue" and old_out == "url":
            lines.append("    - name: receive_from\n      value: ${applied.instance.status.receiveFrom}")
    for old_field, new_fields in status.items():
        rest = rest.replace(f"has(applied.instance.status.{old_field})", f"has(applied.instance.status.{new_fields[0]})")
    return HEADER.format(src=f"apis/{old}/resource-type.yaml") + out_head + "  outputs:\n" + "\n".join(lines) + "\n\n  resources:\n" + rest


def composition(old: str) -> str:
    new, ok, nk, _, _, status, _ = PAIRS[old]
    t = (ROOT / f"aws/{old}-composition.yaml").read_text()
    t = t.replace(f"  name: {old}-aws\n", f"  name: {new}-aws\n")
    t = t.replace("    apiVersion: aws.platform.valxel.dev/v1alpha1\n", f"    apiVersion: {GROUP}/v1alpha1\n", 1)
    t = t.replace(f"    kind: {ok}\n", f"    kind: {nk}\n", 1)
    head, sep, tail = t.rpartition(f"            apiVersion: aws.platform.valxel.dev/v1alpha1\n            kind: {ok}\n            status:\n")
    assert sep, f"{old}: no status block"
    body, end = tail.split("            {{- end }}", 1)
    for old_field, new_fields in status.items():
        line = next(x for x in body.splitlines(keepends=True) if x.strip().startswith(f"{old_field}:"))
        value = line.split(":", 1)[1]
        body = body.replace(line, "".join(f"              {n}:{value}" for n in new_fields))
    return (HEADER.format(src=f"aws/{old}-composition.yaml") + head
            + f"            apiVersion: {GROUP}/v1alpha1\n            kind: {nk}\n            status:\n" + body
            + "            {{- end }}" + end)


def fixture(old: str, name: str) -> str:
    new, ok, nk, _, _, _, _ = PAIRS[old]
    t = (ROOT / f"aws/render/{old}/{name}").read_text()
    t = t.replace("apiVersion: aws.platform.valxel.dev/v1alpha1\nkind: " + ok, f"apiVersion: {GROUP}/v1alpha1\nkind: {nk}")
    return HEADER.format(src=f"aws/render/{old}/{name}") + t


def files() -> dict[Path, str]:
    out = {}
    for old, (new, *_) in PAIRS.items():
        out[ROOT / f"apis/{new}/definition.yaml"] = xrd(old)
        out[ROOT / f"apis/{new}/resource-type.yaml"] = resource_type(old)
        out[ROOT / f"aws/{new}-composition.yaml"] = composition(old)
        for name in ("xr.yaml", "observed.yaml"):
            out[ROOT / f"aws/render/{new}/{name}"] = fixture(old, name)
    return out


def main() -> int:
    for path, text in files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
