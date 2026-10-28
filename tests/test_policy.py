import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from tenx import policy
from tenx.policy import Policy, PolicyError, amount, from_dict, load


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


if __name__ == "__main__":
    unittest.main()
