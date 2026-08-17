"""Serverless and event-plumbing resources: Lambda, CloudWatch Logs, SNS, SQS,
EventBridge and Step Functions.

Same shape as `ec2.py`: read-only calls, one `Resource` per thing, never raise
for a per-item denial.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
from botocore.exceptions import ClientError

from ..model import Resource, collector, jsonable, paginate, tag_list_to_dict


@collector("lambda", "function")
def functions(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("lambda", region_name=region)
    for fn in paginate(client, "list_functions", "Functions"):
        arn = fn["FunctionArn"]
        try:
            tags = tag_list_to_dict(client.list_tags(Resource=arn).get("Tags"))
        except ClientError:
            # lambda:ListTags is a separate permission from ListFunctions.
            tags = {}
        yield Resource(
            arn=arn,
            region=region,
            service="lambda",
            resource_type="function",
            name=fn.get("FunctionName"),
            tags=tags,
            config=jsonable(
                {
                    "FunctionName": fn.get("FunctionName"),
                    "FunctionArn": arn,
                    "Runtime": fn.get("Runtime"),
                    "Role": fn.get("Role"),
                    "MemorySize": fn.get("MemorySize"),
                    "Timeout": fn.get("Timeout"),
                    "LastModified": fn.get("LastModified"),
                    "CodeSize": fn.get("CodeSize"),
                    "Handler": fn.get("Handler"),
                    # Names only. Values routinely hold credentials, and this
                    # blob lands in a database and in front of an LLM.
                    "EnvironmentVariableNames": sorted(
                        fn.get("Environment", {}).get("Variables", {})
                    ),
                    "VpcConfig": fn.get("VpcConfig"),
                    "Architectures": fn.get("Architectures"),
                }
            ),
        )


@collector("logs", "log-group")
def log_groups(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("logs", region_name=region)
    for group in paginate(client, "describe_log_groups", "logGroups"):
        yield Resource(
            arn=group["arn"].removesuffix(":*"),
            region=region,
            service="logs",
            resource_type="log-group",
            name=group.get("logGroupName"),
            config=jsonable(
                {
                    "logGroupName": group.get("logGroupName"),
                    "arn": group.get("arn"),
                    "creationTime": group.get("creationTime"),
                    # Absent means "never expires", which is the waste finding.
                    # Record the null rather than substituting a default.
                    "retentionInDays": group.get("retentionInDays"),
                    "storedBytes": group.get("storedBytes"),
                    "metricFilterCount": group.get("metricFilterCount"),
                }
            ),
        )


@collector("sns", "topic")
def topics(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("sns", region_name=region)
    for topic in paginate(client, "list_topics", "Topics"):
        arn = topic["TopicArn"]
        try:
            attrs = client.get_topic_attributes(TopicArn=arn).get("Attributes", {})
        except ClientError:
            attrs = {}
        yield Resource(
            arn=arn,
            region=region,
            service="sns",
            resource_type="topic",
            # list_topics returns only ARNs; the name is the last segment.
            name=arn.rsplit(":", 1)[-1],
            config=jsonable(
                {
                    "TopicArn": arn,
                    "DisplayName": attrs.get("DisplayName"),
                    # Zero confirmed subscriptions is a topic nobody listens to.
                    "SubscriptionsConfirmed": attrs.get("SubscriptionsConfirmed"),
                    "SubscriptionsPending": attrs.get("SubscriptionsPending"),
                }
            ),
        )


@collector("sqs", "queue")
def queues(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("sqs", region_name=region)
    # An account with no queues omits QueueUrls entirely; paginate() already
    # treats a missing key as empty.
    for url in paginate(client, "list_queues", "QueueUrls"):
        try:
            attrs = client.get_queue_attributes(
                QueueUrl=url, AttributeNames=["All"]
            ).get("Attributes", {})
        except ClientError:
            attrs = {}
        arn = attrs.get("QueueArn")
        if not arn:
            # No ARN, no identity -- skip rather than emit a blank one.
            continue
        yield Resource(
            arn=arn,
            region=region,
            service="sqs",
            resource_type="queue",
            name=url.rsplit("/", 1)[-1],
            config=jsonable(
                {
                    "QueueArn": arn,
                    "ApproximateNumberOfMessages": attrs.get("ApproximateNumberOfMessages"),
                    "CreatedTimestamp": attrs.get("CreatedTimestamp"),
                    "LastModifiedTimestamp": attrs.get("LastModifiedTimestamp"),
                    "VisibilityTimeout": attrs.get("VisibilityTimeout"),
                    "RedrivePolicy": attrs.get("RedrivePolicy"),
                }
            ),
        )


@collector("events", "rule")
def event_rules(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("events", region_name=region)
    for rule in paginate(client, "list_rules", "Rules"):
        yield Resource(
            arn=rule["Arn"],
            region=region,
            service="events",
            resource_type="rule",
            name=rule.get("Name"),
            config=jsonable(
                {
                    "Name": rule.get("Name"),
                    "Arn": rule.get("Arn"),
                    "ScheduleExpression": rule.get("ScheduleExpression"),
                    # DISABLED rules are dead plumbing nobody cleaned up.
                    "State": rule.get("State"),
                    "EventPattern": rule.get("EventPattern"),
                    "Description": rule.get("Description"),
                }
            ),
        )


@collector("states", "state-machine")
def state_machines(session: boto3.Session, region: str) -> Iterator[Resource]:
    client = session.client("stepfunctions", region_name=region)
    for machine in paginate(client, "list_state_machines", "stateMachines"):
        yield Resource(
            arn=machine["stateMachineArn"],
            region=region,
            service="states",
            resource_type="state-machine",
            name=machine.get("name"),
            config=jsonable(
                {
                    "name": machine.get("name"),
                    "stateMachineArn": machine.get("stateMachineArn"),
                    "type": machine.get("type"),
                    "creationDate": machine.get("creationDate"),
                }
            ),
        )
