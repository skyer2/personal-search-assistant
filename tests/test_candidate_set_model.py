from app.research.planning.candidate import CandidateSet


def test_candidate_set_roundtrip_and_lifecycle_fields():
    payload = CandidateSet(
        candidate_set_id="cs1",
        status="complete",
        candidates=[],
        source_task_ids=["t_discovery"],
    ).to_dict()
    payload["items"] = ["Alpha", "Beta"]
    candidate_set = CandidateSet.from_dict(payload)
    assert candidate_set.available
    assert candidate_set.items == ["Alpha", "Beta"]
    assert candidate_set.expanded is False
    assert candidate_set.expanded_plan_version is None
    roundtrip = CandidateSet.from_dict(candidate_set.to_dict())
    assert roundtrip.to_dict() == candidate_set.to_dict()
