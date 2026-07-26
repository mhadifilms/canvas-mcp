"""Grade arithmetic: what you have, what's left, and what you need on it.

Canvas shows a current percentage and leaves the useful questions unanswered -
how much is this assignment actually worth, and what do I need on the rest to
finish with a B? Both are answerable from the assignment groups, and both are
pure functions of that data, which is why they live here away from the HTTP.

Two grading models are supported, matching Canvas:

* **Unweighted** - every point is equal; the grade is earned/possible.
* **Weighted** - each assignment group carries a percentage of the final grade,
  and the group's internal percentage is what gets weighted. Groups with no
  graded work yet are excluded and the remaining weights are normalised, which
  is what Canvas does and why an early-semester grade can swing wildly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class GroupStanding:
    """One assignment group's contribution to the grade."""

    id: str
    name: str
    weight: float                 # percent of the final grade, 0 when unweighted
    earned: float = 0.0           # points scored on graded work
    graded_possible: float = 0.0  # points available on graded work
    remaining: float = 0.0        # points still to be graded
    graded_count: int = 0
    remaining_count: int = 0

    @property
    def percentage(self) -> float | None:
        if self.graded_possible <= 0:
            return None
        return self.earned / self.graded_possible

    @property
    def total_possible(self) -> float:
        return self.graded_possible + self.remaining


@dataclass
class CourseStanding:
    weighted: bool
    groups: list[GroupStanding] = field(default_factory=list)

    @property
    def earned(self) -> float:
        return sum(g.earned for g in self.groups)

    @property
    def graded_possible(self) -> float:
        return sum(g.graded_possible for g in self.groups)

    @property
    def remaining(self) -> float:
        return sum(g.remaining for g in self.groups)

    @property
    def counted_groups(self) -> list[GroupStanding]:
        """Groups Canvas would include in the grade right now."""
        return [g for g in self.groups if g.graded_possible > 0]

    @property
    def current(self) -> float | None:
        """The grade as it stands, 0-1, or None if nothing has been graded."""
        counted = self.counted_groups
        if not counted:
            return None
        if not self.weighted:
            return self.earned / self.graded_possible if self.graded_possible else None
        total_weight = sum(g.weight for g in counted)
        if total_weight <= 0:
            return self.earned / self.graded_possible if self.graded_possible else None
        return sum(g.weight * (g.percentage or 0) for g in counted) / total_weight

    def projected(self, remaining_rate: float) -> float | None:
        """The final grade if every remaining point is earned at ``remaining_rate``.

        Linear in the rate, which is what makes `needed_for` solvable directly.
        """
        active = [g for g in self.groups if g.total_possible > 0]
        if not active:
            return None
        if not self.weighted:
            possible = sum(g.total_possible for g in active)
            if possible <= 0:
                return None
            return (self.earned + remaining_rate * self.remaining) / possible
        total_weight = sum(g.weight for g in active)
        if total_weight <= 0:
            possible = sum(g.total_possible for g in active)
            return (self.earned + remaining_rate * self.remaining) / possible if possible else None
        return (
            sum(
                g.weight * (g.earned + remaining_rate * g.remaining) / g.total_possible
                for g in active
            )
            / total_weight
        )

    def needed_for(self, target: float) -> float | None:
        """Fraction needed on all remaining work to finish at ``target`` (0-1).

        Returns None when there is nothing left to earn. A result above 1 means the
        target is out of reach; at or below 0 means it is already secured.
        """
        if self.remaining <= 0:
            return None
        floor = self.projected(0.0)
        ceiling = self.projected(1.0)
        if floor is None or ceiling is None:
            return None
        slope = ceiling - floor
        if slope <= 0:
            return None
        return (target - floor) / slope

    def impact(self, group_id: str, points: float) -> float | None:
        """What fraction of the final grade an assignment of this size carries.

        Under weighting, a 50-point quiz in a 10%-weighted group worth 500 points
        total moves the final grade by 1 point, not 10 - which is exactly the
        intuition students get wrong.
        """
        group = next((g for g in self.groups if g.id == str(group_id)), None)
        if group is None or points <= 0:
            return None
        if not self.weighted:
            total = sum(g.total_possible for g in self.groups)
            return points / total if total > 0 else None
        total_weight = sum(g.weight for g in self.groups if g.total_possible > 0)
        if total_weight <= 0 or group.total_possible <= 0:
            return None
        return (group.weight / total_weight) * (points / group.total_possible)


def summarize(groups: list[dict[str, Any]], *, weighted: bool) -> CourseStanding:
    """Build a standing from Canvas assignment_groups (with assignments+submission)."""
    standing = CourseStanding(weighted=weighted)
    for group in groups:
        entry = GroupStanding(
            id=str(group.get("id")),
            name=str(group.get("name") or "Assignments"),
            weight=_number(group.get("group_weight")),
        )
        for assignment in group.get("assignments") or []:
            if not isinstance(assignment, dict) or assignment.get("omit_from_final_grade"):
                continue
            points = _number(assignment.get("points_possible"))
            submission = assignment.get("submission") or {}
            if submission.get("excused"):
                continue  # An excused assignment is removed from the denominator.
            score = submission.get("score")
            if score is not None and submission.get("workflow_state") == "graded":
                entry.earned += _number(score)
                entry.graded_possible += points
                entry.graded_count += 1
            elif points > 0:
                entry.remaining += points
                entry.remaining_count += 1
        standing.groups.append(entry)
    return standing


def with_hypothetical(
    standing: CourseStanding, group_id: str, points_possible: float, score: float
) -> CourseStanding:
    """The standing as it would be if one ungraded assignment came back at ``score``."""
    clone = CourseStanding(
        weighted=standing.weighted,
        groups=[replace(group) for group in standing.groups],
    )
    target = next((g for g in clone.groups if g.id == str(group_id)), None)
    if target is None:
        return clone
    if points_possible > 0:
        target.remaining = max(0.0, target.remaining - points_possible)
        target.remaining_count = max(0, target.remaining_count - 1)
    target.earned += score
    target.graded_possible += points_possible
    target.graded_count += 1
    return clone


def letter_for(percentage: float | None, scheme: list[tuple[str, float]]) -> str:
    if percentage is None:
        return "-"
    for name, cutoff in scheme:
        if percentage >= cutoff - 1e-9:
            return name
    return scheme[-1][0] if scheme else "-"


def next_grade_up(percentage: float | None, scheme: list[tuple[str, float]]) -> tuple[str, float] | None:
    """The cheapest letter grade still above where the student is now."""
    if not scheme:
        return None
    candidates = [
        (name, cutoff)
        for name, cutoff in scheme
        if percentage is None or cutoff > percentage + 1e-9
    ]
    return min(candidates, key=lambda pair: pair[1]) if candidates else None


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
