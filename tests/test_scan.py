"""Collector-contract checks that need no AWS credentials and no cluster."""

import pytest

from biographer.scan.model import GLOBAL, Resource, tag_list_to_dict
from biographer.scan.runner import load_collectors


def test_every_collector_registers_with_a_valid_scope():
    specs = load_collectors()
    assert len(specs) >= 20, "collector registry looks truncated"
    for spec in specs:
        assert spec.scope in ("regional", "global")
        assert spec.service and spec.resource_type
        assert callable(spec.fn)


def test_collector_labels_are_unique():
    """Two collectors sharing a label make failures ambiguous in scans.stats."""
    labels = [s.label for s in load_collectors()]
    assert len(labels) == len(set(labels)), sorted(
        lbl for lbl in labels if labels.count(lbl) > 1
    )


def test_resource_refuses_a_blank_identifier():
    """Invariant 4: every answer referencing a resource carries an identifier."""
    with pytest.raises(ValueError):
        Resource(arn="", region=GLOBAL, service="s3", resource_type="bucket")


def test_tag_normalisation_handles_every_shape_aws_returns():
    assert tag_list_to_dict([{"Key": "env", "Value": "prod"}]) == {"env": "prod"}
    assert tag_list_to_dict([{"key": "env", "value": "prod"}]) == {"env": "prod"}
    assert tag_list_to_dict({"env": "prod"}) == {"env": "prod"}
    assert tag_list_to_dict(None) == {}
    # A tag with no value is legal in AWS and must not vanish.
    assert tag_list_to_dict([{"Key": "orphan"}]) == {"orphan": ""}
