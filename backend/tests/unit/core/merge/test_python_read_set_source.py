"""Assembling the read-set index from the declared attributes and the analyzed queries.

The schema says which attributes exist, the transform queries say what each reads, and the two
arrive separately. What is pinned here is what happens when the second half is missing: every
declared attribute still gets an entry, so the resolver widens it instead of dropping it.
"""

from __future__ import annotations

from infrahub.core.merge.python_target_sources import DatabasePythonReadSetSource
from infrahub.core.schema.schema_branch_computed import TransformReadSet
from tests.adapters.python_target_sources import (
    FailingAnalyzedPythonReadSets,
    StaticAnalyzedPythonReadSets,
    StaticDeclaredPythonAttributes,
)

BRANCH = "main"
DEVICE = "TestingDevice"
SUMMARY = TransformReadSet(read_kinds=frozenset({DEVICE}), read_fields={DEVICE: frozenset({"name"})})


def _source(
    *, declared: list[tuple[str, str]], analyzed: dict[tuple[str, str], TransformReadSet] | None = None
) -> DatabasePythonReadSetSource:
    return DatabasePythonReadSetSource(
        declared_attributes=StaticDeclaredPythonAttributes(declared=declared),
        read_sets=StaticAnalyzedPythonReadSets(analyzed=analyzed or {}),
    )


async def test_an_analyzed_attribute_keeps_its_read_set() -> None:
    source = _source(declared=[(DEVICE, "summary")], analyzed={(DEVICE, "summary"): SUMMARY})

    read_sets = await source.read_sets(branch=BRANCH)

    assert [(entry.kind, entry.attribute_name) for entry in read_sets] == [(DEVICE, "summary")]
    assert read_sets[0].read_set == SUMMARY


async def test_an_attribute_the_analysis_skipped_is_reported_as_undeterminable() -> None:
    """One transform the gather could not resolve must not silence the attribute it feeds."""
    source = _source(declared=[(DEVICE, "summary"), (DEVICE, "digest")], analyzed={(DEVICE, "summary"): SUMMARY})

    read_sets = await source.read_sets(branch=BRANCH)

    by_name = {entry.attribute_name: entry.read_set for entry in read_sets}
    assert by_name["summary"] == SUMMARY
    assert by_name["digest"].depends_on_everything is True


async def test_a_failed_analysis_widens_every_declared_attribute() -> None:
    """The analysis resolves its peers strictly, so one missing peer raises for all of them.

    Returning nothing would leave every value stale, so each declared attribute is reported
    undeterminable and recomputed over its whole kind.
    """
    analyzed = FailingAnalyzedPythonReadSets()
    source = DatabasePythonReadSetSource(
        declared_attributes=StaticDeclaredPythonAttributes(declared=[(DEVICE, "summary"), (DEVICE, "digest")]),
        read_sets=analyzed,
    )

    read_sets = await source.read_sets(branch=BRANCH)

    assert analyzed.calls == [BRANCH]
    assert {entry.attribute_name for entry in read_sets} == {"summary", "digest"}
    assert all(entry.read_set.depends_on_everything for entry in read_sets)


async def test_a_branch_declaring_nothing_never_reaches_the_analysis() -> None:
    analyzed = StaticAnalyzedPythonReadSets(analyzed={})
    source = DatabasePythonReadSetSource(
        declared_attributes=StaticDeclaredPythonAttributes(declared=[]), read_sets=analyzed
    )

    assert await source.read_sets(branch=BRANCH) == []
    assert analyzed.calls == []
