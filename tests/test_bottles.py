import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BottleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "bottles.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator = Actor("qc-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_lot(self, total=10):
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
                "lot_no": "LOT-1",
                "target": 5.0,
                "sd": 0.1,
                "expires_at": "2099-01-01",
                "total_bottles": total,
            },
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        return assay, lot

    def _setup_instrument(self, capacity=1):
        return self.service.create(
            self.supervisor,
            "instrument",
            {
                "name": "Analyzer A",
                "serial": "A-100",
                "calibration_due": "2099-01-01",
                "bottle_capacity": capacity,
            },
        )

    def test_aliquot_creates_bottles_with_details(self):
        assay, lot = self._setup_lot(total=5)
        result = self.service.aliquot_lot(
            self.operator,
            lot["id"],
            {"count": 3, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        self.assertEqual(len(result["created"]), 3)
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        self.assertEqual(len(bottles), 3)
        for i, bottle in enumerate(bottles, start=1):
            self.assertEqual(bottle["status"], "in_storage")
            self.assertEqual(bottle["data"]["bottle_no"], "LOT-1-%03d" % i)
            self.assertEqual(bottle["data"]["opened_at"], "2026-10-01")
            self.assertEqual(bottle["data"]["expires_at"], "2026-10-31T23:59:59Z")
            self.assertIsNone(bottle["data"]["instrument_id"])
        # lot aliquoted_count updated
        lot = self.service.get(lot["id"])
        self.assertEqual(lot["data"]["aliquoted_count"], 3)

    def test_aliquot_cannot_exceed_total(self):
        assay, lot = self._setup_lot(total=2)
        with self.assertRaises(ValidationError):
            self.service.aliquot_lot(
                self.operator,
                lot["id"],
                {"count": 3, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
            )

    def test_claim_sets_in_use(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 2, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        bottle = self.service.claim_bottle(
            self.operator, bottles[0]["id"], {"instrument_id": instrument["id"]}
        )
        self.assertEqual(bottle["status"], "in_use")
        self.assertEqual(bottle["data"]["instrument_id"], instrument["id"])
        self.assertEqual(bottle["data"]["claimed_by"], "qc-operator")

    def test_claim_when_at_capacity_queues(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 3, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        # First claim → in_use
        first = self.service.claim_bottle(
            self.operator, bottles[0]["id"], {"instrument_id": instrument["id"]}
        )
        self.assertEqual(first["status"], "in_use")
        # Second claim → queued (capacity full)
        second = self.service.claim_bottle(
            self.operator, bottles[1]["id"], {"instrument_id": instrument["id"]}
        )
        self.assertEqual(second["status"], "queued")
        # Third claim → queued
        third = self.service.claim_bottle(
            self.operator, bottles[2]["id"], {"instrument_id": instrument["id"]}
        )
        self.assertEqual(third["status"], "queued")

    def test_release_promotes_queued_bottle(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 3, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        first = self.service.claim_bottle(
            self.operator, bottles[0]["id"], {"instrument_id": instrument["id"]}
        )
        self.service.claim_bottle(
            self.operator, bottles[1]["id"], {"instrument_id": instrument["id"]}
        )
        # Release first → second should be promoted to in_use
        self.service.release_bottle(self.operator, first["id"])
        second = self.service.get(bottles[1]["id"])
        self.assertEqual(second["status"], "in_use")
        self.assertEqual(second["data"]["instrument_id"], instrument["id"])

    def test_concurrent_claim_last_bottle_only_one_succeeds(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 1, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        bottle_id = bottles[0]["id"]
        # First claim succeeds
        first = self.service.claim_bottle(
            self.operator, bottle_id, {"instrument_id": instrument["id"]}
        )
        self.assertEqual(first["status"], "in_use")
        # Second claim fails — bottle already occupied
        with self.assertRaises(ConflictError):
            self.service.claim_bottle(
                self.operator, bottle_id, {"instrument_id": instrument["id"]}
            )

    def test_aliquot_retry_no_duplicate_bottles(self):
        assay, lot = self._setup_lot(total=5)
        data = {"count": 3, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"}
        first = self.service.aliquot_lot(self.operator, lot["id"], data)
        self.assertEqual(len(first["created"]), 3)
        # Retry with same params — no new bottles created
        second = self.service.aliquot_lot(self.operator, lot["id"], data)
        self.assertEqual(len(second["created"]), 0)
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        self.assertEqual(len(bottles), 3)
        # aliquoted_count unchanged
        lot = self.service.get(lot["id"])
        self.assertEqual(lot["data"]["aliquoted_count"], 3)

    def test_expire_bottle_recalculates_in_transit_runs(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 1, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        bottle = self.service.claim_bottle(
            self.operator, bottles[0]["id"], {"instrument_id": instrument["id"]}
        )
        # Create a pending QC run referencing the bottle
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "bottle_id": bottle["id"],
                "value": 5.02,
                "run_at": "2026-10-02T08:00:00Z",
            },
        )
        self.assertEqual(run["status"], "pending")
        # Expire the bottle
        result = self.service.expire_bottle(self.operator, bottle["id"])
        self.assertEqual(result["bottle_id"], bottle["id"])
        self.assertEqual(len(result["affected"]), 1)
        self.assertEqual(result["affected"][0]["action"], "rejected")
        # Run should be rejected (needs redo)
        run = self.service.get(run["id"])
        self.assertEqual(run["status"], "rejected")
        self.assertTrue(run["data"].get("bottle_expired"))

    def test_expire_bottle_skips_released_batches(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 1, "opened_at": "2026-10-01", "expires_at": "2026-10-31T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        bottle = self.service.claim_bottle(
            self.operator, bottles[0]["id"], {"instrument_id": instrument["id"]}
        )
        # Create and accept a QC run
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "bottle_id": bottle["id"],
                "value": 5.02,
                "run_at": "2026-10-02T08:00:00Z",
            },
        )
        run = self.service.transition(
            self.operator, run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(run["status"], "accepted")
        # Create and release a result batch
        batch = self.service.create(
            self.operator,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-10-02T08:05:00Z",
                "patient_count": 10,
            },
        )
        batch = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"}
        )
        self.assertEqual(batch["status"], "released")
        # Expire the bottle — run linked to released batch should be skipped
        result = self.service.expire_bottle(self.operator, bottle["id"])
        self.assertEqual(len(result["affected"]), 1)
        self.assertEqual(result["affected"][0]["action"], "skipped")
        # Run status unchanged
        run = self.service.get(run["id"])
        self.assertEqual(run["status"], "accepted")

    def test_legacy_qc_run_without_bottle_id_still_works(self):
        """Legacy data: qc_run without bottle_id should still evaluate."""
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        # Create a run without bottle_id (legacy)
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": 5.02,
                "run_at": "2026-10-02T08:00:00Z",
            },
        )
        self.assertNotIn("bottle_id", run["data"])
        run = self.service.transition(
            self.operator, run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(run["status"], "accepted")

    def test_expire_all_checks_past_due_bottles(self):
        assay, lot = self._setup_lot(total=5)
        instrument = self._setup_instrument(capacity=1)
        self.service.aliquot_lot(
            self.operator, lot["id"],
            {"count": 2, "opened_at": "2026-09-01", "expires_at": "2026-09-30T23:59:59Z"},
        )
        bottles = self.service.repository.find_bottles_by_lot(lot["id"])
        # Both bottles are past due (expires_at 2026-09-30 < now 2026-10-04)
        result = self.service.check_expirations(self.operator)
        self.assertEqual(result["checked"], 2)
        for bottle in bottles:
            bottle = self.service.get(bottle["id"])
            self.assertEqual(bottle["status"], "expired")


if __name__ == "__main__":
    unittest.main()
