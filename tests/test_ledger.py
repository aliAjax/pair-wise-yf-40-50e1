import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository, SCHEMA_VERSION
from src.rules import RuleEngine, trace_downstream_facilities, active_propagation_edges
from src.service import DomainService, LedgerProcessingError
import sqlite3


ADMIN = Actor("admin-1", "admin")
INSPECTOR_A = Actor("inspector-a", "inspector")
INSPECTOR_B = Actor("inspector-b", "inspector")
LAB = Actor("lab-1", "lab")
QUAR = Actor("quar-1", "quarantine")


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def make_batch(self, code, origin, destination, actor=ADMIN, effective=None):
        data = {"code": code, "origin": origin, "destination": destination}
        entity = self.service.create(actor, "consignment", data)
        if effective is not None:
            # Consignment creation stamps the link with entity created_at;
            # tests that need explicit timing add a manual historical link.
            self.service.register_link(ADMIN, {
                "upstream": origin,
                "downstream": destination,
                "consignment_id": entity["id"],
                "effective_from": effective,
            })
        return entity

    def make_facility(self, name, address="County"):
        return self.service.create(ADMIN, "facility", {"name": name, "address": address})

    def positive(self, consignment_id, actor=LAB, sample_id=None, effective_at=None,
                 pest_name="fruit fly"):
        return self.service.submit_lab_result(actor, consignment_id, {
            "sample_id": sample_id or ("S-" + consignment_id[:4]),
            "pest_found": True,
            "pest_name": pest_name,
            "finding": "larvae observed",
            "effective_at": effective_at,
        })

    def notifications(self, root, **kwargs):
        return self.service.list_notifications(consignment_id=root, **kwargs)

    def latest_for(self, root, location):
        return self.notifications(root, target_location=location)[-1]


