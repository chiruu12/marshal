"""The routing ledger — pure, no filesystem.

These tests are mostly about what the ledger REFUSES to say: it must not rank on cost it never
measured, must not read a pruned run record as an unjudged one, and must not turn an empty
denominator into a 0% success rate.
"""

from __future__ import annotations

import random
from datetime import UTC

import pytest

from marshal_engine.accounting.ledger import (
    UNKNOWN_CLIENT,
    UNTAGGED,
    RoutingCell,
    rank_cells,
    summarize_routing,
)
from marshal_engine.accounting.usage import UsageEvent


def _event(
    run_id: str,
    *,
    client: str | None = "a",
    task_kind: str | None = "refactor",
    cost: float = 0.0,
    source: str = "unavailable",
    duration_ms: int = 0,
    ts: str = "2026-07-01T00:00:00+00:00",
) -> UsageEvent:
    return UsageEvent(
        ts=ts,
        run_id=run_id,
        backend="opencode",
        client=client,
        task_kind=task_kind,
        cost_usd=cost,
        source=source,
        duration_ms=duration_ms,
        status="exited_clean",
    )


def _cell(**kw: object) -> RoutingCell:
    kw.setdefault("task_kind", "k")
    kw.setdefault("client", "c")
    return RoutingCell(**kw)  # type: ignore[arg-type]


# --- the join ---------------------------------------------------------------------------------


def test_a_pruned_run_record_is_not_an_unjudged_run() -> None:
    """Absence in the index and a None value mean different things.

    The ledger is append-only and permanent; run records are not. If a pruned record counted as
    unjudged, deleted history would silently enter the denominator of every rate.
    """
    events = [_event("gone"), _event("here")]
    ledger = summarize_routing(events, {"here": None})  # "gone" has no record at all
    cell = ledger.cells[0]
    assert cell.n_runs == 2
    assert cell.n_no_record == 1
    assert cell.n_unjudged == 1
    assert cell.n_judged == 0
    assert ledger.events_without_record == 1


def test_an_unjudged_run_is_excluded_from_the_rate_not_counted_as_a_failure() -> None:
    ledger = summarize_routing(
        [_event("r1"), _event("r2")], {"r1": "integrated", "r2": None}
    )
    cell = ledger.cells[0]
    assert cell.n_judged == 1
    assert cell.integration_rate == 1.0  # 1/1 judged, NOT 1/2 runs
    assert any("not judged" in n for n in cell.notes)


def test_nothing_judged_yields_an_unknown_rate_not_zero_percent() -> None:
    """A rate over an empty denominator is unknown. Rendering it as 0% defames an unreviewed client."""
    ledger = summarize_routing([_event("r1")], {"r1": None})
    cell = ledger.cells[0]
    assert cell.integration_rate is None
    assert cell.rank is None          # unranked...
    assert cell in ledger.cells       # ...but still reported, with its counts
    assert cell.n_runs == 1
    assert ledger.recommended is None  # never a guess
    assert ledger.caveat is not None and "set_outcome" in ledger.caveat


# --- cost honesty -----------------------------------------------------------------------------


def test_unmeasured_cost_is_none_never_zero() -> None:
    """`0.0` would read as free. The honest answer to 'what did it cost' here is 'unknown'."""
    ledger = summarize_routing(
        [_event("r1", source="unavailable", cost=0.0)], {"r1": "integrated"}
    )
    cell = ledger.cells[0]
    assert cell.mean_cost_per_integrated is None
    assert cell.mean_cost_per_integrated != 0.0
    assert cell.priced_integrated_runs == 0
    assert cell.cost_ranked is False
    assert any("unranked on price" in n for n in cell.notes)


def test_only_measured_sources_contribute_cost() -> None:
    events = [
        _event("r1", source="native", cost=0.10),
        _event("r2", source="admin-api", cost=0.20),
        _event("r3", source="unavailable", cost=99.0),  # a number nobody measured
    ]
    ledger = summarize_routing(events, dict.fromkeys(("r1", "r2", "r3"), "integrated"))
    cell = ledger.cells[0]
    assert cell.priced_integrated_runs == 2
    assert cell.mean_cost_per_integrated == pytest.approx(0.15)
    assert cell.measured_cost_all_usd == pytest.approx(0.30)
    assert cell.cost_native + cell.cost_admin_api == pytest.approx(cell.measured_cost_all_usd)
    # The unmeasured run is visible as a gap, not silently folded in.
    assert any("2/3 integrated" in n for n in cell.notes)


def test_spend_on_rejected_runs_stays_visible() -> None:
    """Cost-per-integrated alone flatters a client that burns four rejects for every keeper."""
    events = [_event(f"r{i}", source="native", cost=1.0) for i in range(5)]
    outcomes = {"r0": "integrated", **{f"r{i}": "rejected" for i in range(1, 5)}}
    cell = summarize_routing(events, outcomes).cells[0]
    assert cell.mean_cost_per_integrated == pytest.approx(1.0)
    assert cell.measured_cost_all_usd == pytest.approx(5.0)  # what it actually cost you


