"""The grade arithmetic is the part a student would act on, so it gets checked hard."""

import pytest

from canvas_mcp import gradecalc
from canvas_mcp.queries import DEFAULT_SCHEME


def group(gid, name, weight, assignments):
    return {"id": gid, "name": name, "group_weight": weight, "assignments": assignments}


def graded(aid, points, score, group_id=None):
    a = {
        "id": aid,
        "points_possible": points,
        "submission": {"workflow_state": "graded", "score": score},
    }
    if group_id:
        a["assignment_group_id"] = group_id
    return a


def pending(aid, points):
    return {"id": aid, "points_possible": points, "submission": {"workflow_state": "unsubmitted"}}


# --------------------------------------------------------------------------- #
# Unweighted courses
# --------------------------------------------------------------------------- #

def test_unweighted_current_grade_is_points_over_points():
    standing = gradecalc.summarize(
        [group("1", "Everything", 0, [graded("a", 100, 80), graded("b", 100, 90)])],
        weighted=False,
    )
    assert standing.current == pytest.approx(0.85)
    assert standing.earned == 170
    assert standing.graded_possible == 200


def test_ungraded_work_does_not_drag_the_current_grade_down():
    """Canvas shows the grade on graded work only; a pending essay is not a zero."""
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 100, 90), pending("b", 100)])],
        weighted=False,
    )
    assert standing.current == pytest.approx(0.90)
    assert standing.remaining == 100


def test_projection_spans_do_nothing_to_ace_everything():
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 100, 90), pending("b", 100)])],
        weighted=False,
    )
    assert standing.projected(0.0) == pytest.approx(0.45)
    assert standing.projected(1.0) == pytest.approx(0.95)
    assert standing.projected(0.5) == pytest.approx(0.70)


def test_needed_for_target_unweighted():
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 100, 90), pending("b", 100)])],
        weighted=False,
    )
    # 90 earned of 200 total; a B at 80% means 160 points, so 70 of the last 100.
    assert standing.needed_for(0.80) == pytest.approx(0.70)


def test_needed_for_an_unreachable_target_exceeds_one():
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 900, 400), pending("b", 100)])],
        weighted=False,
    )
    assert standing.needed_for(0.90) > 1.0


def test_needed_for_an_already_secured_target_is_not_positive():
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 900, 890), pending("b", 100)])],
        weighted=False,
    )
    assert standing.needed_for(0.80) <= 0


def test_needed_for_is_none_when_nothing_is_left():
    standing = gradecalc.summarize([group("1", "All", 0, [graded("a", 100, 85)])], weighted=False)
    assert standing.needed_for(0.90) is None


# --------------------------------------------------------------------------- #
# Weighted courses - where student intuition usually goes wrong
# --------------------------------------------------------------------------- #

WEIGHTED = [
    group("hw", "Homework", 20, [graded("h1", 100, 100)]),
    group("exam", "Exams", 80, [graded("e1", 100, 70), pending("e2", 100)]),
]


def test_weighted_grade_uses_group_weights_not_raw_points():
    standing = gradecalc.summarize(WEIGHTED, weighted=True)
    # Homework 100% at 20%, exams 70% at 80% -> 0.2 + 0.56
    assert standing.current == pytest.approx(0.76)


def test_weighted_grade_ignores_groups_with_nothing_graded():
    groups = [
        group("hw", "Homework", 30, [graded("h1", 10, 10)]),
        group("final", "Final", 70, [pending("f1", 100)]),
    ]
    standing = gradecalc.summarize(groups, weighted=True)
    # Only homework has been graded, so the grade is 100% - the classic early-term
    # illusion, and matching what Canvas itself displays.
    assert standing.current == pytest.approx(1.0)
    assert standing.projected(0.0) == pytest.approx(0.30)


def test_weighted_projection_and_needed_agree():
    standing = gradecalc.summarize(WEIGHTED, weighted=True)
    needed = standing.needed_for(0.90)
    assert standing.projected(needed) == pytest.approx(0.90)


def test_weighted_impact_reflects_the_group_weight():
    """Identical point values are worth different amounts in different groups."""
    standing = gradecalc.summarize(WEIGHTED, weighted=True)
    # Exams carry 80% spread over 200 points, so 100 points is half of that.
    assert standing.impact("exam", 100) == pytest.approx(0.40)
    # The same raw 100 points of homework is the whole 20% group - half the impact.
    assert standing.impact("hw", 100) == pytest.approx(0.20)


def test_a_big_assignment_in_a_small_group_barely_matters():
    """The intuition students get wrong: 500 points that are only worth 5%."""
    groups = [
        group("part", "Participation", 5, [graded("p1", 500, 500), pending("p2", 500)]),
        group("exam", "Exams", 95, [graded("e1", 100, 80)]),
    ]
    standing = gradecalc.summarize(groups, weighted=True)
    assert standing.impact("part", 500) == pytest.approx(0.025)
    assert standing.impact("exam", 100) == pytest.approx(0.95)


def test_unweighted_impact_is_just_share_of_total_points():
    standing = gradecalc.summarize(
        [group("1", "All", 0, [graded("a", 100, 90), pending("b", 100)])], weighted=False
    )
    assert standing.impact("1", 100) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Canvas's exclusion rules
# --------------------------------------------------------------------------- #