class LedgerHappyPathTest(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.farm_b = self.make_facility("Farm-B")
        self.farm_c = self.make_facility("Farm-C")
        # Port-A -> Farm-B (batch C-1), Farm-B -> Farm-C (resale batch C-2)
        self.c1 = self.make_batch("C-1", "Port-A", "Farm-B", effective="2026-09-01T00:00:00+00:00")
        self.c2 = self.make_batch("C-2", "Farm-B", "Farm-C", effective="2026-09-10T00:00:00+00:00")

    def test_positive_result_traces_downstream_and_versions_notifications(self):
        result = self.positive(self.c1["id"], sample_id="S-1",
                               effective_at="2026-09-15T00:00:00+00:00")
        run = result["run"]
        self.assertEqual(run["status"], "completed")
        detail = self.service.get_run_detail(run["id"])
        targets = [item["target_location"] for item in detail["items"]]
        self.assertEqual(targets, ["Farm-B", "Farm-C"])

        notes = self.notifications(self.c1["id"])
        self.assertEqual([(n["target_location"], n["version_no"], n["status"]) for n in notes],
                         [("Farm-B", 1, "issued"), ("Farm-C", 1, "issued")])

        # Farm-B confirms its notice: its version is preserved on recompute.
        note_b = self.latest_for(self.c1["id"], "Farm-B")
        self.service.acknowledge_notification(QUAR, note_b["id"], note="receipt signed")

        # A late effective result arrives: unfinished runs invalidate and the
        # ledger recomputes; Farm-B keeps its acknowledged version, Farm-C's
        # unconfirmed notice is voided and reissued at version 2.
        rerun = self.positive(
            self.c1["id"], actor=INSPECTOR_A, sample_id="S-1-recheck",
            effective_at="2026-09-20T00:00:00+00:00", pest_name="fruit fly (confirmed)"
        )
        self.assertEqual(rerun["run"]["status"], "completed")

        notes_b = self.notifications(self.c1["id"], target_location="Farm-B")
        self.assertEqual(len(notes_b), 1)
        self.assertEqual(notes_b[0]["status"], "acknowledged")

        notes_c = self.notifications(self.c1["id"], target_location="Farm-C")
        self.assertEqual([n["version_no"] for n in notes_c], [1, 2])
        self.assertEqual([n["status"] for n in notes_c], ["voided", "issued"])
        self.assertEqual(notes_c[1]["supersedes_id"], notes_c[0]["id"])
        self.assertEqual(notes_c[0]["superseded_by_id"], notes_c[1]["id"])

        # The voided historical notice is still readable.
        self.assertIsNotNone(self.service.repository.get_notification(notes_c[0]["id"]))

    def test_completed_conclusions_remain_queryable_after_late_result(self):
        first = self.positive(self.c1["id"], sample_id="S-2",
                              effective_at="2026-09-15T00:00:00+00:00")["run"]
        self.positive(self.c1["id"], sample_id="S-2-recheck",
                      effective_at="2026-09-22T00:00:00+00:00")
        runs = self.service.list_runs(self.c1["id"])
        self.assertEqual(len(runs), 2)
        self.assertEqual([r["generation"] for r in runs], [1, 2])
        stored_first = self.service.get_run_detail(first["id"])
        self.assertEqual(stored_first["status"], "completed")
        self.assertEqual(len(stored_first["items"]), 2)

    def test_acknowledged_notification_cannot_be_voided_later(self):
        self.positive(self.c1["id"], sample_id="S-3",
                      effective_at="2026-09-15T00:00:00+00:00")
        note = self.latest_for(self.c1["id"], "Farm-C")
        self.service.acknowledge_notification(QUAR, note["id"])
        self.positive(self.c1["id"], sample_id="S-3b",
                      effective_at="2026-09-25T00:00:00+00:00")
        note_after = self.latest_for(self.c1["id"], "Farm-C")
        self.assertEqual(note_after["id"], note["id"])
        self.assertEqual(note_after["status"], "acknowledged")


class EffectiveTimeGraphTest(LedgerTestBase):
    def test_graph_respects_effective_time(self):
        edges = [
            {"id": "e1", "upstream": "A", "downstream": "B", "consignment_id": "c1",
             "effective_from": "2026-09-01T00:00:00+00:00", "effective_to": None},
            {"id": "e2", "upstream": "B", "downstream": "C", "consignment_id": "c2",
             "effective_from": "2026-09-10T00:00:00+00:00", "effective_to": None},
        ]
        early = trace_downstream_facilities(edges, "A", "2026-09-05T00:00:00+00:00")
        self.assertEqual([f["location"] for f in early], ["B"])
        later = trace_downstream_facilities(edges, "A", "2026-09-12T00:00:00+00:00")
        self.assertEqual([f["location"] for f in later], ["B", "C"])
        self.assertEqual(later[1]["consignment_ids"], ["c2"])

    def test_future_link_does_not_leak_into_earlier_trace(self):
        self.make_facility("Farm-B")
        self.make_facility("Farm-C")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B", effective="2026-09-01T00:00:00+00:00")
        c2 = self.make_batch("C-2", "Farm-B", "Farm-C", effective="2026-10-01T00:00:00+00:00")
        # Lab result effective in September: C-2's link (effective October)
        # must not appear downstream yet.
        run = self.positive(c1["id"], sample_id="S-time",
                            effective_at="2026-09-15T00:00:00+00:00")["run"]
        targets = {i["target_location"] for i in self.service.get_run_detail(run["id"])["items"]}
        self.assertEqual(targets, {"Farm-B"})

    def test_earliest_link_wins_for_same_pair(self):
        edges = [
            {"id": "e-new", "upstream": "A", "downstream": "B", "consignment_id": "c2",
             "effective_from": "2026-10-01T00:00:00+00:00", "effective_to": None},
            {"id": "e-old", "upstream": "A", "downstream": "B", "consignment_id": "c1",
             "effective_from": "2026-09-01T00:00:00+00:00", "effective_to": None},
        ]
        active = active_propagation_edges(edges, "2026-10-02T00:00:00+00:00")
        self.assertEqual({e["id"] for e in active}, {"e-old"})


class LabConflictTest(LedgerTestBase):
    def test_second_inspector_same_sample_keeps_field_record_as_conflict(self):
        farm = self.make_facility("Farm-B")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B")

        first = self.service.submit_lab_result(INSPECTOR_A, c1["id"], {
            "sample_id": "S-DUP", "pest_found": True, "pest_name": "mite",
            "finding": "eggs",
        })
        self.assertEqual(first["outcome"], "effective")

        second = self.service.submit_lab_result(INSPECTOR_B, c1["id"], {
            "sample_id": "S-DUP", "pest_found": False, "pest_name": "",
            "finding": "inspector saw no pest at sampling site",
        })
        self.assertEqual(second["outcome"], "conflict")
        self.assertIsNotNone(second["conflict"]["effective"])
        self.assertEqual(second["conflict"]["effective"]["submitted_by"], "inspector-a")
        retained = second["conflict"]["retained_record"]
        self.assertEqual(retained["submitted_by"], "inspector-b")
        self.assertFalse(retained["pest_found"])
        self.assertEqual(retained["finding"], "inspector saw no pest at sampling site")

        # Only the first result drives the ledger.
        submissions = self.service.list_lab_results(c1["id"])
        effective = [s for s in submissions if s["status"] == "effective"]
        conflicts = [s for s in submissions if s["status"] == "conflict"]
        self.assertEqual(len(effective), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["winner_id"], effective[0]["id"])

        # Conflict is listable through the query interface too.
        self.assertEqual(self.service.list_lab_results(sample_id="S-DUP", status="conflict"),
                         conflicts)

    def test_viewer_cannot_submit_results(self):
        c1 = self.make_batch("C-99", "Port-A", "Farm-B")
        with self.assertRaises(PermissionDenied):
            self.service.submit_lab_result(Actor("v", "viewer"), c1["id"], {
                "sample_id": "X", "pest_found": True,
            })

    def test_invalid_effective_time_rejected(self):
        c1 = self.make_batch("C-98", "Port-A", "Farm-B")
        with self.assertRaises(ValidationError):
            self.service.submit_lab_result(LAB, c1["id"], {
                "sample_id": "X", "pest_found": True, "effective_at": "not-a-date",
            })


class BreakpointResumeTest(LedgerTestBase):
    def test_failed_run_resumes_from_breakpoint_without_duplicate_notices(self):
        self.make_facility("Farm-B")
        self.make_facility("Farm-C")
        self.make_facility("Farm-D")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B", effective="2026-09-01T00:00:00+00:00")
        c2 = self.make_batch("C-2", "Farm-B", "Farm-C", effective="2026-09-02T00:00:00+00:00")
        c3 = self.make_batch("C-3", "Farm-C", "Farm-D", effective="2026-09-03T00:00:00+00:00")

        original = self.repo.issue_notification
        call_count = {"n": 0}

        def flaky(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:  # Farm-B succeeds, Farm-C dispatch fails
                raise RuntimeError("notification gateway down")
            return original(**kwargs)

        self.repo.issue_notification = flaky
        with self.assertRaises(LedgerProcessingError):
            self.positive(c1["id"], sample_id="S-br",
                          effective_at="2026-09-05T00:00:00+00:00")

        failed = self.service.list_runs(c1["id"], status="failed")
        self.assertEqual(len(failed), 1)
        run_id = failed[0]["id"]

        # First target was checkpointed with one version-1 notice.
        before = self.notifications(c1["id"])
        self.assertEqual([(n["target_location"], n["version_no"]) for n in before],
                         [("Farm-B", 1)])

        resumed = self.service.resume_run(run_id, ADMIN)
        self.assertEqual(resumed["status"], "completed")

        after = self.notifications(c1["id"])
        self.assertEqual(
            [(n["target_location"], n["version_no"]) for n in after],
            [("Farm-B", 1), ("Farm-C", 1), ("Farm-D", 1)],
        )
        # Idempotency checkpoint keeps resume from re-notifying Farm-B.
        self.assertEqual(call_count["n"], 4)  # B + failing C, then C + D
        self.repo.issue_notification = original

        # Resuming a completed run is a no-op.
        again = self.service.resume_run(run_id, ADMIN)
        self.assertEqual(again["status"], "completed")

    def test_invalidated_run_cannot_be_resumed(self):
        self.make_facility("Farm-B")
        self.make_facility("Farm-C")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B", effective="2026-09-01T00:00:00+00:00")
        self.make_batch("C-2", "Farm-B", "Farm-C", effective="2026-09-02T00:00:00+00:00")

        original = self.repo.issue_notification

        def fail_once(**kwargs):
            raise RuntimeError("gateway down")

        self.repo.issue_notification = fail_once
        with self.assertRaises(LedgerProcessingError):
            self.positive(c1["id"], sample_id="S-a",
                          effective_at="2026-09-05T00:00:00+00:00")
        self.repo.issue_notification = original

        failed = self.service.list_runs(c1["id"], status="failed")
        self.assertEqual(len(failed), 1)
        run_id = failed[0]["id"]

        # Late result invalidates the still-unfinished run before it resumes.
        self.positive(c1["id"], sample_id="S-b",
                      effective_at="2026-10-02T00:00:00+00:00")
        with self.assertRaises(ConflictError):
            self.service.resume_run(run_id, ADMIN)


class NegativeResultTest(LedgerTestBase):
    def test_late_negative_result_invalidates_without_new_notices(self):
        self.make_facility("Farm-B")
        self.make_facility("Farm-C")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B", effective="2026-09-01T00:00:00+00:00")
        self.make_batch("C-2", "Farm-B", "Farm-C", effective="2026-09-02T00:00:00+00:00")

        positive_run = self.positive(c1["id"], sample_id="S-p",
                                     effective_at="2026-09-10T00:00:00+00:00")["run"]
        self.assertEqual(len(self.notifications(c1["id"])), 2)

        negative = self.service.submit_lab_result(LAB, c1["id"], {
            "sample_id": "S-n", "pest_found": False,
            "finding": "sample retested negative",
            "effective_at": "2026-09-20T00:00:00+00:00",
        })
        self.assertEqual(negative["outcome"], "effective")
        run = negative["run"]
        self.assertFalse(run["pest_found"])
        self.assertEqual(run["status"], "completed")
        detail = self.service.get_run_detail(run["id"])
        self.assertEqual(detail["items"], [])

        # Unconfirmed positive notices are voided; no new positive notices.
        statuses = sorted(n["status"] for n in self.notifications(c1["id"]))
        self.assertEqual(statuses, ["voided", "voided"])
        # Old positive conclusion remains queryable.
        self.assertEqual(
            self.service.get_run_detail(positive_run["id"])["status"], "completed"
        )


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "old.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _build_legacy_db(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, data TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                actor_id TEXT NOT NULL, idem_key TEXT NOT NULL, entity_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(actor_id, idem_key)
            );
        """)
        import json
        connection.execute(
            "INSERT INTO entities VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            ("old-1", "consignment", "quarantined",
             json.dumps({"code": "OLD-1", "origin": "OldPort", "destination": "OldFarm"}),
             "legacy", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
            "to_status, detail, created_at) VALUES ('old-1','legacy','admin','create',"
            "NULL,'declared','{}','2026-01-01T00:00:00+00:00')"
        )
        connection.commit()
        connection.close()

    def test_upgrade_adds_tables_and_backfills_links_from_origin_destination(self):
        self._build_legacy_db()
        repo = SQLiteRepository(self.db_path)
        self.assertEqual(repo._connect().execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)

        links = repo.list_links()
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link["upstream"], "OldPort")
        self.assertEqual(link["downstream"], "OldFarm")
        self.assertEqual(link["consignment_id"], "old-1")
        self.assertEqual(link["source"], "migration")
        self.assertEqual(link["effective_from"], "2026-01-01T00:00:00+00:00")

        # Historical entity and audit records still open.
        service = DomainService(repo, RuleEngine())
        entity = service.get("old-1")
        self.assertEqual(entity["data"]["code"], "OLD-1")
        self.assertEqual(service.audit_log("old-1")[0]["action"], "create")

        # Backfill is idempotent on restart.
        repo2 = SQLiteRepository(self.db_path)
        self.assertEqual(len(repo2.list_links()), 1)

    def test_manual_backfill_is_admin_only(self):
        self._build_legacy_db()
        repo = SQLiteRepository(self.db_path)
        service = DomainService(repo, RuleEngine())
        with self.assertRaises(PermissionDenied):
            service.backfill_links(Actor("i", "inspector"))
        # Startup migration already backfilled; deleting the link lets an
        # operator-triggered backfill restore it from origin/destination.
        self.assertEqual(service.backfill_links(ADMIN)["added"], 0)
        with repo._connect() as connection:
            connection.execute("DELETE FROM propagation_links")
        self.assertEqual(service.backfill_links(ADMIN)["added"], 1)
        self.assertEqual(service.backfill_links(ADMIN)["added"], 0)


class NotificationQueryTest(LedgerTestBase):
    def test_voided_filter_and_ack_endpoint(self):
        self.make_facility("Farm-B")
        c1 = self.make_batch("C-1", "Port-A", "Farm-B")
        self.positive(c1["id"], sample_id="S-1")
        self.positive(c1["id"], sample_id="S-2", effective_at="2026-10-03T00:00:00+00:00")
        all_notes = self.service.list_notifications(consignment_id=c1["id"])
        self.assertEqual(len(all_notes), 2)
        live = self.service.list_notifications(consignment_id=c1["id"], include_void=False)
        self.assertEqual([n["status"] for n in live], ["issued"])

        note = live[0]
        acked = self.service.acknowledge_notification(QUAR, note["id"], note="ok")
        self.assertEqual(acked["acknowledged_by"], "quar-1")
        with self.assertRaises(ConflictError):
            self.service.acknowledge_notification(
                QUAR, all_notes[0]["id"]
            )


if __name__ == "__main__":
    unittest.main()
