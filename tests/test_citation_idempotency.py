from app.agent.harness.citations import CitationManager


def test_binding_the_same_locator_reuses_admitted_evidence():
    manager = CitationManager()
    locator = "https://example.com/report"

    first = manager.bind_worker_facts(
        1,
        "research",
        ["Fact one", "Fact two"],
        [locator, "https://other.example/report"],
    )
    second = manager.bind_worker_facts(
        2,
        "research",
        ["Fact one", "Fact two"],
        [locator, "https://other.example/report"],
    )

    assert [source.source_id for source in first] == ["src-1", "src-2"]
    assert [source.source_id for source in second] == ["src-1", "src-2"]
    assert len(manager.sources) == 2
    assert len(manager.fact_bindings) == 4
