"""The operate workflow of the graph: check an existing project, and diagnose a failure."""

import unittest

from graph_fakes import GraphCase

from tenx.graph.build import steps_for


class OperateCase(GraphCase):
    def setUp(self):
        super().setUp()
        self.project = self.root / "project"
        (self.project / "models").mkdir(parents=True)
        (self.project / "dbt_project.yml").write_text("name: shop\n")
        (self.project / "models" / "orders.sql").write_text("select 1\n")

    def operate(self, **changes):
        fields = {"workflow": "operate", "project": str(self.project), "cross_source": False}
        return self.request(**{**fields, **changes})


class OperatePlanTest(OperateCase, unittest.TestCase):
    def test_the_operate_workflow_checks_the_project_then_diagnoses_it(self):
        self.assertEqual(
            steps_for(self.operate()),
            ("intake", "profile", "overlap", "gate", "diagnostics", "report"),
        )

    def test_the_build_workflow_is_unchanged(self):
        self.assertEqual(
            steps_for(self.request()),
            ("profile", "overlap", "cross-source", "engineer", "gate", "report"),
        )


if __name__ == "__main__":
    unittest.main()
