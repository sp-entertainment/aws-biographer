"""CDK app: chat Lambda behind a Function URL, plus the scheduled manage pass.

ADR-0003 explains the shape -- one Lambda with a Function URL and no API
Gateway, one scheduled Lambda, and nothing else that has to stay alive through
judging.

Secrets reach the functions as CloudFormation dynamic references, so their
values never appear in this file, in the synthesised template, in a stack
parameter, or in a deployment log. `scripts/put_secrets.py` puts them in
Secrets Manager under a dedicated KMS key; nothing here or there ever reads
them back.
"""

from __future__ import annotations

import os
import pathlib

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "build" / "biographer.zip"
SECRET_NAME = "biographer/config"
ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT", "")
REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")


def secret_ref(key: str) -> str:
    """A CloudFormation dynamic reference, resolved at deploy time by AWS."""
    return f"{{{{resolve:secretsmanager:{SECRET_NAME}:SecretString:{key}}}}}"


class BiographerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not ARTIFACT.exists():
            raise FileNotFoundError(
                f"{ARTIFACT} missing -- run `python scripts/build_lambda.py` first"
            )
        code = lambda_.Code.from_asset(str(ARTIFACT))

        # Explicit log groups: logRetention is deprecated, and it also
        # provisions a custom resource Lambda purely to set a retention
        # value that belongs on the log group itself.
        chat_logs = logs.LogGroup(
            self, "ChatLogs", retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY)
        manage_logs = logs.LogGroup(
            self, "ManageLogs", retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY)

        environment = {
            "DATABASE_URL": secret_ref("database_url"),
            "CRDB_API_KEY": secret_ref("crdb_api_key"),
            "CRDB_CLUSTER_ID": secret_ref("crdb_cluster_id"),
            "BIOGRAPHER_ROLE_ARN": secret_ref("biographer_role_arn"),
            "BIOGRAPHER_EXTERNAL_ID": secret_ref("biographer_external_id"),
            "AWS_REGION_NAME": REGION,
        }

        # Invoking a model is something the application does for itself, so this
        # sits on the execution role -- never on the read-only role it assumes
        # into the studied account. Scoped to model ARNs rather than bedrock:*.
        bedrock = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:Converse"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/*",
            ],
        )
        # Assuming the read-only role is the ONLY way either function touches
        # the studied account. Invariant 3 lives in that role, not in code.
        assume = iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{ACCOUNT}:role/*"],
        )

        chat = lambda_.Function(
            self, "Chat",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="biographer.agent.server.lambda_handler",
            code=code,
            environment=environment,
            timeout=Duration.seconds(120),
            memory_size=1024,
            # No reserved concurrency: this account's total limit is 10, and
            # reserving any of it drops unreserved below the 10 AWS requires
            # to stay free. The account limit is itself the fan-out ceiling,
            # and the spend ceiling in server.py is the real cost control.
            # Re-add a reservation if the concurrency quota is ever raised.
            log_group=chat_logs,
        )
        chat.add_to_role_policy(bedrock)
        chat.add_to_role_policy(assume)

        url = chat.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        manage = lambda_.Function(
            self, "ManagePass",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="biographer.manage.lambda_handler",
            code=code,
            environment=environment,
            # A multi-region scan plus verification over every memory; the
            # default 3 seconds would time out before the first region finished.
            timeout=Duration.minutes(10),
            memory_size=1024,
            log_group=manage_logs,
        )
        manage.add_to_role_policy(bedrock)
        manage.add_to_role_policy(assume)

        schedule_role = iam.Role(
            self, "ScheduleRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        manage.grant_invoke(schedule_role)

        scheduler.CfnSchedule(
            self, "ManageSchedule",
            # Hourly: often enough that verification is continuous, and it
            # doubles as the keep-warm that stops a CockroachDB Basic cluster
            # suspending in front of a judge (ADR-0004).
            schedule_expression="rate(1 hour)",
            flexible_time_window={"mode": "OFF"},
            target={
                "arn": manage.function_arn,
                "roleArn": schedule_role.role_arn,
                "input": '{"force_findings": false}',
            },
        )

        cdk.CfnOutput(self, "DemoUrl", value=url.url)
        cdk.CfnOutput(self, "ManageFunction", value=manage.function_name)


app = cdk.App()
BiographerStack(
    app, "BiographerStack",
    env=cdk.Environment(account=ACCOUNT or None, region=REGION),
    description="AWS Biographer -- an AWS account agent with verifiable memory",
)
app.synth()
