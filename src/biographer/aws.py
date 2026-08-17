"""AWS session construction.

Invariant 3 -- read-only against AWS -- is enforced at the IAM layer, not here.
This module's job is to hand out a session that assumes the read-only role when
one is configured, and to fail loudly rather than silently falling back to
ambient credentials in a deployed context.
"""

from __future__ import annotations

import logging
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import settings

log = logging.getLogger(__name__)

# Retries matter more than usual here: a scan makes thousands of calls across
# many services, and the default of a few attempts turns ordinary throttling
# into missing resources, which looks identical to a resource that isn't there.
BOTO_CONFIG = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=30,
    user_agent_extra="biographer/0.1",
)

_lock = threading.Lock()
_sessions: dict[str, boto3.Session] = {}


def session_for(account_id: str | None = None) -> boto3.Session:
    """Return a session for the studied account.

    With `BIOGRAPHER_ROLE_ARN` set, assumes that role with its external ID. With
    it unset, returns ambient credentials -- acceptable for local development
    against your own account, never for a deployed scan of someone else's.

    Sessions are cached because `boto3.Session()` is not cheap and a scan builds
    one client per service per region on top of it.
    """
    key = account_id or "default"
    with _lock:
        cached = _sessions.get(key)
        if cached is not None:
            return cached

    cfg = settings()
    if not cfg.role_arn:
        log.warning(
            "no BIOGRAPHER_ROLE_ARN set -- using ambient credentials. Invariant 3 "
            "puts the read-only boundary in IAM, and ambient credentials are not "
            "that boundary. Acceptable locally, never in deployment."
        )
        made = boto3.Session(region_name=cfg.aws_region)
    else:
        sts = boto3.client("sts", region_name=cfg.aws_region, config=BOTO_CONFIG)
        params = {
            "RoleArn": cfg.role_arn,
            "RoleSessionName": "biographer-scan",
            "DurationSeconds": 3600,
        }
        if cfg.external_id:
            params["ExternalId"] = cfg.external_id
        try:
            creds = sts.assume_role(**params)["Credentials"]
        except ClientError as exc:
            # AWS forbids root from assuming any role, so a developer running
            # with root credentials cannot exercise the read-only path locally.
            # Degrade with a loud warning rather than blocking the scan; in
            # deployment the caller is a Lambda execution role and this branch
            # never runs.
            if "may not be assumed by root" not in str(exc):
                raise
            log.warning(
                "caller is account root, which AWS forbids from assuming roles -- "
                "falling back to ambient credentials. The read-only boundary is "
                "NOT in force. Use a non-root principal before deploying."
            )
            made = boto3.Session(region_name=cfg.aws_region)
            with _lock:
                _sessions[key] = made
            return made

        made = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=cfg.aws_region,
        )

    with _lock:
        _sessions[key] = made
    return made


def app_session() -> boto3.Session:
    """The application's OWN identity, never the studied account's role.

    Two different things were conflated here once and it is worth naming: the
    read-only role exists to read someone else's account, and it is deliberately
    incapable of anything else. Bedrock, CockroachDB, and every other thing the
    application does for itself run as the application. Routing them through the
    studied account's role means either an AccessDenied or -- far worse -- a
    read-only role quietly granted powers invariant 3 says it must not have.
    """
    return boto3.Session(region_name=settings().aws_region)


def client(session: boto3.Session, service: str, region: str | None = None):
    """A boto3 client carrying the scan's retry configuration."""
    return session.client(service, region_name=region, config=BOTO_CONFIG)


_account_ids: dict[int, str] = {}


def account_id_of(session: boto3.Session) -> str:
    """The account a session belongs to, cached per session.

    Collectors need this to synthesise ARNs for services whose APIs don't return
    one. Uncached, a scan would make one STS call per collector per region --
    dozens of round trips for a value that cannot change mid-scan.
    """
    key = id(session)
    with _lock:
        cached = _account_ids.get(key)
    if cached is None:
        cached = client(session, "sts").get_caller_identity()["Account"]
        with _lock:
            _account_ids[key] = cached
    return cached


def enabled_regions(session: boto3.Session) -> list[str]:
    """Regions this account can actually reach.

    Opted-out regions raise on every call, so scanning them is pure latency.
    """
    ec2 = client(session, "ec2")
    resp = ec2.describe_regions(AllRegions=False)
    return sorted(r["RegionName"] for r in resp.get("Regions", []))