# --- ranking ----------------------------------------------------------------------------------


def test_rate_beats_cost() -> None:
    cheap_bad = _cell(client="cheap", integration_rate=0.2, mean_cost_per_integrated=0.01,
                      priced_integrated_runs=1, n_judged=5)
    dear_good = _cell(client="dear", integration_rate=0.9, mean_cost_per_integrated=5.0,
                      priced_integrated_runs=1, n_judged=10)
    assert [c.client for c in rank_cells([cheap_bad, dear_good])] == ["dear", "cheap"]


def test_duration_breaks_a_rate_tie_and_unknown_duration_never_wins() -> None:
    fast = _cell(client="fast", integration_rate=1.0, mean_duration_ms=100.0, duration_runs=1)
    slow = _cell(client="slow", integration_rate=1.0, mean_duration_ms=900.0, duration_runs=1)
    unknown = _cell(client="unknown", integration_rate=1.0)  # duration_runs == 0
    ordered = [c.client for c in rank_cells([slow, unknown, fast])]
    assert ordered.index("fast") < ordered.index("slow") < ordered.index("unknown")


def test_unmeasured_cost_cannot_win_the_cost_tiebreak() -> None:
    """The bug this prevents: treating unmeasured cost as $0 makes the silent backend the winner."""
    measured = _cell(client="measured", integration_rate=1.0, mean_duration_ms=10.0,
                     duration_runs=1, mean_cost_per_integrated=2.0, priced_integrated_runs=1)
    silent = _cell(client="silent", integration_rate=1.0, mean_duration_ms=10.0, duration_runs=1)
    assert [c.client for c in rank_cells([silent, measured])] == ["measured", "silent"]


def test_unmeasured_cost_does_not_forfeit_rank_earned_on_rate() -> None:
    """Being unmeasured is not a penalty - it only forfeits the tiebreak it cannot participate in."""
    silent_good = _cell(client="silent", integration_rate=1.0)
    measured_bad = _cell(client="measured", integration_rate=0.1,
                         mean_cost_per_integrated=0.001, priced_integrated_runs=1)
    assert [c.client for c in rank_cells([measured_bad, silent_good])] == ["silent", "measured"]


def test_unjudged_cells_sort_last_and_stay_unranked() -> None:
    judged = _cell(client="judged", integration_rate=0.5, n_judged=2)
    unjudged = _cell(client="aaa-unjudged")  # alphabetically first, but no evidence
    ordered = rank_cells([unjudged, judged])
    assert [c.client for c in ordered] == ["judged", "aaa-unjudged"]
    assert ordered[0].rank == 1
    assert ordered[1].rank is None


def test_ranking_is_deterministic_under_shuffled_input() -> None:
    cells = [_cell(task_kind="k", client=c, integration_rate=1.0) for c in ("c", "a", "b")]
    for seed in range(5):
        shuffled = cells[:]
        random.Random(seed).shuffle(shuffled)
        assert [c.client for c in rank_cells(shuffled)] == ["a", "b", "c"]


# --- small samples are reported, never hidden -------------------------------------------------


def test_a_single_run_can_rank_first_but_carries_its_n() -> None:
    """No threshold: hiding the row would be the dishonest option. Attaching `n` is the honest one."""
    events = [_event("r1", client="lucky"), _event("r2", client="steady"), _event("r3", client="steady")]
    outcomes = {"r1": "integrated", "r2": "integrated", "r3": "rejected"}
    ledger = summarize_routing(events, outcomes)
    top = ledger.cells[0]
    assert top.client == "lucky"
    assert top.rank == 1
    assert "n=1" in top.evidence
    assert any("small sample" in n for n in top.notes)
    assert ledger.recommended == "lucky"


def test_no_threshold_parameter_exists() -> None:
    """A minimum-n knob would let a caller silently drop evidence; there is deliberately none."""
    import inspect

    for fn in (summarize_routing, rank_cells):
        params = set(inspect.signature(fn).parameters)
        assert not {"min_n", "threshold", "min_runs", "min_samples"} & params


# --- grouping and windows ---------------------------------------------------------------------


def test_missing_task_kind_and_client_get_visible_sentinels() -> None:
    ledger = summarize_routing([_event("r1", client=None, task_kind=None)], {"r1": "integrated"})
    assert (ledger.cells[0].task_kind, ledger.cells[0].client) == (UNTAGGED, UNKNOWN_CLIENT)


def test_task_kind_is_stripped_but_not_case_folded() -> None:
    """A typo should be a visible row, not silently merged into its neighbour."""
    events = [_event("r1", task_kind=" refactor "), _event("r2", task_kind="Refactor")]
    ledger = summarize_routing(events, {"r1": "integrated", "r2": "integrated"})
    assert {c.task_kind for c in ledger.cells} == {"refactor", "Refactor"}


