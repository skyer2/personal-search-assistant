from app.research.coverage.gaps import build_semantic_gaps, stable_gap_id


def test_gap_id_is_semantic_and_not_task_bound():
    first = stable_gap_id("DeepSeek", "commercialization", "coverage")
    second = stable_gap_id("DeepSeek", "commercialization", "coverage")
    assert first == second
    assert "task" not in first


def test_gap_attempts_survive_reassessment():
    coverage = {
        "contract_id": "c",
        "spec_id": "s",
        "units": [
            {
                "coverage_id": "u1",
                "subject_id": "DeepSeek",
                "dimension_id": "commercialization",
                "status": "missing",
                "evidence_ids": [],
                "claim_ids": [],
                "confidence": 0,
                "reason_codes": ["no_claim"],
            }
        ],
    }
    previous = build_semantic_gaps(coverage)
    previous[0].attempted_actions.append("CONDUCT_RESEARCH")
    previous[0].attempt_count = 1
    reassessed = build_semantic_gaps(coverage, previous_gaps=[previous[0].to_dict()])
    assert reassessed[0].gap_id == previous[0].gap_id
    assert reassessed[0].attempted_actions == ["CONDUCT_RESEARCH"]
    assert reassessed[0].attempt_count == 1
