"""Lane routing and fusion. Pure functions -- no cluster, no model."""

from biographer.retrieval import LANE_WEIGHTS, Hit, _fuse, route


def test_an_arn_routes_to_the_identifier_lane():
    plan = route("what is arn:aws:ec2:us-east-1:1:instance/i-0abc12345678 for?")
    assert "identifier" in plan.lanes
    assert plan.identifiers


def test_a_bare_instance_id_routes_to_the_identifier_lane():
    plan = route("tell me about i-0ad130ebd061c9a6f")
    assert "identifier" in plan.lanes


def test_ordinary_prose_does_not_trigger_the_identifier_lane():
    """A loose 'i-' pattern would fire on any hyphenated word."""
    plan = route("what is going on with my in-flight requests?")
    assert "identifier" not in plan.lanes
    assert plan.identifiers == []


def test_waste_vocabulary_routes_to_structured_not_only_vector():
    """'Unattached' is a fact in a config blob, not a mood."""
    plan = route("what looks abandoned?")
    assert "structured" in plan.lanes
    assert plan.waste


def test_the_vector_lane_always_runs():
    """It is the only lane that answers words the schema does not contain."""
    for question in ("what looks abandoned?", "tell me about i-0abc12345678", "hello"):
        assert "vector" in route(question).lanes


def test_region_and_service_are_extracted():
    plan = route("untagged s3 buckets in us-east-1")
    assert plan.regions == ["us-east-1"]
    assert "s3" in plan.services


def test_fusion_rewards_appearing_in_multiple_lanes():
    """Agreement across lanes is itself evidence, so the scores add."""
    shared = Hit("resource", "arn:a", "a", "")
    only_vector = Hit("resource", "arn:b", "b", "")
    fused = _fuse(
        {"identifier": [Hit("resource", "arn:a", "a", "")],
         "vector": [shared, only_vector]},
        limit=10,
    )
    top = fused[0]
    assert top.identifier == "arn:a"
    assert top.lanes == {"identifier", "vector"}


def test_identifier_lane_outweighs_vector():
    """Embeddings are bad at identifiers; an exact match must win."""
    assert LANE_WEIGHTS["identifier"] > LANE_WEIGHTS["vector"]


def test_human_memories_edge_out_agent_ones_at_equal_rank():
    fused = _fuse(
        {"vector": [Hit("memory", "m1", "t", "b", payload={"origin": "agent",
                                                           "verified_at": 1}),
                    Hit("memory", "m2", "t", "b", payload={"origin": "human",
                                                           "verified_at": 1})]},
        limit=10,
    )
    # m1 ranks first within the lane, but human provenance closes the gap.
    scores = {h.identifier: h.score for h in fused}
    assert scores["m2"] > scores["m1"] * 0.95
