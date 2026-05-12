from bebop_bot.scoring import (
    compute_coherence,
    compute_entity_score,
    compute_momentum,
    compute_narrowness,
    compute_quality,
)


def test_compute_narrowness_buckets():
    assert compute_narrowness(0) == 1.0
    assert compute_narrowness(1) == 2.0
    assert compute_narrowness(2) == 3.0
    assert compute_narrowness(5) == 5.0
    assert compute_narrowness(15) == 5.0
    assert compute_narrowness(25) == 4.0
    assert compute_narrowness(75) == 3.0
    assert compute_narrowness(200) == 2.0


def test_compute_quality_buckets():
    assert compute_quality(0.0, 10) == 1.0
    assert compute_quality(5.0, 10) == 2.0
    assert compute_quality(8.0, 10) == 3.0
    assert compute_quality(11.0, 10) == 4.0
    assert compute_quality(20.0, 10) == 5.0


def test_compute_momentum_no_history():
    assert compute_momentum(0.0, 0.0) == 1.0
    assert compute_momentum(1.0, 0.0) == 5.0


def test_compute_momentum_ratio():
    assert compute_momentum(10.0, 5.0) == 4.0
    assert compute_momentum(30.0, 5.0) == 5.0
    assert compute_momentum(7.0, 5.0) == 3.0
    assert compute_momentum(4.0, 5.0) == 2.0
    assert compute_momentum(1.0, 5.0) == 1.0


def test_compute_coherence_zero_axes():
    assert compute_coherence([]) == 1.0


def test_compute_coherence_one_axis():
    partners = [("mechanism", "v4 hook", 1.0)]
    assert compute_coherence(partners) == 2.0


def test_compute_coherence_two_axes():
    partners = [
        ("mechanism", "v4 hook", 1.0),
        ("venue", "Uniswap", 1.0),
    ]
    assert compute_coherence(partners) == 3.0


def test_compute_coherence_three_axes():
    partners = [
        ("mechanism", "v4 hook", 1.0),
        ("venue", "Uniswap", 1.0),
        ("sector", "AMM", 1.0),
    ]
    assert compute_coherence(partners) == 4.0


def test_compute_coherence_four_axes():
    partners = [
        ("mechanism", "v4 hook", 1.0),
        ("venue", "Uniswap", 1.0),
        ("sector", "AMM", 1.0),
        ("handle", "ctrl", 1.0),
    ]
    assert compute_coherence(partners) == 5.0


def test_compute_coherence_five_axes_caps_at_5():
    partners = [
        ("mechanism", "v4 hook", 1.0),
        ("venue", "Uniswap", 1.0),
        ("sector", "AMM", 1.0),
        ("handle", "ctrl", 1.0),
        ("token", "PEPE", 1.0),
    ]
    assert compute_coherence(partners) == 5.0


def test_compute_coherence_low_weight_skipped():
    partners = [
        ("mechanism", "v4 hook", 0.2),
        ("venue", "Uniswap", 0.3),
    ]
    assert compute_coherence(partners) == 1.0


def test_compute_coherence_unknown_handle_does_not_count():
    partners = [
        ("handle", "randomperson", 2.0),
    ]
    assert compute_coherence(partners) == 1.0


def test_compute_coherence_known_handle_counts():
    partners = [("handle", "ctrl", 2.0)]
    assert compute_coherence(partners) == 2.0


def test_compute_entity_score_geometric_mean():
    score = compute_entity_score(
        unique_authors_7d=5,
        weighted_24h=10.0,
        raw_24h=10,
        mean_weighted_7d=2.0,
        cooccurrence_partners=[
            ("mechanism", "v4 hook", 1.0),
            ("venue", "Uniswap", 1.0),
        ],
    )
    assert 0 < score.composite <= 5.0
    # 4th root of (5*4*5*3) = (300)^.25 ~= 4.16
    assert abs(score.composite - 300 ** 0.25) < 0.01


def test_compute_entity_score_zero_floors_safely():
    score = compute_entity_score(
        unique_authors_7d=0,
        weighted_24h=0.0,
        raw_24h=0,
        mean_weighted_7d=0.0,
        cooccurrence_partners=[],
    )
    assert score.composite >= 1.0
