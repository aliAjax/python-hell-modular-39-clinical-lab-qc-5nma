import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, bottle_expires_at
from src.service import DomainService


class BottleWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "bottles.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator_a = Actor("operator-a", "operator")
        self.operator_b = Actor("operator-b", "operator")
        assay, lot, instrument = self._base(total_bottles=4)
        self.assay = assay
        self.lot = lot
        self.instrument = instrument

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self, total_bottles=4):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {
                "assay_id": assay["id"],
                "lot_no": "LOT-2026-10",
                "target": 5.0,
                "sd": 0.1,
                "expires_at": "2026-12-31",
                "total_bottles": total_bottles,
                "open_vial_days": 7,
            },
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, lot, instrument

    def _dispense(self, order_no="DO-1", bottles=None):
        bottles = bottles or [
            {"bottle_no": "B1", "opened_at": "2026-10-01T08:00:00Z"},
            {"bottle_no": "B2", "opened_at": "2026-10-01T09:00:00Z", "expires_at": "2026-10-03T09:00:00Z"},
            {"bottle_no": "B3", "opened_at": "2026-10-02"},
        ]
        return self.service.dispense_lot(self.supervisor, self.lot["id"], order_no, bottles)

    def test_dispense_records_total_and_computes_expiry(self):
        result = self._dispense()
        self.assertEqual(len(result["bottles"]), 3)
        by_no = {b["data"]["bottle_no"]: b for b in result["bottles"]}
        self.assertEqual(by_no["B1"]["data"]["expires_at"], "2026-10-08T08:00:00Z")
        # explicit expiry overrides the open-vial window
        self.assertEqual(by_no["B2"]["data"]["expires_at"], "2026-10-03T09:00:00Z")
        # date-only opening stays date-only and is capped by lot expiry
        self.assertEqual(by_no["B3"]["data"]["expires_at"], "2026-10-09")
        order = self.service.list("dispense_orders")[0]
        self.assertEqual(order["data"]["bottle_count"], 3)
        # accounting: the 4th bottle still fits the received total
        self._dispense(
            "DO-2", [{"bottle_no": "B4", "opened_at": "2026-10-02T08:00:00Z"}]
        )
        with self.assertRaises(ConflictError):
            self.service.dispense_lot(
                self.supervisor,
                self.lot["id"],
                "DO-3",
                [{"bottle_no": "B5", "opened_at": "2026-10-02T08:00:00Z"}],
            )

    def test_dispense_order_retry_does_not_double_book(self):
        bottles = [{"bottle_no": "B1", "opened_at": "2026-10-01T08:00:00Z"}]
        first = self.service.dispense_lot(self.supervisor, self.lot["id"], "DO-IDEM", bottles)
        second = self.service.dispense_lot(self.supervisor, self.lot["id"], "DO-IDEM", bottles)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        self.assertEqual(len(self.service.list("working_bottles")), 1)
        self.assertEqual(len(self.service.list("dispense_orders")), 1)
        audits = [a for a in self.service.audit_log() if a["action"] == "dispense"]
        self.assertEqual(len(audits), 1)

    def test_last_bottle_concurrent_claim_single_winner(self):
        result = self._dispense(
            "DO-ONLY", [{"bottle_no": "ONLY", "opened_at": "2026-10-01T08:00:00Z"}]
        )
        bottle = result["bottles"][0]
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(actor, claim_no):
            barrier.wait()
            try:
                outcomes.append(
                    self.service.claim_bottle(actor, bottle["id"], self.instrument["id"], claim_no)
                )
            except ConflictError as exc:
                outcomes.append(exc)

        t1 = threading.Thread(target=claim, args=(self.operator_a, "CLAIM-A"))
        t2 = threading.Thread(target=claim, args=(self.operator_b, "CLAIM-B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        fulfilled = [o for o in outcomes if not isinstance(o, Exception)]
        conflicts = [o for o in outcomes if isinstance(o, ConflictError)]
        self.assertEqual(len(fulfilled), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("already assigned", str(conflicts[0]))
        fresh = self.service.get(bottle["id"])
        self.assertEqual(fresh["status"], "in_use")
        # the loser can see who holds the bottle
        self.assertEqual(fresh["data"]["instrument_id"], self.instrument["id"])
        self.assertIn(fresh["data"]["claimed_by"], ("operator-a", "operator-b"))

    def test_capacity_overflow_queues_and_promotes(self):
        instrument2 = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer B", "serial": "B-200", "calibration_due": "2099-01-01"},
        )
        self.service.create(
            self.supervisor,
            "instrument_assay",
            {
                "instrument_id": self.instrument["id"],
                "assay_id": self.assay["id"],
                "bottle_capacity": 1,
            },
        )
        result = self._dispense(
            "DO-Q",
            [
                {"bottle_no": "Q1", "opened_at": "2026-10-01T08:00:00Z"},
                {"bottle_no": "Q2", "opened_at": "2026-10-01T08:00:00Z"},
                {"bottle_no": "Q3", "opened_at": "2026-10-01T08:00:00Z"},
            ],
        )
        first = self.service.request_bottle(
            self.operator_a, self.instrument["id"], self.assay["id"], "REQ-1"
        )
        self.assertEqual(first["request_status"], "fulfilled")
        second = self.service.request_bottle(
            self.operator_b, self.instrument["id"], self.assay["id"], "REQ-2"
        )
        self.assertEqual(second["request_status"], "queued")
        # a different instrument with its own slot is unaffected
        other = self.service.request_bottle(
            self.operator_a, instrument2["id"], self.assay["id"], "REQ-OTHER"
        )
        self.assertEqual(other["request_status"], "fulfilled")
        # consume the held bottle: the queued request is auto-promoted in FIFO order
        consume = self.service.consume_bottle(self.supervisor, first["assigned_bottle_id"], "empty")
        self.assertEqual(consume["bottle"]["status"], "consumed")
        promoted_ids = [item["claim_id"] for item in consume["promoted"]]
        self.assertIn(second["claim"]["id"], promoted_ids)
        queued = self.service.list("bottle_claims", status="queued")
        self.assertEqual(queued, [])

    def test_expiry_recomputes_inflight_but_preserves_released(self):
        result = self._dispense(
            "DO-E", [{"bottle_no": "E1", "opened_at": "2026-10-01T08:00:00Z",
                      "expires_at": "2026-10-08T08:00:00Z"}]
        )
        bottle = result["bottles"][0]
        self.service.claim_bottle(
            self.operator_a, bottle["id"], self.instrument["id"], "CLAIM-E"
        )
        # in-flight run: accepted but its patient batch is still waiting
        inflight = self.service.create(
            self.operator_a,
            "qc_run",
            {
                "assay_id": self.assay["id"],
                "qc_lot_id": self.lot["id"],
                "instrument_id": self.instrument["id"],
                "value": 5.01,
                "run_at": "2026-10-02T08:00:00Z",
                "working_bottle_id": bottle["id"],
            },
        )
        inflight = self.service.transition(
            self.supervisor, inflight["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(inflight["status"], "accepted")
        waiting_batch = self.service.create(
            self.operator_a,
            "result_batch",
            {
                "assay_id": self.assay["id"],
                "instrument_id": self.instrument["id"],
                "qc_run_id": inflight["id"],
                "run_at": "2026-10-02T09:00:00Z",
                "patient_count": 3,
            },
        )
        # already released batch on an earlier run keeps its original basis
        released_run = self.service.create(
            self.operator_a,
            "qc_run",
            {
                "assay_id": self.assay["id"],
                "qc_lot_id": self.lot["id"],
                "instrument_id": self.instrument["id"],
                "value": 5.00,
                "run_at": "2026-10-01T08:00:00Z",
                "working_bottle_id": bottle["id"],
            },
        )
        released_run = self.service.transition(
            self.supervisor, released_run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        released_batch = self.service.create(
            self.operator_a,
            "result_batch",
            {
                "assay_id": self.assay["id"],
                "instrument_id": self.instrument["id"],
                "qc_run_id": released_run["id"],
                "run_at": "2026-10-01T09:00:00Z",
                "patient_count": 5,
            },
        )
        released_batch = self.service.transition(
            self.supervisor, released_batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        basis_before = dict(released_batch["data"]["release_basis"])

        outcome = self.service.expire_bottle(
            self.supervisor, bottle["id"], reason="open-vial stability exceeded",
            as_of="2026-10-08T08:00:01Z",
        )
        self.assertIn(inflight["id"], outcome["redo_required_run_ids"])
        self.assertNotIn(released_run["id"], outcome["redo_required_run_ids"])
        self.assertEqual(outcome["preserved_released_batch_ids"], [released_batch["id"]])
        held = self.service.get(waiting_batch["id"])
        self.assertEqual(held["status"], "intercepted")
        self.assertIn("expired", held["data"]["intercept_reason"])
        redone = self.service.get(inflight["id"])
        self.assertEqual(redone["status"], "redo_required")
        self.assertEqual(redone["data"]["invalidated_bottle_id"], bottle["id"])
        # released patient batch retains status and original evidence
        preserved = self.service.get(released_batch["id"])
        self.assertEqual(preserved["status"], "released")
        self.assertEqual(preserved["data"]["release_basis"], basis_before)
        self.assertEqual(preserved["data"]["release_basis"]["working_bottle_id"], bottle["id"])

    def test_sweep_expires_all_due_bottles(self):
        self._dispense(
            "DO-S",
            [
                {"bottle_no": "S1", "opened_at": "2026-09-20T08:00:00Z"},
                {"bottle_no": "S2", "opened_at": "2026-10-03T08:00:00Z"},
            ],
        )
        sweep = self.service.sweep_expired_bottles(self.supervisor, as_of="2026-10-04T00:00:00Z")
        self.assertEqual(sweep["expired_count"], 1)
        statuses = {b["data"]["bottle_no"]: b["status"] for b in self.service.list("working_bottles")}
        self.assertEqual(statuses["S1"], "expired")
        self.assertEqual(statuses["S2"], "in_stock")

    def test_run_after_expiry_is_rejected_and_redo_retest_path(self):
        result = self._dispense(
            "DO-X",
            [
                {"bottle_no": "X1", "opened_at": "2026-10-01T08:00:00Z"},
                {"bottle_no": "X2", "opened_at": "2026-10-09T08:00:00Z"},
            ],
        )
        bottles = {b["data"]["bottle_no"]: b for b in result["bottles"]}
        bottle = bottles["X1"]
        self.service.claim_bottle(self.operator_a, bottle["id"], self.instrument["id"], "CLAIM-X1")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.operator_a,
                "qc_run",
                {
                    "assay_id": self.assay["id"],
                    "qc_lot_id": self.lot["id"],
                    "instrument_id": self.instrument["id"],
                    "value": 5.0,
                    "run_at": "2026-10-09T00:00:00Z",
                    "working_bottle_id": bottle["id"],
                },
            )
        run = self.service.create(
            self.operator_a,
            "qc_run",
            {
                "assay_id": self.assay["id"],
                "qc_lot_id": self.lot["id"],
                "instrument_id": self.instrument["id"],
                "value": 5.01,
                "run_at": "2026-10-02T08:00:00Z",
                "working_bottle_id": bottle["id"],
            },
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})
        batch = self.service.create(
            self.operator_a,
            "result_batch",
            {
                "assay_id": self.assay["id"],
                "instrument_id": self.instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-10-02T09:00:00Z",
                "patient_count": 1,
            },
        )
        outcome = self.service.expire_bottle(
            self.supervisor, bottle["id"], reason="expired", as_of="2026-10-09T00:00:01Z"
        )
        self.assertEqual(outcome["redo_required_run_ids"], [run["id"]])
        self.assertEqual(self.service.get(batch["id"])["status"], "intercepted")
        # redo on a fresh bottle: replacement run accepted -> run and batch retest -> release
        replacement_bottle = bottles["X2"]
        self.service.claim_bottle(
            self.operator_a, replacement_bottle["id"], self.instrument["id"], "CLAIM-X2"
        )
        redo = self.service.create(
            self.operator_a,
            "qc_run",
            {
                "assay_id": self.assay["id"],
                "qc_lot_id": self.lot["id"],
                "instrument_id": self.instrument["id"],
                "value": 5.00,
                "run_at": "2026-10-09T09:00:00Z",
                "working_bottle_id": replacement_bottle["id"],
            },
        )
        redo = self.service.transition(self.supervisor, redo["id"], "evaluate", {"evaluated_by": "qc-1"})
        self.assertEqual(redo["status"], "accepted")
        retested_run = self.service.transition(
            self.supervisor,
            run["id"],
            "retest",
            {"replacement_run_id": redo["id"], "reason": "working bottle expired"},
        )
        self.assertEqual(retested_run["status"], "retesting")
        retested_batch = self.service.transition(
            self.supervisor,
            batch["id"],
            "retest",
            {"replacement_run_id": redo["id"], "reason": "redo after bottle expiry"},
        )
        self.assertEqual(retested_batch["status"], "waiting")
        self.assertEqual(retested_batch["data"]["qc_run_id"], redo["id"])
        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_basis"]["bottle_no"], "X2")
        self.assertEqual(released["data"]["previous_qc_run_id"], run["id"])

    def test_claim_retry_is_idempotent(self):
        result = self._dispense(
            "DO-C", [{"bottle_no": "C1", "opened_at": "2026-10-01T08:00:00Z"}]
        )
        bottle = result["bottles"][0]
        first = self.service.claim_bottle(
            self.operator_a, bottle["id"], self.instrument["id"], "CLAIM-IDEM"
        )
        second = self.service.claim_bottle(
            self.operator_a, bottle["id"], self.instrument["id"], "CLAIM-IDEM"
        )
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["claim"]["id"], second["claim"]["id"])
        self.assertEqual(len(self.service.list("bottle_claims")), 1)


class LegacyDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "legacy.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_lot_without_total_bottles_and_runs_without_bottle(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "ALT", "unit": "U/L", "allowed_low": 0, "allowed_high": 40},
        )
        # old lots carry no total_bottles/open_vial_days: dispense is still allowed
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "OLD-LOT", "target": 20, "sd": 1,
             "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Old Analyzer", "serial": "O-1", "calibration_due": "2099-01-01"},
        )
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": 20.2,
                "run_at": "2026-09-27T08:00:00Z",
            },
        )
        self.assertIsNone(run["data"].get("working_bottle_id"))
        # history is still searchable by the original lot number
        hits = self.service.list("qc_runs", filters={"qc_lot_id": lot["id"]})
        self.assertEqual([h["id"] for h in hits], [run["id"]])
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})
        self.assertEqual(run["status"], "accepted")
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 2,
            },
        )
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["release_basis"]["qc_lot_id"], lot["id"])
        self.assertIsNone(batch["data"]["release_basis"]["working_bottle_id"])
        # legacy lot can still be dispensed with computed default expiry
        result = self.service.dispense_lot(
            self.supervisor,
            lot["id"],
            "OLD-DO",
            [{"bottle_no": "W1", "opened_at": "2026-10-01T08:00:00Z"}],
        )
        self.assertEqual(result["bottles"][0]["data"]["expires_at"], "2026-10-08T08:00:00Z")


class ExpiryRuleTest(unittest.TestCase):
    def test_open_vial_window_capped_by_lot(self):
        self.assertEqual(
            bottle_expires_at("2026-10-05", "2026-10-01T08:00:00Z", 7),
            "2026-10-05T00:00:00Z",
        )
        self.assertEqual(
            bottle_expires_at("2026-12-31", "2026-10-01", 7),
            "2026-10-08",
        )
        with self.assertRaises(ValidationError):
            bottle_expires_at("2026-12-31", "2026-10-01", 0)
        with self.assertRaises(ValidationError):
            bottle_expires_at("2026-09-30", "2026-10-01", 7)


if __name__ == "__main__":
    unittest.main()
