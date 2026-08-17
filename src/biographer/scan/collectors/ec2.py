"""EC2 and the compute-adjacent resources. Reference implementation.

Every other collector module follows the shape of this one.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3

from ...aws import account_id_of, client
from ..model import Resource, collector, jsonable, paginate, tag_list_to_dict


@collector("ec2", "instance")
def instances(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for reservation in paginate(ec2, "describe_instances", "Reservations"):
        for inst in reservation.get("Instances", []):
            tags = tag_list_to_dict(inst.get("Tags"))
            yield Resource(
                arn=f"arn:aws:ec2:{region}:{account}:instance/{inst['InstanceId']}",
                region=region,
                service="ec2",
                resource_type="instance",
                name=tags.get("Name") or inst["InstanceId"],
                tags=tags,
                config=jsonable(
                    {
                        "InstanceId": inst["InstanceId"],
                        "InstanceType": inst.get("InstanceType"),
                        "State": inst.get("State", {}).get("Name"),
                        "LaunchTime": inst.get("LaunchTime"),
                        "VpcId": inst.get("VpcId"),
                        "SubnetId": inst.get("SubnetId"),
                        "ImageId": inst.get("ImageId"),
                        "KeyName": inst.get("KeyName"),
                        "IamInstanceProfile": inst.get("IamInstanceProfile", {}).get("Arn"),
                        "SecurityGroups": [
                            g.get("GroupId") for g in inst.get("SecurityGroups", [])
                        ],
                        "PrivateIpAddress": inst.get("PrivateIpAddress"),
                        "PublicIpAddress": inst.get("PublicIpAddress"),
                    }
                ),
            )


@collector("ec2", "volume")
def volumes(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for vol in paginate(ec2, "describe_volumes", "Volumes"):
        tags = tag_list_to_dict(vol.get("Tags"))
        attachments = vol.get("Attachments", [])
        yield Resource(
            arn=f"arn:aws:ec2:{region}:{account}:volume/{vol['VolumeId']}",
            region=region,
            service="ec2",
            resource_type="volume",
            name=tags.get("Name") or vol["VolumeId"],
            tags=tags,
            config=jsonable(
                {
                    "VolumeId": vol["VolumeId"],
                    "Size": vol.get("Size"),
                    "VolumeType": vol.get("VolumeType"),
                    "State": vol.get("State"),
                    "CreateTime": vol.get("CreateTime"),
                    "Encrypted": vol.get("Encrypted"),
                    # The waste question turns on this being empty.
                    "AttachedTo": [a.get("InstanceId") for a in attachments],
                }
            ),
        )


@collector("ec2", "address")
def elastic_ips(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for addr in ec2.describe_addresses().get("Addresses", []):
        tags = tag_list_to_dict(addr.get("Tags"))
        alloc = addr.get("AllocationId", addr.get("PublicIp"))
        yield Resource(
            arn=f"arn:aws:ec2:{region}:{account}:elastic-ip/{alloc}",
            region=region,
            service="ec2",
            resource_type="address",
            name=tags.get("Name") or addr.get("PublicIp"),
            tags=tags,
            config=jsonable(
                {
                    "PublicIp": addr.get("PublicIp"),
                    "AllocationId": addr.get("AllocationId"),
                    # Unattached EIPs bill hourly; this is the flagship waste case.
                    "AssociationId": addr.get("AssociationId"),
                    "InstanceId": addr.get("InstanceId"),
                    "NetworkInterfaceId": addr.get("NetworkInterfaceId"),
                }
            ),
        )


@collector("ec2", "security-group")
def security_groups(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for group in paginate(ec2, "describe_security_groups", "SecurityGroups"):
        tags = tag_list_to_dict(group.get("Tags"))
        yield Resource(
            arn=f"arn:aws:ec2:{region}:{account}:security-group/{group['GroupId']}",
            region=region,
            service="ec2",
            resource_type="security-group",
            name=group.get("GroupName"),
            tags=tags,
            config=jsonable(
                {
                    "GroupId": group["GroupId"],
                    "GroupName": group.get("GroupName"),
                    "VpcId": group.get("VpcId"),
                    "Description": group.get("Description"),
                    "IpPermissions": group.get("IpPermissions", []),
                    "IpPermissionsEgress": group.get("IpPermissionsEgress", []),
                }
            ),
        )


@collector("ec2", "vpc")
def vpcs(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for vpc in paginate(ec2, "describe_vpcs", "Vpcs"):
        tags = tag_list_to_dict(vpc.get("Tags"))
        yield Resource(
            arn=f"arn:aws:ec2:{region}:{account}:vpc/{vpc['VpcId']}",
            region=region,
            service="ec2",
            resource_type="vpc",
            name=tags.get("Name") or vpc["VpcId"],
            tags=tags,
            config=jsonable(
                {
                    "VpcId": vpc["VpcId"],
                    "CidrBlock": vpc.get("CidrBlock"),
                    "IsDefault": vpc.get("IsDefault"),
                    "State": vpc.get("State"),
                }
            ),
        )


@collector("ec2", "subnet")
def subnets(session: boto3.Session, region: str) -> Iterator[Resource]:
    ec2 = client(session, "ec2", region)
    account = account_id_of(session)
    for subnet in paginate(ec2, "describe_subnets", "Subnets"):
        tags = tag_list_to_dict(subnet.get("Tags"))
        yield Resource(
            arn=subnet.get("SubnetArn")
            or f"arn:aws:ec2:{region}:{account}:subnet/{subnet['SubnetId']}",
            region=region,
            service="ec2",
            resource_type="subnet",
            name=tags.get("Name") or subnet["SubnetId"],
            tags=tags,
            config=jsonable(
                {
                    "SubnetId": subnet["SubnetId"],
                    "VpcId": subnet.get("VpcId"),
                    "CidrBlock": subnet.get("CidrBlock"),
                    "AvailabilityZone": subnet.get("AvailabilityZone"),
                    "MapPublicIpOnLaunch": subnet.get("MapPublicIpOnLaunch"),
                }
            ),
        )
