"""Databases, container clusters, load balancers -- the stateful tier.

Same shape as `ec2.py`: one collector per resource type, regional scope, and
nothing here mutates AWS.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
from botocore.exceptions import ClientError

from ..model import Resource, collector, jsonable, paginate, tag_list_to_dict


@collector("rds", "db-instance")
def db_instances(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("rds", region_name=region)
    for inst in paginate(client, "describe_db_instances", "DBInstances"):
        arn = inst.get("DBInstanceArn")
        if not arn:
            continue
        # RDS hands back the tags inline, so no second call per instance.
        tags = tag_list_to_dict(inst.get("TagList"))
        yield Resource(
            arn=arn,
            region=region,
            service="rds",
            resource_type="db-instance",
            name=tags.get("Name") or inst.get("DBInstanceIdentifier"),
            tags=tags,
            config=jsonable(
                {
                    "DBInstanceIdentifier": inst.get("DBInstanceIdentifier"),
                    "DBInstanceArn": arn,
                    "Engine": inst.get("Engine"),
                    "EngineVersion": inst.get("EngineVersion"),
                    "DBInstanceClass": inst.get("DBInstanceClass"),
                    "AllocatedStorage": inst.get("AllocatedStorage"),
                    "InstanceCreateTime": inst.get("InstanceCreateTime"),
                    "MultiAZ": inst.get("MultiAZ"),
                    # The exposure question turns on these two.
                    "PubliclyAccessible": inst.get("PubliclyAccessible"),
                    "StorageEncrypted": inst.get("StorageEncrypted"),
                    "DBInstanceStatus": inst.get("DBInstanceStatus"),
                }
            ),
        )


@collector("rds", "db-cluster")
def db_clusters(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("rds", region_name=region)
    for cluster in paginate(client, "describe_db_clusters", "DBClusters"):
        arn = cluster.get("DBClusterArn")
        if not arn:
            continue
        tags = tag_list_to_dict(cluster.get("TagList"))
        yield Resource(
            arn=arn,
            region=region,
            service="rds",
            resource_type="db-cluster",
            name=tags.get("Name") or cluster.get("DBClusterIdentifier"),
            tags=tags,
            config=jsonable(
                {
                    "DBClusterIdentifier": cluster.get("DBClusterIdentifier"),
                    "DBClusterArn": arn,
                    "Engine": cluster.get("Engine"),
                    "EngineVersion": cluster.get("EngineVersion"),
                    "EngineMode": cluster.get("EngineMode"),
                    "Status": cluster.get("Status"),
                    "ClusterCreateTime": cluster.get("ClusterCreateTime"),
                    "MultiAZ": cluster.get("MultiAZ"),
                    "StorageEncrypted": cluster.get("StorageEncrypted"),
                }
            ),
        )


@collector("dynamodb", "table")
def tables(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("dynamodb", region_name=region)
    for name in paginate(client, "list_tables", "TableNames"):
        # list_tables gives names only; the detail is the point, so follow up.
        # One denied or mid-delete table must not cost us the rest of the list.
        try:
            table = client.describe_table(TableName=name).get("Table", {})
        except ClientError:
            continue
        arn = table.get("TableArn")
        if not arn:
            continue
        try:
            tags = tag_list_to_dict(
                list(paginate(client, "list_tags_of_resource", "Tags", ResourceArn=arn))
            )
        except ClientError:
            tags = {}
        yield Resource(
            arn=arn,
            region=region,
            service="dynamodb",
            resource_type="table",
            name=table.get("TableName") or name,
            tags=tags,
            config=jsonable(
                {
                    "TableName": table.get("TableName"),
                    "TableArn": arn,
                    "TableStatus": table.get("TableStatus"),
                    "CreationDateTime": table.get("CreationDateTime"),
                    # Both are best-effort figures AWS refreshes every ~6h.
                    "ItemCount": table.get("ItemCount"),
                    "TableSizeBytes": table.get("TableSizeBytes"),
                    "BillingMode": table.get("BillingModeSummary", {}).get("BillingMode"),
                    "ProvisionedThroughput": table.get("ProvisionedThroughput"),
                    "GlobalSecondaryIndexes": [
                        gsi.get("IndexName") for gsi in table.get("GlobalSecondaryIndexes", [])
                    ],
                }
            ),
        )


@collector("ecs", "cluster")
def ecs_clusters(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("ecs", region_name=region)
    arns = list(paginate(client, "list_clusters", "clusterArns"))
    # describe_clusters takes at most 100 identifiers per call.
    for start in range(0, len(arns), 100):
        batch = arns[start : start + 100]
        for cluster in client.describe_clusters(clusters=batch).get("clusters", []):
            arn = cluster.get("clusterArn")
            if not arn:
                continue
            yield Resource(
                arn=arn,
                region=region,
                service="ecs",
                resource_type="cluster",
                name=cluster.get("clusterName"),
                config=jsonable(
                    {
                        "clusterName": cluster.get("clusterName"),
                        "clusterArn": arn,
                        "status": cluster.get("status"),
                        # An empty cluster still shows up on the bill via its ASG.
                        "runningTasksCount": cluster.get("runningTasksCount"),
                        "pendingTasksCount": cluster.get("pendingTasksCount"),
                        "activeServicesCount": cluster.get("activeServicesCount"),
                        "registeredContainerInstancesCount": cluster.get(
                            "registeredContainerInstancesCount"
                        ),
                    }
                ),
            )


@collector("elasticloadbalancing", "load-balancer")
def load_balancers(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("elbv2", region_name=region)
    for elb in paginate(client, "describe_load_balancers", "LoadBalancers"):
        arn = elb.get("LoadBalancerArn")
        if not arn:
            continue
        yield Resource(
            arn=arn,
            region=region,
            service="elasticloadbalancing",
            resource_type="load-balancer",
            name=elb.get("LoadBalancerName"),
            config=jsonable(
                {
                    "LoadBalancerName": elb.get("LoadBalancerName"),
                    "LoadBalancerArn": arn,
                    "Type": elb.get("Type"),
                    "Scheme": elb.get("Scheme"),
                    "State": elb.get("State", {}).get("Code"),
                    "CreatedTime": elb.get("CreatedTime"),
                    "VpcId": elb.get("VpcId"),
                    "AvailabilityZones": [
                        az.get("SubnetId") for az in elb.get("AvailabilityZones", [])
                    ],
                    "DNSName": elb.get("DNSName"),
                }
            ),
        )


@collector("lightsail", "instance")
def lightsail_instances(session: boto3.Session, region: str) -> Iterator[Resource]:
    # Lightsail only exists in a subset of regions and raises in the rest; that
    # is the runner's problem, not ours.
    client = session.client("lightsail", region_name=region)
    for inst in paginate(client, "get_instances", "instances"):
        arn = inst.get("arn")
        if not arn:
            continue
        # Lightsail spells its tags lowercase; tag_list_to_dict copes.
        tags = tag_list_to_dict(inst.get("tags"))
        yield Resource(
            arn=arn,
            region=region,
            service="lightsail",
            resource_type="instance",
            name=tags.get("Name") or inst.get("name"),
            tags=tags,
            config=jsonable(
                {
                    "name": inst.get("name"),
                    "arn": arn,
                    "blueprintId": inst.get("blueprintId"),
                    "bundleId": inst.get("bundleId"),
                    "createdAt": inst.get("createdAt"),
                    "state": inst.get("state", {}).get("name"),
                    "publicIpAddress": inst.get("publicIpAddress"),
                    "privateIpAddress": inst.get("privateIpAddress"),
                }
            ),
        )
