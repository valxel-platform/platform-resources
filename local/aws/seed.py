"""What the local data plane's settings name, made in moto (environment-config.yaml): the KMS key
(alias/valxel-local) and the workloads' permissions boundary. Safe to run again. install.py runs it
inside the moto container (`docker exec -i <moto> python3 - < seed.py`), so nothing is needed on
the machine itself; MOTO_URL overrides where it goes."""

import json
import os

import boto3

URL = os.environ.get("MOTO_URL", "http://localhost:5000")
kw = {"endpoint_url": URL, "region_name": "us-east-1", "aws_access_key_id": "local", "aws_secret_access_key": "local"}

kms = boto3.client("kms", **kw)
if not any(a["AliasName"] == "alias/valxel-local" for a in kms.list_aliases()["Aliases"]):
    key = kms.create_key(Description="valxel local data plane key")["KeyMetadata"]["KeyId"]
    kms.create_alias(AliasName="alias/valxel-local", TargetKeyId=key)

iam = boto3.client("iam", **kw)
boundary = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
try:
    iam.create_policy(PolicyName="valxel-workload-boundary", PolicyDocument=json.dumps(boundary),
                      Description="local stand-in for the platform's workload permissions boundary")
except iam.exceptions.EntityAlreadyExistsException:
    pass
print("seeded", URL)
