from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .rules import RuleEngine


class DomainService:
    BOTTLE_ROLES = ("operator", "supervisor", "admin")

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _ensure_role(self, actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def _load_capacities(self):
        capacities = {}
        for binding in self.repository.list_entities(kind="instrument_assay"):
            capacities[binding["data"]["scope"]] = int(binding["data"].get("bottle_capacity", 1))
        return capacities

    def dispense_lot(self, actor, lot_id, order_no, bottles):
        """Split a received lot into numbered working bottles from one order."""
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.dispense_lot(
            actor, order_no, lot_id, bottles, capacities=self._load_capacities()
        )

    def request_bottle(self, actor, instrument_id, assay_id, claim_no):
        """Request the next bottle for an instrument/assay; queue when full."""
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.request_bottle(
            actor, claim_no, instrument_id, assay_id, capacities=self._load_capacities()
        )

    def claim_bottle(self, actor, bottle_id, instrument_id, claim_no):
        """Claim a specific bottle; a competing loser sees it occupied."""
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.claim_bottle(
            actor, claim_no, bottle_id, instrument_id, capacities=self._load_capacities()
        )

    def consume_bottle(self, actor, bottle_id, reason=""):
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.consume_bottle(actor, bottle_id, reason=reason)

    def expire_bottle(self, actor, bottle_id, reason="expired", as_of=None):
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.expire_bottle(actor, bottle_id, reason, as_of=as_of)

    def sweep_expired_bottles(self, actor, as_of=None):
        self._ensure_role(actor, self.BOTTLE_ROLES)
        return self.repository.sweep_expired_bottles(actor, as_of=as_of)

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

    def list(self, kind=None, status=None, filters=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status, filters=filters)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
