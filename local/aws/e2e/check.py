"""What the e2e resources built in moto, checked through moto's own API. Run inside the moto
container by check.sh (python3 - < check.py). Exits non-zero, saying what's missing."""

import sys

import boto3

kw = {"endpoint_url": "http://localhost:5000", "region_name": "us-east-1",
      "aws_access_key_id": "local", "aws_secret_access_key": "local"}
c = lambda s: boto3.client(s, **kw)  # noqa: E731
missing = []

tables = c("dynamodb").list_tables()["TableNames"]
if not any(t.startswith("vx-") for t in tables):
    missing.append(f"no vx- DynamoDB table (tables: {tables})")
buckets = [b["Name"] for b in c("s3").list_buckets()["Buckets"]]
if not any(b.startswith("vx-") for b in buckets):
    missing.append(f"no vx- S3 bucket (buckets: {buckets})")
queues = c("sqs").list_queues().get("QueueUrls", [])
if not any("/vx-" in q and q.endswith(".fifo") for q in queues):
    missing.append(f"no vx- FIFO queue (queues: {queues})")
topics = [t["TopicArn"] for t in c("sns").list_topics()["Topics"]]
if not any(":vx-" in t for t in topics):
    missing.append(f"no vx- SNS topic (topics: {topics})")
streams = c("kinesis").list_streams()["StreamNames"]
if not any(s.startswith("vx-") for s in streams):
    missing.append(f"no vx- Kinesis stream (streams: {streams})")

iam = c("iam")
roles = [r["RoleName"] for r in iam.list_roles()["Roles"] if r["RoleName"].startswith("vx-")]
if len(roles) < 3:
    missing.append(f"want the three components' roles, got {roles}")
for r in roles:
    if r.startswith("vx-agtech-field-api-") and not iam.list_role_policies(RoleName=r)["PolicyNames"]:
        missing.append(f"{r} has no policies: the resources' Compositions attach theirs to it")
alarms = [a["AlarmName"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"]]
if not alarms:
    missing.append("no CloudWatch alarms")

print(f"tables={tables} buckets={buckets} queues={len(queues)} topics={len(topics)} streams={streams} "
      f"roles={roles} alarms={len(alarms)}")
if missing:
    sys.exit("\n".join(missing))
print("moto has everything the e2e resources asked for")
