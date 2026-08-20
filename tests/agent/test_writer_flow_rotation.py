"""Regression coverage for deterministic writing-flow rotation."""

from agent.content.writing.writer import _FLOWS, _expression_goals, _pick_flow


def test_pick_flow_is_stable_for_same_seed():
    assert _pick_flow("2026-08-20") == _pick_flow("2026-08-20")


def test_pick_flow_rotates_across_multiple_seeds():
    selected = {_pick_flow(str(index)) for index in range(5)}

    assert selected <= set(_FLOWS)
    assert len(selected) >= 2


def test_expression_goals_contains_selected_flow_name():
    flow_seed = "2026-08-20:불면증"
    expected_flow = _pick_flow(f"{flow_seed}:wordpress:health")

    prompt = _expression_goals(
        platform_id="wordpress",
        category_id="health",
        flow_seed=flow_seed,
    )

    assert f"'{expected_flow}'" in prompt
    assert "다른 흐름을 고르지 않는다." in prompt
