import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from tenx import policy
from tenx.policy import (
    STOPPED,
    Budget,
    BudgetStop,
    Policy,
    PolicyError,
    amount,
    from_dict,
    load,
    measured,
)


class AmountTest(unittest.TestCase):
    def test_numbers_and_text_give_the_written_value(self):
        self.assertEqual(amount(0.1), Decimal("0.1"))
        self.assertEqual(amount(2), Decimal(2))
        self.assertEqual(amount("0.75"), Decimal("0.75"))
        self.assertEqual(amount(" 1.2345 "), Decimal("1.2345"))
        self.assertEqual(amount(Decimal("0.40")), Decimal("0.40"))

    def test_a_float_is_not_taken_at_its_binary_value(self):
        # Decimal(0.1) would be 0.1000000000000000055511151231257827...
        self.assertEqual(amount(0.1) + amount(0.2), Decimal("0.3"))
        self.assertNotEqual(Decimal(float("0.1")), Decimal("0.1"))

    def test_malformed_amounts_are_refused(self):
        for value in (True, None, -1, "-0.5", "1e3", 1e-05, "12,50", "", "0.12345", [1], "n/a"):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                amount(value, "run_usd")

    def test_the_refusal_names_the_field(self):
        with self.assertRaisesRegex(PolicyError, r"^run_usd must be plain decimal"):
            amount("1e3", "run_usd")


class PolicyTest(unittest.TestCase):
    def test_defaults(self):
        rules = Policy()
        self.assertEqual(rules.run_usd, Decimal("2.00"))
        self.assertEqual(rules.cap(policy.ENGINEER), Decimal("0.75"))
        self.assertEqual(rules.cap(policy.CROSS_SOURCE), Decimal("0.40"))
        self.assertEqual(rules.cap(policy.SCOUT), Decimal("0.10"))
        self.assertEqual(rules.approve_above_usd, Decimal("5.00"))
        self.assertEqual(
            rules.typical_usd,
            {
                "engineer": Decimal("0.20"),
                "cross-source": Decimal("0.08"),
                "scout": Decimal("0.02"),
            },
        )

    def test_a_call_cap_above_the_ceiling_is_refused(self):
        with self.assertRaisesRegex(PolicyError, r"engineer call may spend US\$0.75, more than"):
            Policy(run_usd=Decimal("0.50"))

    def test_a_call_cap_equal_to_the_ceiling_is_allowed(self):
        Policy(run_usd=Decimal("0.75"))

    def test_unknown_kind(self):
        with self.assertRaisesRegex(PolicyError, r"no cap for 'reviewer'"):
            Policy().cap("reviewer")

    def test_to_dict_reads_back_the_same(self):
        rules = Policy(run_usd=Decimal("3.5"))
        self.assertEqual(from_dict(json.loads(json.dumps(rules.to_dict()))), rules)


class FromDictTest(unittest.TestCase):
    CAPS: ClassVar[dict] = {"engineer": 0.5, "cross-source": "0.25", "scout": 0.05}

    def test_fields_left_out_keep_their_defaults(self):
        rules = from_dict({"run_usd": 1})
        self.assertEqual(rules.run_usd, Decimal(1))
        self.assertEqual(rules.call_usd, Policy().call_usd)
        self.assertEqual(rules.approve_above_usd, Decimal("5.00"))

    def test_every_field(self):
        rules = from_dict(
            {
                "run_usd": "1.5",
                "call_usd": self.CAPS,
                "typical_usd": self.CAPS,
                "approve_above_usd": 3,
            }
        )
        self.assertEqual(rules.cap("cross-source"), Decimal("0.25"))
        self.assertEqual(rules.typical_usd["scout"], Decimal("0.05"))
        self.assertEqual(rules.approve_above_usd, Decimal(3))

    def test_mistakes_are_refused_with_the_reason(self):
        mistakes = {
            "not an object": ([], r"must be a JSON object"),
            "unknown field": ({"run_usd": 1, "max_usd": 2}, r"unknown policy fields: max_usd"),
            "bad amount": ({"run_usd": "two"}, r"^run_usd must be plain decimal"),
            "caps not an object": ({"call_usd": 0.5}, r"call_usd must be an object"),
            "unknown kind": (
                {"call_usd": {**self.CAPS, "qa": 1}},
                r"call_usd names unknown kinds of agent: qa",
            ),
            "missing kind": (
                {"typical_usd": {"engineer": 1, "scout": 1}},
                r"typical_usd is missing: cross-source",
            ),
            "bad cap": (
                {"call_usd": {**self.CAPS, "scout": -1}},
                r"^call_usd.scout must be plain decimal",
            ),
            "cap above ceiling": ({"run_usd": "0.40"}, r"engineer call may spend"),
        }
        for name, (document, message) in mistakes.items():
            with self.subTest(name), self.assertRaisesRegex(PolicyError, message):
                from_dict(document)


class LoadTest(unittest.TestCase):
    def test_reads_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            path.write_text('{"run_usd": 1.25}')
            self.assertEqual(load(path).run_usd, Decimal("1.25"))

    def test_missing_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            with self.assertRaisesRegex(PolicyError, r"cannot read the policy"):
                load(path)
            path.write_text("{run_usd: 1}")
            with self.assertRaisesRegex(PolicyError, r"is not JSON"):
                load(path)


