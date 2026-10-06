import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'consignment', 'kind': 'consignment', 'data': {'code': 'C-1', 'origin': 'Port-A', 'destination': 'Farm-B'}}, {'op': 'transition', 'target': 'consignment', 'action': 'inspect', 'data': {'inspector': 'I-1', 'inspection_result': 'suspected'}, 'expect': 'inspected'}, {'op': 'transition', 'target': 'consignment', 'action': 'quarantine', 'data': {'pest_found': True, 'sample_id': 'S-1'}, 'expect': 'quarantined'}, {'op': 'transition', 'target': 'consignment', 'action': 'destroy', 'data': {'method': 'incineration', 'witnessed_by': 'W-1'}, 'expect': 'destroyed'}, {'op': 'create', 'as': 'facility', 'kind': 'facility', 'data': {'name': 'Farm-B', 'address': 'County 1'}}, {'op': 'transition', 'target': 'facility', 'action': 'trace', 'data': {'consignment_ids': ['{consignment}']}, 'expect': 'traced'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
