"""Memory rules that can be checked without a cluster or a model."""

from biographer.memory import store


def test_merge_output_is_capped(monkeypatch):
    """Without a ceiling, repeated merges grow a memory into a transcript."""
    monkeypatch.setattr(store, "ask_cheap", lambda *a, **k: "x" * 5000)
    merged = store.merge_bodies("old", "new")
    assert merged is not None
    assert len(merged) == store.MAX_BODY_CHARS


def test_merge_returns_none_when_the_model_fails(monkeypatch):
    """None is the signal that makes the caller keep both rows (invariant 6)."""
    def boom(*a, **k):
        raise TimeoutError("model timed out")

    monkeypatch.setattr(store, "ask_cheap", boom)
    assert store.merge_bodies("old", "new") is None


def test_merge_treats_an_empty_reply_as_failure():
    """An empty merge would otherwise silently erase a memory's contents."""
    import biographer.memory.store as s
    original = s.ask_cheap
    s.ask_cheap = lambda *a, **k: "   "
    try:
        assert s.merge_bodies("old", "new") is None
    finally:
        s.ask_cheap = original


def test_durability_check_keeps_by_default_when_the_model_fails(monkeypatch):
    """A false positive costs one row; a false negative loses knowledge."""
    def boom(*a, **k):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(store, "ask_cheap", boom)
    assert store.is_durable("anything") is True


def test_durability_check_parses_the_verdict(monkeypatch):
    monkeypatch.setattr(store, "ask_cheap", lambda *a, **k: "DROP")
    assert store.is_durable("the user said hello") is False
    monkeypatch.setattr(store, "ask_cheap", lambda *a, **k: "KEEP")
    assert store.is_durable("unattached since March") is True
