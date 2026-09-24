from app.research.runtime.runner import _requires_insight_layer


def test_insight_gate_requires_mechanism_for_trends_and_comparisons() -> None:
    assert _requires_insight_layer("trend_forecast", "")
    assert _requires_insight_layer("comparison", "")
    assert _requires_insight_layer("recommendation", "分析未来发展方向")


def test_insight_gate_does_not_block_generic_recommendations() -> None:
    assert not _requires_insight_layer("recommendation", "推荐值得加入的公司")
    assert not _requires_insight_layer("structured_report", "整理公司公开信息")