def caps(run, engineer="0.75", cross_source="0.40", scout="0.10") -> Policy:
    return Policy(
        run_usd=Decimal(run),
        call_usd={
            "engineer": Decimal(engineer),
            "cross-source": Decimal(cross_source),
            "scout": Decimal(scout),
        },
    )


class MeasuredTest(unittest.TestCase):
    def test_report_figures(self):
        self.assertEqual(measured(0.0455485), Decimal("0.0455485"))
        self.assertEqual(measured(0), Decimal(0))
        for value in (None, True, -0.1, "0.2", [0.1]):
            with self.subTest(value=value):
                self.assertIsNone(measured(value))


class BudgetTest(unittest.TestCase):
    def test_a_call_that_does_not_fit_is_stopped_before_it_starts(self):
        budget = Budget(caps("1.00"))
        engineer = budget.reserve("engineer", "engineer")
        with self.assertRaises(BudgetStop) as stop:
            budget.reserve("cross-source", "cross-source")
        self.assertEqual(stop.exception.status, STOPPED)
        self.assertEqual(
            str(stop.exception),
            "stopped: budget: cross-source was not started; its cap of US$0.40 with "
            "US$0 spent and US$0.75 reserved would pass the run's ceiling of US$1.00",
        )
        # Nothing was reserved for the refused call.
        self.assertEqual(budget.reserved_usd, Decimal("0.75"))
        budget.settle(engineer, 0.5, True)
        self.assertEqual(budget.spent_usd, Decimal("0.5"))
        self.assertEqual(budget.reserved_usd, Decimal(0))
        # Settled at its measured cost, the room it left is free again.
        budget.reserve("cross-source", "cross-source")

    def test_a_call_that_fits_exactly_starts(self):
        # As floats, 0.1 + 0.2 > 0.3, which would refuse the second call.
        self.assertGreater(0.1 + 0.2, 0.3)
        budget = Budget(caps("0.3", cross_source="0.2", scout="0.1", engineer="0.1"))
        budget.reserve("scout a", "scout")
        budget.reserve("cross-source", "cross-source")
        self.assertEqual(budget.reserved_usd, Decimal("0.3"))
        with self.assertRaises(BudgetStop):
            budget.reserve("scout b", "scout")

    def test_ten_small_calls_fill_a_ceiling_exactly(self):
        budget = Budget(caps("1.00", engineer="0.1", cross_source="0.1"))
        for number in range(10):
            reservation = budget.reserve(f"scout {number}", "scout")
            budget.settle(reservation, 0.1, True)
        self.assertEqual(budget.spent_usd, Decimal("1.0"))
        # Added one by one as floats, ten 0.1s fall short of 1.0 (Python 3.12's
        # sum() compensates, so the loop is written out).
        total = 0.0
        for _ in range(10):
            total += 0.1
        self.assertNotEqual(total, 1.0)
        with self.assertRaises(BudgetStop):
            budget.reserve("scout 10", "scout")

    def test_a_call_without_a_known_cost_is_charged_its_whole_reservation(self):
        budget = Budget(caps("10"))
        cases = [(None, True), (0.12, False), ("0.12", True), (True, True)]
        for cost, known in cases:
            with self.subTest(cost=cost, known=known):
                settled = budget.settle(budget.reserve("engineer", "engineer"), cost, known)
                self.assertIsNone(settled.measured_usd)
                self.assertEqual(settled.charged_usd, Decimal("0.75"))
                self.assertEqual(settled.overshoot_usd, Decimal(0))
        self.assertEqual(budget.spent_usd, Decimal("3.00"))

    def test_a_call_over_its_cap_is_charged_what_it_cost(self):
        budget = Budget(caps("2.00", scout="0.01"))
        # The probe of 2026-10-28: a cap of US$0.01 stopped after one response at 0.0455485.
        settled = budget.settle(budget.reserve("scout a", "scout"), 0.0455485, True)
        self.assertEqual(settled.charged_usd, Decimal("0.0455485"))
        self.assertEqual(settled.overshoot_usd, Decimal("0.0355485"))
        self.assertEqual(budget.spent_usd, Decimal("0.0455485"))

    def test_a_reservation_is_settled_once(self):
        budget = Budget()
        reservation = budget.reserve("engineer", "engineer")
        budget.settle(reservation, 0.1, True)
        with self.assertRaisesRegex(ValueError, r"reservation 1 is not open"):
            budget.settle(reservation, 0.1, True)

    def test_unknown_kind_is_refused_before_reserving(self):
        budget = Budget()
        with self.assertRaises(PolicyError):
            budget.reserve("reviewer", "reviewer")
        self.assertEqual(budget.reserved_usd, Decimal(0))

    def test_threads_never_reserve_past_the_ceiling(self):
        # Room for exactly 7 calls of US$0.10; 32 threads race for it, 50 times.
        for _ in range(50):
            budget = Budget(caps("0.70", engineer="0.1", cross_source="0.1"))
            start = threading.Barrier(32)
            granted, stopped = [], []

            def call(number, budget=budget, start=start, granted=granted, stopped=stopped):
                start.wait()
                try:
                    granted.append(budget.reserve(f"scout {number}", "scout"))
                except BudgetStop:
                    stopped.append(number)

            threads = [threading.Thread(target=call, args=(n,)) for n in range(32)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual((len(granted), len(stopped)), (7, 25))
            self.assertEqual(len({r.number for r in granted}), 7)
            self.assertEqual(budget.reserved_usd, Decimal("0.70"))


if __name__ == "__main__":
    unittest.main()
