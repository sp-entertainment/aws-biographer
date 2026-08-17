"""Edge extraction and the source-precedence rules. No AWS, no cluster."""

from biographer.scan.edges import CONFIG, HUMAN, INFERRED, Index, extract
from biographer.scan.model import Resource


def _instance(arn="arn:aws:ec2:us-east-1:1:instance/i-0a", **config):
    return Resource(arn=arn, region="us-east-1", service="ec2",
                    resource_type="instance", config=config)


def test_config_references_resolve_from_bare_ids_to_arns():
    """Config blobs name other resources by id; edges must carry real ARNs."""
    sg = Resource(arn="arn:aws:ec2:us-east-1:1:security-group/sg-0b", region="us-east-1",
                  service="ec2", resource_type="security-group")
    edges = extract([_instance(SecurityGroups=["sg-0b"]), sg])
    assert any(e.dst_arn == sg.arn and e.edge_type == "protected_by" for e in edges)


def test_references_to_unknown_resources_produce_no_edge():
    """A dangling edge is worse than a missing one -- it asserts a lie."""
    edges = extract([_instance(SecurityGroups=["sg-does-not-exist"])])
    assert edges == []


def test_a_resource_never_links_to_itself():
    edges = extract([_instance(VpcId="i-0a")])
    assert all(e.src_arn != e.dst_arn for e in edges)


def test_log_group_naming_convention_is_inferred_never_asserted():
    """Invariant 8: convention is a hunch, so it may only be proposed."""
    fn = Resource(arn="arn:aws:lambda:us-east-1:1:function:worker", region="us-east-1",
                  service="lambda", resource_type="function", name="worker")
    lg = Resource(arn="arn:aws:logs:us-east-1:1:log-group:/aws/lambda/worker",
                  region="us-east-1", service="logs", resource_type="log-group",
                  name="/aws/lambda/worker", config={"logGroupName": "/aws/lambda/worker"})
    edges = extract([fn, lg])
    logs_to = [e for e in edges if e.edge_type == "logs_to"]
    assert len(logs_to) == 1
    assert logs_to[0].source == INFERRED, "convention must never be recorded as fact"
    assert logs_to[0].source != CONFIG and logs_to[0].source != HUMAN


def test_index_rejects_arns_it_has_not_seen():
    index = Index([_instance()])
    assert index.arn_for("arn:aws:ec2:us-east-1:1:instance/i-nope") is None
    assert index.arn_for(None) is None
    assert index.arn_for("i-0a") is not None
