"""Tests for the contract's schema.

The demo contract in tests/fixtures is written by hand from the demo goal.
"""

import copy
import json
import unittest
from pathlib import Path

import jsonschema

from tenx.contract import SCHEMA

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "demo_contract.json").read_text(encoding="utf-8"))


class Schema(unittest.TestCase):
    def test_the_demo_contract_has_the_schema_shape(self):
        jsonschema.validate(CONTRACT, SCHEMA)

    def test_the_schema_refuses_extra_and_missing_fields(self):
        extra = copy.deepcopy(CONTRACT)
        extra["models"][0]["owner"] = "data"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extra, SCHEMA)
        missing = copy.deepcopy(CONTRACT)
        del missing["models"][0]["grain"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(missing, SCHEMA)

    def test_the_schema_refuses_an_unknown_layer_or_rule(self):
        layer = copy.deepcopy(CONTRACT)
        layer["models"][0]["layer"] = "marts"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(layer, SCHEMA)
        rule = copy.deepcopy(CONTRACT)
        rule["models"][0]["exclusions"][0]["rule"] = "filter"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(rule, SCHEMA)


if __name__ == "__main__":
    unittest.main()
