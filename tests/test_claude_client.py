from bebop_bot.claude_client import (
    SCORING_SYSTEM_PROMPT,
    _compute_composite,
    _extract_json,
)


def test_composite_formula_geometric_mean():
    assert abs(_compute_composite(5, 5, 5) - 5.0) < 1e-6
    assert abs(_compute_composite(1, 1, 1) - 1.0) < 1e-6
    assert abs(_compute_composite(4, 4, 1) - (16 ** (1 / 3))) < 1e-6
    assert _compute_composite(0, 5, 5) == 0.0


def test_composite_capped_at_5():
    assert _compute_composite(5, 5, 5) <= 5.0


def test_extract_json_plain():
    assert _extract_json('{"scores": []}') == {"scores": []}


def test_extract_json_with_code_fence():
    raw = '```json\n{"scores": [{"i": 1, "on_topic": 5, "substance": 4, "novelty": 3}]}\n```'
    out = _extract_json(raw)
    assert out is not None
    assert out["scores"][0]["i"] == 1


def test_extract_json_garbage_returns_none():
    assert _extract_json("no json here") is None


def test_extract_json_with_prose_around():
    raw = 'Here you go: {"scores": [{"i": 1}]} - done'
    out = _extract_json(raw)
    assert out == {"scores": [{"i": 1}]}


def test_scoring_system_prompt_verbatim():
    # The prompt should contain the rubric verbatim, including dimension headings
    p = SCORING_SYSTEM_PROMPT
    assert "You are a curation filter for a feed about Bebop" in p
    assert "DEX aggregator and PMM/RFQ execution protocol" in p
    assert "on_topic - does it relate to the domain above?" in p
    assert "substance - is it analytical/argued/data-backed" in p
    assert "novelty - does it add something not already obvious" in p
    assert "1 = unrelated" in p
    assert "5 = bullseye, central to the domain" in p
    assert "1 = spam, shill, price call, chart screenshot only, engagement bait" in p
    assert "5 = deep analysis, original data, builder-level detail" in p
    assert "1 = obvious / well-worn" in p
    assert "5 = genuinely new thinking, contrarian and supported" in p
    assert (
        '{"scores": [{"i": 1, "on_topic": N, "substance": N, "novelty": N, "reasoning": "one line"}, ...]}'
        in p
    )
