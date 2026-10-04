from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # --- working bottle operations ---

    def aliquot_lot(self, actor, lot_id, data):
        """Split a QC lot into working bottles.

        Bottle IDs are deterministic so retries after a write failure
        skip already-created bottles (no double-occupancy or
        double-bookkeeping).
        """
        lot = self.repository.get_entity(lot_id)
        if not lot:
            raise NotFoundError("qc lot not found: " + lot_id)
        self.rules.validate_transition(
            actor, lot, "aliquot", dict(data or {}), self._lookup
        )
        count = int(data["count"])
        opened_at = data["opened_at"]
        expires_at = data["expires_at"]
        created = self.repository.aliquot_bottles(
            lot_id, count, opened_at, expires_at, actor.user_id
        )
        self.audit.record(
            lot_id, actor, "aliquot", lot["status"], lot["status"],
            {"count": count, "created": created},
        )
        return {"lot_id": lot_id, "count": count, "created": created}

    def claim_bottle(self, actor, bottle_id, data):
        """Claim a working bottle for an instrument.

        Atomic: two concurrent claims for the last bottle cannot both
        succeed.  The loser sees a ConflictError because the bottle is
        no longer ``in_storage``.
        """
        bottle = self.repository.get_entity(bottle_id)
        if not bottle:
            raise NotFoundError("bottle not found: " + bottle_id)
        if bottle["kind"] != "working_bottle":
            raise ValidationError("entity is not a working bottle: " + bottle_id)
        instrument_id = data.get("instrument_id")
        if not instrument_id:
            raise ValidationError("instrument_id is required")
        updated = self.repository.claim_bottle(bottle_id, instrument_id, actor.user_id)
        self.audit.record(
            bottle_id, actor, "claim", bottle["status"], updated["status"],
            {"instrument_id": instrument_id},
        )
        return updated

    def release_bottle(self, actor, bottle_id):
        """Return a working bottle to storage, freeing capacity.

        Promotes the oldest queued bottle for the same
        (instrument, assay).
        """
        bottle = self.repository.get_entity(bottle_id)
        if not bottle:
            raise NotFoundError("bottle not found: " + bottle_id)
        if bottle["kind"] != "working_bottle":
            raise ValidationError("entity is not a working bottle: " + bottle_id)
        updated, promoted = self.repository.release_bottle(bottle_id, actor.user_id)
        self.audit.record(
            bottle_id, actor, "release", bottle["status"], updated["status"],
            {"promoted": promoted},
        )
        return updated

    def expire_bottle(self, actor, bottle_id):
        """Mark a working bottle as expired.

        Recalculates in-transit QC runs that reference this bottle and
        returns them for redo.  Released patient result batches keep
        their original basis and are not touched.
        """
        bottle = self.repository.get_entity(bottle_id)
        if not bottle:
            raise NotFoundError("bottle not found: " + bottle_id)
        if bottle["kind"] != "working_bottle":
            raise ValidationError("entity is not a working bottle: " + bottle_id)
        run_ids = self.repository.expire_bottle(bottle_id)
        affected = []
        for run_id in run_ids:
            run = self.repository.get_entity(run_id)
            linked_released = any(
                b["status"] == "released"
                for b in self.repository.find_entities("result_batch", "qc_run_id", run_id)
            )
            if linked_released:
                affected.append({
                    "run_id": run_id,
                    "action": "skipped",
                    "reason": "released batch keeps original basis",
                })
                continue
            if run["status"] == "pending":
                evaluated = self.transition(
                    actor, run_id, "evaluate",
                    {"evaluated_by": actor.user_id, "reason": "bottle expired"},
                )
                if evaluated["status"] == "accepted":
                    self._mark_run_rejected(run_id, "bottle expired after evaluation")
                    affected.append({
                        "run_id": run_id,
                        "action": "rejected",
                        "reason": "bottle expired",
                    })
                else:
                    affected.append({
                        "run_id": run_id,
                        "action": "kept",
                        "status": evaluated["status"],
                    })
            elif run["status"] in ("rejected", "retesting", "investigated"):
                self._add_run_note(run_id, "bottle expired")
                affected.append({
                    "run_id": run_id,
                    "action": "noted",
                    "status": run["status"],
                })
            else:
                # accepted/resolved/corrected — not in transit, keep as is
                affected.append({
                    "run_id": run_id,
                    "action": "skipped",
                    "reason": "run is not in transit",
                })
        self.audit.record(
            bottle_id, actor, "expire", bottle["status"], "expired",
            {"affected": affected},
        )
        return {"bottle_id": bottle_id, "affected": affected}

    def check_expirations(self, actor):
        """Expire all bottles past their ``expires_at``."""
        bottles = self.repository.find_expired_bottles()
        results = []
        for bottle in bottles:
            result = self.expire_bottle(actor, bottle["id"])
            results.append(result)
        return {"checked": len(bottles), "results": results}

    def _mark_run_rejected(self, run_id, reason):
        run = self.repository.get_entity(run_id)
        data = dict(run["data"])
        data["reject_reason"] = reason
        data["bottle_expired"] = True
        self.repository.update_entity(run_id, run["version"], "rejected", data)

    def _add_run_note(self, run_id, note):
        run = self.repository.get_entity(run_id)
        data = dict(run["data"])
        notes = list(data.get("notes") or [])
        notes.append(note)
        data["notes"] = notes
        self.repository.update_entity(run_id, run["version"], run["status"], data)
