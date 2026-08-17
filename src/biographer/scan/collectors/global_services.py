"""Global (non-regional) services: S3, IAM, CloudFront, Route53.

The runner hands these collectors the `GLOBAL` sentinel as `region`, which is
what the `Resource` rows record. The API calls themselves still need a real
endpoint, so they go to us-east-1 -- the home region for IAM, CloudFront and
Route53. S3 buckets are listed globally but each lives somewhere, so the real
location is recorded in config.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
from botocore.exceptions import ClientError

from ..model import Resource, collector, jsonable, paginate, tag_list_to_dict

# Where the control plane for these services actually answers.
HOME = "us-east-1"


@collector("s3", "bucket", scope="global")
def buckets(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("s3", region_name=HOME)
    # Bucket subresource calls (tagging, versioning, ...) must go to the
    # bucket's own region or they come back as a 301 PermanentRedirect, so
    # keep one client per region rather than rebuilding it per bucket.
    regional: dict[str, Any] = {HOME: client}
    for bucket in paginate(client, "list_buckets", "Buckets"):
        name = bucket["Name"]

        location = None
        try:
            # us-east-1 is reported as a null LocationConstraint; "EU" is the
            # legacy spelling of eu-west-1.
            raw = client.get_bucket_location(Bucket=name).get("LocationConstraint")
            location = {None: HOME, "": HOME, "EU": "eu-west-1"}.get(raw, raw)
        except ClientError:
            pass

        home = location or HOME
        if home not in regional:
            regional[home] = session.client("s3", region_name=home)
        bucket_client = regional[home]

        # Each of these is separately deniable and separately absent; a missing
        # one must not cost us the bucket row itself.
        tags: dict[str, str] = {}
        try:
            tags = tag_list_to_dict(bucket_client.get_bucket_tagging(Bucket=name).get("TagSet"))
        except ClientError:
            pass

        versioning = None
        try:
            versioning = bucket_client.get_bucket_versioning(Bucket=name).get("Status")
        except ClientError:
            pass

        encryption = None
        try:
            encryption = (
                bucket_client.get_bucket_encryption(Bucket=name)
                .get("ServerSideEncryptionConfiguration", {})
                .get("Rules")
            )
        except ClientError:
            pass

        public_access_block = None
        try:
            public_access_block = bucket_client.get_public_access_block(
                Bucket=name
            ).get("PublicAccessBlockConfiguration")
        except ClientError:
            pass

        yield Resource(
            arn=f"arn:aws:s3:::{name}",
            region=region,
            service="s3",
            resource_type="bucket",
            name=name,
            tags=tags,
            config=jsonable(
                {
                    "Name": name,
                    "CreationDate": bucket.get("CreationDate"),
                    # Where the bucket really is, as opposed to the sentinel.
                    "Location": location,
                    "Versioning": versioning,
                    "Encryption": encryption,
                    # None means "never configured", which is the exposure case.
                    "PublicAccessBlock": public_access_block,
                }
            ),
        )


@collector("iam", "role", scope="global")
def roles(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("iam", region_name=HOME)
    for role in paginate(client, "list_roles", "Roles"):
        yield Resource(
            arn=role["Arn"],
            region=region,
            service="iam",
            resource_type="role",
            name=role["RoleName"],
            # ListRoles does not return tags; normalise anyway in case it starts.
            tags=tag_list_to_dict(role.get("Tags")),
            config=jsonable(
                {
                    "RoleName": role["RoleName"],
                    "Arn": role["Arn"],
                    "CreateDate": role.get("CreateDate"),
                    "Description": role.get("Description"),
                    "MaxSessionDuration": role.get("MaxSessionDuration"),
                    # Who can assume this role -- the whole blast-radius question.
                    "AssumeRolePolicyDocument": role.get("AssumeRolePolicyDocument"),
                    "Path": role.get("Path"),
                }
            ),
        )


@collector("iam", "user", scope="global")
def users(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("iam", region_name=HOME)
    for user in paginate(client, "list_users", "Users"):
        yield Resource(
            arn=user["Arn"],
            region=region,
            service="iam",
            resource_type="user",
            name=user["UserName"],
            tags=tag_list_to_dict(user.get("Tags")),
            config=jsonable(
                {
                    "UserName": user["UserName"],
                    "Arn": user["Arn"],
                    "CreateDate": user.get("CreateDate"),
                    # Absent means the console password was never used.
                    "PasswordLastUsed": user.get("PasswordLastUsed"),
                    "Path": user.get("Path"),
                }
            ),
        )


@collector("iam", "policy", scope="global")
def policies(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("iam", region_name=HOME)
    # Scope='Local' is customer-managed only; the AWS-managed set is a thousand
    # rows of noise that belong to nobody's account.
    for policy in paginate(client, "list_policies", "Policies", Scope="Local"):
        yield Resource(
            arn=policy["Arn"],
            region=region,
            service="iam",
            resource_type="policy",
            name=policy["PolicyName"],
            tags=tag_list_to_dict(policy.get("Tags")),
            config=jsonable(
                {
                    "PolicyName": policy["PolicyName"],
                    "Arn": policy["Arn"],
                    # Zero attachments is the dead-policy case.
                    "AttachmentCount": policy.get("AttachmentCount"),
                    "CreateDate": policy.get("CreateDate"),
                    "UpdateDate": policy.get("UpdateDate"),
                    "DefaultVersionId": policy.get("DefaultVersionId"),
                }
            ),
        )


@collector("cloudfront", "distribution", scope="global")
def distributions(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("cloudfront", region_name=HOME)
    # The items hang two levels down under DistributionList/Items, and on an
    # account with no distributions DistributionList carries no Items at all,
    # so `paginate` (which reads one top-level key) cannot express this.
    for page in client.get_paginator("list_distributions").paginate():
        for dist in page.get("DistributionList", {}).get("Items") or []:
            yield Resource(
                arn=dist["ARN"],
                region=region,
                service="cloudfront",
                resource_type="distribution",
                name=dist.get("DomainName") or dist["Id"],
                tags={},
                config=jsonable(
                    {
                        "Id": dist["Id"],
                        "ARN": dist["ARN"],
                        "DomainName": dist.get("DomainName"),
                        "Enabled": dist.get("Enabled"),
                        "Status": dist.get("Status"),
                        "Aliases": dist.get("Aliases", {}).get("Items", []),
                        "Origins": [
                            o.get("DomainName")
                            for o in dist.get("Origins", {}).get("Items", [])
                        ],
                    }
                ),
            )


@collector("route53", "hosted-zone", scope="global")
def hosted_zones(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("route53", region_name=HOME)
    for zone in paginate(client, "list_hosted_zones", "HostedZones"):
        # Id comes back as "/hostedzone/Z123"; the ARN wants the bare id.
        zone_id = zone["Id"].rsplit("/", 1)[-1]
        yield Resource(
            arn=f"arn:aws:route53:::hostedzone/{zone_id}",
            region=region,
            service="route53",
            resource_type="hosted-zone",
            name=zone.get("Name") or zone_id,
            tags={},
            config=jsonable(
                {
                    "Id": zone["Id"],
                    "Name": zone.get("Name"),
                    "PrivateZone": zone.get("Config", {}).get("PrivateZone"),
                    "ResourceRecordSetCount": zone.get("ResourceRecordSetCount"),
                }
            ),
        )