def test_excused_work_leaves_the_denominator():
    groups = [
        {
            "id": "1",
            "name": "All",
            "group_weight": 0,
            "assignments": [
                graded("a", 100, 90),
                {"id": "b", "points_possible": 100, "submission": {"excused": True}},
            ],
        }
    ]
    standing = gradecalc.summarize(groups, weighted=False)
    assert standing.graded_possible == 100
    assert standing.remaining == 0
    assert standing.current == pytest.approx(0.90)


def test_omitted_assignments_are_ignored_entirely():
    groups = [
        {
            "id": "1",
            "name": "All",
            "group_weight": 0,
            "assignments": [
                graded("a", 100, 90),
                {"id": "b", "points_possible": 500, "omit_from_final_grade": True,
                 "submission": {"workflow_state": "unsubmitted"}},
            ],
        }
    ]
    standing = gradecalc.summarize(groups, weighted=False)
    assert standing.remaining == 0


def test_submitted_but_ungraded_still_counts_as_remaining():
    groups = [
        {
            "id": "1",
            "name": "All",
            "group_weight": 0,
            "assignments": [
                {"id": "a", "points_possible": 50,
                 "submission": {"workflow_state": "submitted", "score": None}},
            ],
        }
    ]
    standing = gradecalc.summarize(groups, weighted=False)
    assert standing.remaining == 50
    assert standing.current is None


def test_zero_point_practice_work_does_not_inflate_anything():
    groups = [group("1", "All", 0, [graded("a", 100, 80), pending("practice", 0)])]
    standing = gradecalc.summarize(groups, weighted=False)
    assert standing.remaining == 0
    assert standing.current == pytest.approx(0.80)


def test_nothing_graded_yet_reads_as_no_grade():
    standing = gradecalc.summarize([group("1", "All", 0, [pending("a", 100)])], weighted=False)
    assert standing.current is None
    assert standing.projected(1.0) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Hypotheticals and letters
# --------------------------------------------------------------------------- #

def test_what_if_moves_the_grade_by_the_expected_amount():
    standing = gradecalc.summarize(WEIGHTED, weighted=True)
    after = gradecalc.with_hypothetical(standing, "exam", 100, 100)
    # Exams become 170/200 = 85%, weighted 0.8 -> 0.68, plus homework 0.2
    assert after.current == pytest.approx(0.88)
    assert after.remaining == 0


def test_what_if_a_zero_is_as_bad_as_it_should_be():
    standing = gradecalc.summarize(WEIGHTED, weighted=True)
    after = gradecalc.with_hypothetical(standing, "exam", 100, 0)
    assert after.current == pytest.approx(0.48)


def test_letter_lookup_and_next_grade_up():
    assert gradecalc.letter_for(0.95, DEFAULT_SCHEME) == "A"
    assert gradecalc.letter_for(0.905, DEFAULT_SCHEME) == "A-"
    assert gradecalc.letter_for(0.5, DEFAULT_SCHEME) == "F"
    assert gradecalc.letter_for(None, DEFAULT_SCHEME) == "-"

    name, cutoff = gradecalc.next_grade_up(0.845, DEFAULT_SCHEME)
    assert (name, cutoff) == ("B+", 0.87)


def test_letter_boundaries_are_inclusive():
    """A student sitting exactly on 90 has the A-, not the B+."""
    assert gradecalc.letter_for(0.90, DEFAULT_SCHEME) == "A-"


def test_malformed_canvas_data_does_not_explode():
    groups = [
        {"id": "1", "name": None, "group_weight": None, "assignments": [
            {"id": "a", "points_possible": "not a number", "submission": None},
            "this should not be here",
        ]},
    ]
    standing = gradecalc.summarize(groups, weighted=True)
    assert standing.current is None


# --------------------------------------------------------------------------- #
# Crunch detection
# --------------------------------------------------------------------------- #

def _planner(offset_hours, points, title="thing"):
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return {
        "plannable_date": when.isoformat().replace("+00:00", "Z"),
        "plannable": {"title": title, "points_possible": points},
        "plannable_type": "assignment",
    }


def test_a_single_big_assignment_is_not_a_pile_up():
    """One 150-point essay on its own is just an assignment."""
    from canvas_mcp.queries import find_crunches

    assert find_crunches([_planner(72, 150)]) == []


def test_three_things_close_together_is_a_pile_up():
    from canvas_mcp.queries import find_crunches

    items = [_planner(24, 10, "a"), _planner(30, 10, "b"), _planner(40, 10, "c")]
    clusters = find_crunches(items)
    assert len(clusters) == 1
    assert len(clusters[0]["items"]) == 3


def test_two_heavy_things_together_count_even_though_there_are_only_two():
    from canvas_mcp.queries import find_crunches

    clusters = find_crunches([_planner(24, 80, "midterm"), _planner(30, 60, "essay")])
    assert len(clusters) == 1
    assert clusters[0]["points"] == 140


def test_spread_out_deadlines_are_left_alone():
    from canvas_mcp.queries import find_crunches

    items = [_planner(24, 50), _planner(24 + 96, 50), _planner(24 + 192, 50)]
    assert find_crunches(items) == []


def test_overlapping_windows_collapse_to_one_cluster():
    from canvas_mcp.queries import find_crunches

    items = [_planner(h, 10) for h in (24, 28, 32, 36)]
    clusters = find_crunches(items)
    assert len(clusters) == 1
    assert len(clusters[0]["items"]) == 4
