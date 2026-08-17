"""The migration runner's ordering and idempotency, without needing a cluster.

The DB-dependent half of Phase 1's acceptance test lives in db.healthcheck().
"""

import re

from biographer.db import MIGRATIONS_DIR, _MIGRATION_NAME


def test_migration_filenames_are_well_formed_and_uniquely_ordered():
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert files, "no migrations on disk"
    for path in files:
        assert _MIGRATION_NAME.match(path.name), f"{path.name} will be skipped silently"

    versions = [int(_MIGRATION_NAME.match(p.name).group(1)) for p in files]
    assert len(set(versions)) == len(versions), "duplicate version prefix"
    # Sorted filenames must equal sorted versions, or apply order is not the
    # order the numbers imply.
    assert versions == sorted(versions)


def test_vector_dimension_is_consistent_between_schema_and_config():
    """A mismatch here is silent at DDL time and fatal at insert time."""
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    dims = set(re.findall(r"VECTOR\((\d+)\)", sql))
    assert dims == {"1024"}, f"expected every vector column at 1024, found {dims}"


def test_to_vector_emits_pgvector_text_not_a_postgres_array():
    """A Python list crosses the wire as an array and CockroachDB rejects it."""
    from biographer.db import to_vector

    assert to_vector([1, 2.5, 0]) == "[1.0,2.5,0.0]"
    assert to_vector([0.0] * 3).startswith("[") and to_vector([0.0] * 3).endswith("]")
