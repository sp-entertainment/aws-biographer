"""CloudTrail classification and identifier resolution, no AWS or cluster needed."""

from biographer.scan.cloudtrail import _actor, _resolve, classify


def test_event_names_classify_into_change_types():
    assert classify("RunInstances") == "created"
    assert classify("CreateBucket") == "created"
    assert classify("AllocateAddress") == "created"
    assert classify("TerminateInstances") == "deleted"
    assert classify("ReleaseStaticIp") == "deleted"
    assert classify("ModifyVolume") == "modified"
    # Unknown shapes are still real changes and must not be dropped.
    assert classify("PutUserPolicy") == "modified"


def test_resolve_prefers_the_authoritative_resources_array():
    index = {"i-0abc12345678": "arn:aws:ec2:us-east-1:1:instance/i-0abc12345678"}
    event = {"Resources": [{"ResourceName": "arn:aws:s3:::explicit"}]}
    assert _resolve(event, "{}", index) == "arn:aws:s3:::explicit"


def test_resolve_falls_back_to_identifiers_in_the_raw_record():
    """Most write events leave Resources empty and name the target inline."""
    arn = "arn:aws:ec2:us-east-1:1:instance/i-0abc12345678"
    index = {"i-0abc12345678": arn}
    raw = '{"requestParameters":{"instanceId":"i-0abc12345678"}}'
    assert _resolve({"Resources": []}, raw, index) == arn


def test_resolve_refuses_identifiers_it_has_never_seen():
    """An unknown id is not evidence; inventing an ARN would break invariant 4."""
    raw = '{"requestParameters":{"instanceId":"i-0999999999999"}}'
    assert _resolve({"Resources": []}, raw, {}) is None


def test_actor_falls_through_to_the_session_issuer():
    """Assumed roles carry no Username, and that is when 'who' matters most."""
    detail = {
        "userIdentity": {
            "type": "AssumedRole",
            "sessionContext": {"sessionIssuer": {"userName": "deploy-role"}},
        }
    }
    assert _actor(detail) == "deploy-role"
    assert _actor({"userIdentity": {"type": "AWSService"}}) == "AWSService"
