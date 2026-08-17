"""Push local .env values into Secrets Manager without them entering a transcript.

The values are read from .env by this process and handed straight to the API.
They are never printed, never passed on a command line, and never read back --
per the project's secret-safety rules, nothing here calls get-secret-value or
batch-get-secret-value.

The Lambdas receive these at runtime through CloudFormation dynamic references
(`{{resolve:secretsmanager:...}}`), so the values never appear in a template,
a stack parameter, or a deployment log either.

Run:  python scripts/put_secrets.py
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

SECRET_NAME = "biographer/config"
KEY_ALIAS = "alias/biographer-secrets"

# Only what the deployed Lambdas actually need. AWS credentials are deliberately
# absent -- the execution role supplies those.
KEYS = (
    "DATABASE_URL",
    "CRDB_API_KEY",
    "CRDB_CLUSTER_ID",
    "BIOGRAPHER_ROLE_ARN",
    "BIOGRAPHER_EXTERNAL_ID",
)


def ensure_key(kms) -> str:
    """A dedicated CMK, not the AWS-managed default.

    A dedicated key means access can be revoked and audited independently of
    every other Secrets Manager secret in the account.
    """
    try:
        return kms.describe_key(KeyId=KEY_ALIAS)["KeyMetadata"]["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NotFoundException":
            raise
    key = kms.create_key(
        Description="Encrypts the AWS Biographer application secret",
        KeyUsage="ENCRYPT_DECRYPT",
        Origin="AWS_KMS",
    )["KeyMetadata"]
    kms.create_alias(AliasName=KEY_ALIAS, TargetKeyId=key["KeyId"])
    kms.enable_key_rotation(KeyId=key["KeyId"])
    print(f"created KMS key {KEY_ALIAS} with annual rotation enabled")
    return key["Arn"]


def main() -> int:
    payload = {k.lower(): os.environ.get(k, "") for k in KEYS}
    missing = [k for k, v in payload.items() if not v]
    if missing:
        print(f"missing from .env: {', '.join(missing)}", file=sys.stderr)
        return 1

    region = os.environ.get("AWS_REGION", "us-east-1")
    session = boto3.Session(region_name=region)
    key_arn = ensure_key(session.client("kms"))
    secrets = session.client("secretsmanager")
    body = json.dumps(payload)

    try:
        secrets.create_secret(Name=SECRET_NAME, SecretString=body, KmsKeyId=key_arn,
                              Description="AWS Biographer runtime configuration")
        print(f"created secret {SECRET_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise
        secrets.put_secret_value(SecretId=SECRET_NAME, SecretString=body)
        print(f"updated secret {SECRET_NAME}")

    # Key names only. Values are never echoed.
    print(f"keys stored: {', '.join(sorted(payload))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
