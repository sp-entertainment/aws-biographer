"""Claim evaluation. The rules that decide what retires and what does not."""

from biographer.memory.verify import Kind, Verdict, evaluate


def test_a_memory_with_no_claim_is_unverifiable_not_false(monkeypatch):
    """Human annotations often have nothing checkable, and they matter most."""
    assert evaluate("acct", None).verdict is Verdict.UNVERIFIABLE
    assert evaluate("acct", {}).verdict is Verdict.UNVERIFIABLE


def test_an_unknown_claim_kind_is_unverifiable_not_false():
    """A malformed claim must never be read as evidence the memory is wrong."""
    check = evaluate("acct", {"kind": "something_invented", "arn": "arn:aws:x"})
    assert check.verdict is Verdict.UNVERIFIABLE


def test_a_missing_resource_in_an_unswept_region_is_unverifiable(monkeypatch):
    """Absence of evidence is not evidence of absence.

    Without this, one partial scan retires every memory about a skipped region.
    """
    import biographer.memory.verify as v

    monkeypatch.setattr(v, "_resource", lambda *a: None)
    monkeypatch.setattr(v, "_region_was_swept", lambda *a: False)
    check = v.evaluate("acct", {"kind": Kind.CONFIG_ABSENT.value,
                                "arn": "arn:aws:ec2:eu-west-1:1:volume/vol-0a",
                                "region": "eu-west-1", "path": "AttachedTo"})
    assert check.verdict is Verdict.UNVERIFIABLE


def test_a_missing_resource_in_a_swept_region_is_false(monkeypatch):
    import biographer.memory.verify as v

    monkeypatch.setattr(v, "_resource", lambda *a: None)
    monkeypatch.setattr(v, "_region_was_swept", lambda *a: True)
    check = v.evaluate("acct", {"kind": Kind.CONFIG_ABSENT.value,
                                "arn": "arn:aws:ec2:us-east-1:1:volume/vol-0a",
                                "region": "us-east-1", "path": "AttachedTo"})
    assert check.verdict is Verdict.FALSE


def test_config_absent_holds_while_the_field_is_empty(monkeypatch):
    import biographer.memory.verify as v

    monkeypatch.setattr(v, "_resource",
                        lambda *a: {"tags": {}, "config": {"AttachedTo": []},
                                    "region": "us-east-1", "arn": "x", "last_seen": None})
    claim = {"kind": Kind.CONFIG_ABSENT.value, "arn": "x", "region": "us-east-1",
             "path": "AttachedTo"}
    assert v.evaluate("acct", claim).verdict is Verdict.HOLDS


def test_config_absent_goes_false_once_the_field_is_populated(monkeypatch):
    """This is the retirement path: the volume got attached."""
    import biographer.memory.verify as v

    monkeypatch.setattr(v, "_resource",
                        lambda *a: {"tags": {}, "config": {"AttachedTo": ["i-0a"]},
                                    "region": "us-east-1", "arn": "x", "last_seen": None})
    claim = {"kind": Kind.CONFIG_ABSENT.value, "arn": "x", "region": "us-east-1",
             "path": "AttachedTo"}
    check = v.evaluate("acct", claim)
    assert check.verdict is Verdict.FALSE
    assert "i-0a" in check.detail


def test_untagged_claim_goes_false_when_a_tag_appears(monkeypatch):
    import biographer.memory.verify as v

    monkeypatch.setattr(v, "_resource",
                        lambda *a: {"tags": {"Owner": "platform"}, "config": {},
                                    "region": "us-east-1", "arn": "x", "last_seen": None})
    check = v.evaluate("acct", {"kind": Kind.UNTAGGED.value, "arn": "x",
                                "region": "us-east-1"})
    assert check.verdict is Verdict.FALSE
    assert "Owner" in check.detail