def test_task_kind_filter_selects_one_group() -> None:
    events = [_event("r1", task_kind="docs"), _event("r2", task_kind="bugfix")]
    ledger = summarize_routing(events, {"r1": "integrated", "r2": "integrated"}, task_kind="docs")
    assert [c.task_kind for c in ledger.cells] == ["docs"]
    assert ledger.task_kind_filter == "docs"


def test_a_blank_task_kind_filter_selects_untagged_rather_than_nothing() -> None:
    """A filter may only name a key events can produce, or it silently hides the whole ledger.

    Whitespace-only used to normalize to `""` on the filter side while events normalized blank to
    `untagged`, so this asked for a key nothing has and returned an empty ledger - reading as "no
    history" when the history was there.
    """
    events = [_event("r1", task_kind=None), _event("r2", task_kind="docs")]
    outcomes = {"r1": "integrated", "r2": "integrated"}
    for blank in ("   ", "", "\t\n"):
        ledger = summarize_routing(events, outcomes, task_kind=blank)
        assert [c.task_kind for c in ledger.cells] == ["untagged"], blank
        assert ledger.task_kind_filter == "untagged", blank


def test_the_task_kind_filter_is_stripped_to_match_its_group() -> None:
    events = [_event("r1", task_kind="docs")]
    ledger = summarize_routing(events, {"r1": "integrated"}, task_kind="  docs  ")
    assert [c.task_kind for c in ledger.cells] == ["docs"]


def test_events_outside_the_window_are_excluded() -> None:
    from datetime import datetime

    events = [
        _event("old", ts="2020-01-01T00:00:00+00:00"),
        _event("new", ts="2026-07-01T00:00:00+00:00"),
    ]
    since = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = summarize_routing(events, {"old": "integrated", "new": "integrated"}, since=since)
    assert ledger.total_runs == 1


def test_an_empty_ledger_recommends_nothing() -> None:
    ledger = summarize_routing([], {})
    assert ledger.cells == []
    assert ledger.recommended is None
    assert ledger.total_runs == 0


# --- ranking is per task_kind, not global -----------------------------------------------------


def test_rank_restarts_within_each_task_kind() -> None:
    """A client measured on `docs` and one measured on `refactor` are not in the same league.

    Ranking them together would produce a single global order, and `recommended` would then name a
    client for a kind of task it has never been judged on. Each kind gets its own #1.
    """
    cells = [
        _cell(task_kind="docs", client="slow", n_judged=2, n_integrated=1, integration_rate=0.5),
        _cell(task_kind="docs", client="good", n_judged=2, n_integrated=2, integration_rate=1.0),
        _cell(task_kind="refactor", client="weak", n_judged=4, n_integrated=1, integration_rate=0.25),
    ]
    ranked = rank_cells(cells)
    assert [(c.task_kind, c.client, c.rank) for c in ranked] == [
        ("docs", "good", 1),
        ("docs", "slow", 2),
        ("refactor", "weak", 1),
    ], "ranking leaked across task kinds"


def test_no_single_recommendation_when_several_task_kinds_are_in_view() -> None:
    """`recommended` answers "who should get this task". With several kinds there is no "this"."""
    outcomes = {"r1": "integrated", "r2": "rejected"}
    ledger = summarize_routing(
        [
            _event("r1", client="a", task_kind="docs"),
            _event("r2", client="b", task_kind="refactor"),
        ],
        outcomes,
    )
    assert ledger.recommended is None
    assert ledger.recommended_task_kind is None
    # ...but the per-kind answers are still available, which is the useful form here.
    assert ledger.recommended_by_task_kind == {"docs": "a", "refactor": "b"}


def test_single_task_kind_still_gets_a_headline_recommendation() -> None:
    ledger = summarize_routing(
        [_event("r1", client="a"), _event("r2", client="b")],
        {"r1": "integrated", "r2": "rejected"},
    )
    assert ledger.recommended == "a"
    assert ledger.recommended_task_kind == "refactor"
    assert ledger.recommended_by_task_kind == {"refactor": "a"}


def test_the_best_client_of_a_kind_cannot_be_outranked_by_a_better_one_elsewhere() -> None:
    """REGRESSION: with a global order, a 100%-on-docs client displaced the top refactor client,
    and `recommended` named it for refactor work it had never done."""
    ledger = summarize_routing(
        [
            _event("d1", client="docs-star", task_kind="docs"),
            _event("r1", client="refactor-best", task_kind="refactor"),
            _event("r2", client="refactor-worse", task_kind="refactor"),
        ],
        {"d1": "integrated", "r1": "integrated", "r2": "rejected"},
    )
    by_key = {(c.task_kind, c.client): c.rank for c in ledger.cells}
    assert by_key[("refactor", "refactor-best")] == 1
    assert by_key[("docs", "docs-star")] == 1
    assert ledger.recommended_by_task_kind["refactor"] == "refactor-best"
