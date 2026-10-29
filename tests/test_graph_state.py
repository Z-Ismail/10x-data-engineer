"""Tests for the graph state: what it may hold and how parallel writes join."""

import typing
import unittest
from decimal import Decimal

from tenx.domain import AgentFinding
from tenx.graph.state import (
    STATUSES,
    GraphState,
    StateConflict,
    merge_artefacts,
    merge_attempts,
    plain,
)


def ref(name: str, attempt: int, digit: str = "a") -> dict:
    return {
        "name": name,
        "path": f"/runs/r/{name}",
        "kind": "file",
        "sha256": digit * 64,
        "attempt": attempt,
    }


class PlainTest(unittest.TestCase):
    def test_json_values_pass_unchanged(self):
        value = {"a": [1, "x", None, True, {"b": []}], "c": "0.10"}
        self.assertIs(plain(value), value)

    def test_anything_else_is_refused_with_where_it_is(self):
        finding = AgentFinding(agent="a", claim="c", kind="fact", confidence="low")
        cases = [
            ({"costs": [{"usd": 0.1}]}, "update.costs[0].usd is a float"),
            ({"usd": Decimal("0.1")}, "update.usd is a Decimal"),
            ({"findings": [finding]}, "update.findings[0] is a AgentFinding"),
            ({"steps": ("profile",)}, "update.steps is a tuple"),
            ({"seen": {"a"}}, "update.seen is a set"),
            ({1: "x"}, "update has a key that is not text: 1"),
        ]
        for value, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(TypeError) as caught:
                plain(value)
            self.assertIn(expected, str(caught.exception))
        # A record's own JSON form is accepted.
        self.assertEqual(plain({"findings": [finding.dump()]})["findings"][0]["claim"], "c")


class MergeArtefactsTest(unittest.TestCase):
    def test_new_names_are_added_and_nothing_is_changed_in_place(self):
        left = {"profiles": ref("profiles", 0)}
        merged = merge_artefacts(left, {"project": ref("project", 1)})
        self.assertEqual(merged, {"profiles": ref("profiles", 0), "project": ref("project", 1)})
        self.assertEqual(left, {"profiles": ref("profiles", 0)})
        self.assertEqual(merge_artefacts(None, None), {})

    def test_a_later_attempt_replaces_an_earlier_one(self):
        merged = merge_artefacts(
            {"project": ref("project", 1)}, {"project": ref("project", 2, "b")}
        )
        self.assertEqual(merged, {"project": ref("project", 2, "b")})

    def test_the_same_write_twice_is_not_a_conflict(self):
        same = {"project": ref("project", 1)}
        self.assertEqual(merge_artefacts(same, dict(same)), same)

    def test_disagreeing_writes_are_refused(self):
        cases = [
            # The same attempt giving another file: two steps disagree.
            ({"project": ref("project", 1)}, {"project": ref("project", 1, "b")}, "attempt 1"),
            # An earlier attempt overwriting a later one.
            ({"project": ref("project", 2)}, {"project": ref("project", 1, "b")}, "attempt 2"),
            # A name that refers to another artefact.
            ({}, {"project": ref("gate", 1)}, "refers to 'gate'"),
        ]
        for left, right, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(StateConflict) as caught:
                merge_artefacts(left, right)
            self.assertIn(expected, str(caught.exception))

    def test_a_malformed_artefact_is_refused(self):
        with self.assertRaises(ValueError):
            merge_artefacts({}, {"project": {**ref("project", 1), "sha256": "x"}})


class MergeAttemptsTest(unittest.TestCase):
    def test_counts_stay_or_rise(self):
        self.assertEqual(
            merge_attempts({"engineer": 1, "gate": 1}, {"engineer": 2, "cross-source": 1}),
            {"engineer": 2, "gate": 1, "cross-source": 1},
        )
        self.assertEqual(merge_attempts({"engineer": 2}, {"engineer": 2}), {"engineer": 2})

    def test_a_falling_or_malformed_count_is_refused(self):
        for right in [{"engineer": 1}, {"engineer": True}, {"engineer": "3"}, {"engineer": -1}]:
            with self.subTest(right=right), self.assertRaises(StateConflict):
                merge_attempts({"engineer": 2}, right)


class GraphStateTest(unittest.TestCase):
    def test_statuses_match_the_state(self):
        hints = typing.get_type_hints(GraphState, include_extras=True)
        self.assertEqual(typing.get_args(hints["status"]), STATUSES)
        reducers = {
            key: typing.get_args(hint)[1]
            for key, hint in hints.items()
            if typing.get_origin(hint) is typing.Annotated
        }
        self.assertEqual(
            set(reducers), {"artefacts", "findings", "validations", "costs", "attempts"}
        )
        self.assertIs(reducers["artefacts"], merge_artefacts)
        self.assertIs(reducers["attempts"], merge_attempts)


if __name__ == "__main__":
    unittest.main()
