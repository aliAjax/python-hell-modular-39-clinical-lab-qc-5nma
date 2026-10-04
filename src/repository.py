import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError, ValidationError
from .rules import _parse_iso, bottle_expires_at


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entity_from_row(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "version": int(row["version"]),
        "data": json.loads(row["data"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS unique_keys (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                );
            """)

    def get_entity(self, entity_id, connection=None):
        sql = "SELECT * FROM entities WHERE id = ?"
        params = (entity_id,)
        if connection is not None:
            row = connection.execute(sql, params).fetchone()
        else:
            with self._connect() as new_connection:
                row = new_connection.execute(sql, params).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, filters=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        items = [_entity_from_row(row) for row in rows]
        for field, value in (filters or {}).items():
            items = [item for item in items if item["data"].get(field) == value]
        return items

    def find_entities(self, kind, field, value):
        if field == "id":
            entity = self.get_entity(value)
            return [entity] if entity and (not kind or entity["kind"] == kind) else []
        return [entity for entity in self.list_entities(kind=kind) if entity["data"].get(field) == value]

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            self._insert_audit(
                connection, entity_id, actor_id, actor_role, action, from_status, to_status, detail
            )
            connection.commit()

    @staticmethod
    def _insert_audit(connection, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------------
    # working-bottle lifecycle operations (single-statement transactions)
    # ------------------------------------------------------------------

    @staticmethod
    def _insert_entity(connection, entity_id, kind, status, data, actor_id):
        now = utcnow()
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (
                entity_id,
                kind,
                status,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                actor_id,
                now,
                now,
            ),
        )

    @staticmethod
    def _save_entity(connection, entity, status=None, patch=None):
        data = dict(entity["data"])
        if patch:
            data.update(patch)
        now = utcnow()
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (
                status if status is not None else entity["status"],
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                now,
                entity["id"],
                entity["version"],
            ),
        )
        entity["version"] += 1
        entity["status"] = status if status is not None else entity["status"]
        entity["data"] = data
        entity["updated_at"] = now

    @staticmethod
    def _claim_unique_key(connection, scope, key, entity_id):
        try:
            connection.execute(
                "INSERT INTO unique_keys(scope, key, entity_id, created_at) VALUES (?, ?, ?, ?)",
                (scope, key, entity_id, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("%s already recorded: %s" % (scope, key))

    @staticmethod
    def _get_unique_key(connection, scope, key):
        row = connection.execute(
            "SELECT entity_id FROM unique_keys WHERE scope = ? AND key = ?", (scope, key)
        ).fetchone()
        return row["entity_id"] if row else None

    @staticmethod
    def _capacity_for(capacities, instrument_id, assay_id):
        if capacities is None:
            return 1
        return int(capacities.get("%s:%s" % (instrument_id, assay_id), 1))

    @staticmethod
    def _active_bottle_count(connection, instrument_id, assay_id):
        """Count in-use bottles: consumed/expired bottles free the slot."""
        return connection.execute(
            "SELECT COUNT(*) FROM entities WHERE kind = 'working_bottle' AND status = 'in_use' "
            "AND json_extract(data, '$.instrument_id') = ? "
            "AND json_extract(data, '$.assay_id') = ?",
            (instrument_id, assay_id),
        ).fetchone()[0]

    def _promote_queue(self, connection, assay_id, actor, capacities):
        """Hand stock bottles to queued requests while slots and stock last."""
        promoted = []
        while True:
            waiting = [
                _entity_from_row(row)
                for row in connection.execute(
                    "SELECT * FROM entities WHERE kind = 'bottle_claim' AND status = 'queued' "
                    "ORDER BY created_at, id"
                ).fetchall()
                if json.loads(row["data"]).get("assay_id") == assay_id
            ]
            if not waiting:
                break
            progress = False
            for claim in waiting:
                instrument_id = claim["data"]["instrument_id"]
                capacity = self._capacity_for(capacities, instrument_id, assay_id)
                active = self._active_bottle_count(connection, instrument_id, assay_id)
                if active >= capacity:
                    continue
                stock = connection.execute(
                    "SELECT * FROM entities WHERE kind = 'working_bottle' AND status = 'in_stock' "
                    "AND json_extract(data, '$.assay_id') = ? "
                    "ORDER BY json_extract(data, '$.expires_at'), created_at, id",
                    (assay_id,),
                ).fetchone()
                if not stock:
                    return promoted
                bottle = _entity_from_row(stock)
                self._save_entity(
                    connection,
                    bottle,
                    status="in_use",
                    patch={
                        "instrument_id": instrument_id,
                        "claimed_by": claim["data"].get("requested_by"),
                        "claim_id": claim["id"],
                    },
                )
                self._save_entity(
                    connection,
                    claim,
                    status="fulfilled",
                    patch={"working_bottle_id": bottle["id"], "fulfilled_at": utcnow()},
                )
                self._insert_audit(
                    connection,
                    bottle["id"],
                    actor.user_id,
                    actor.role,
                    "assign",
                    "in_stock",
                    "in_use",
                    {"claim_id": claim["id"], "instrument_id": instrument_id, "from_queue": True},
                )
                self._insert_audit(
                    connection,
                    claim["id"],
                    actor.user_id,
                    actor.role,
                    "fulfill",
                    "queued",
                    "fulfilled",
                    {"working_bottle_id": bottle["id"], "from_queue": True},
                )
                promoted.append({"claim_id": claim["id"], "working_bottle_id": bottle["id"]})
                progress = True
                break
            if not progress:
                break
        return promoted

    def dispense_lot(self, actor, order_no, lot_id, bottles, capacities=None):
        """Record one dispense order and split the lot into working bottles.

        Idempotent on ``order_no``: replaying the order after a failed write
        returns the recorded order without creating bottles or keys twice.
        """
        if not order_no or not str(order_no).strip():
            raise ValidationError("dispense_order_no is required")
        if not bottles:
            raise ValidationError("at least one bottle is required")
        normalized = []
        seen = set()
        for index, item in enumerate(bottles):
            bottle_no = item.get("bottle_no")
            if not bottle_no:
                raise ValidationError("bottle_no is required for bottle #%d" % (index + 1))
            bottle_no = str(bottle_no)
            if bottle_no in seen:
                raise ValidationError("duplicate bottle_no in order: " + bottle_no)
            seen.add(bottle_no)
            opened_at = item.get("opened_at")
            if not opened_at:
                raise ValidationError("opened_at is required for bottle " + bottle_no)
            normalized.append(
                {
                    "bottle_no": bottle_no,
                    "opened_at": str(opened_at),
                    "expires_at": item.get("expires_at"),
                }
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_id = self._get_unique_key(connection, "dispense_order_no", order_no)
            if existing_id:
                order = self.get_entity(existing_id, connection)
                connection.commit()
                return {
                    "order": order,
                    "bottles": [
                        _entity_from_row(row)
                        for row in connection.execute(
                            "SELECT * FROM entities WHERE kind = 'working_bottle' "
                            "AND json_extract(data, '$.dispense_order_no') = ? ORDER BY created_at, id",
                            (order_no,),
                        ).fetchall()
                    ],
                    "promoted": [],
                    "idempotent_replay": True,
                }
            lot = self.get_entity(lot_id, connection)
            if not lot or lot["kind"] != "qc_lot":
                raise NotFoundError("qc lot not found: " + str(lot_id))
            if lot["status"] == "retired":
                raise ConflictError("cannot dispense from a retired lot")
            dispensed = connection.execute(
                "SELECT COUNT(*) FROM entities WHERE kind = 'working_bottle' "
                "AND json_extract(data, '$.qc_lot_id') = ?",
                (lot_id,),
            ).fetchone()[0]
            total = lot["data"].get("total_bottles")
            if total is not None and dispensed + len(normalized) > int(total):
                raise ConflictError(
                    "dispense exceeds received stock: %s + %s > %s total"
                    % (dispensed, len(normalized), total)
                )
            order_id = str(uuid4())
            order_data = {
                "dispense_order_no": order_no,
                "qc_lot_id": lot_id,
                "assay_id": lot["data"]["assay_id"],
                "bottle_nos": [item["bottle_no"] for item in normalized],
                "bottle_count": len(normalized),
            }
            self._insert_entity(connection, order_id, "dispense_order", "recorded", order_data, actor.user_id)
            self._claim_unique_key(connection, "dispense_order_no", order_no, order_id)
            created = []
            for item in normalized:
                if self._get_unique_key(
                    connection, "bottle_no", "%s:%s" % (lot_id, item["bottle_no"])
                ):
                    raise ConflictError("bottle_no already used in lot: " + item["bottle_no"])

            for item in normalized:
                expires_at = bottle_expires_at(
                    lot["data"].get("expires_at"),
                    item["opened_at"],
                    lot["data"].get("open_vial_days", 7),
                    item["expires_at"],
                )
                bottle_id = str(uuid4())
                bottle_data = {
                    "bottle_no": item["bottle_no"],
                    "qc_lot_id": lot_id,
                    "assay_id": lot["data"]["assay_id"],
                    "dispense_order_no": order_no,
                    "opened_at": item["opened_at"],
                    "expires_at": expires_at,
                }
                self._insert_entity(connection, bottle_id, "working_bottle", "in_stock", bottle_data, actor.user_id)
                self._claim_unique_key(
                    connection, "bottle_no", "%s:%s" % (lot_id, item["bottle_no"]), bottle_id
                )
                self._insert_audit(
                    connection,
                    bottle_id,
                    actor.user_id,
                    actor.role,
                    "dispense",
                    None,
                    "in_stock",
                    {"dispense_order_no": order_no, "qc_lot_id": lot_id, "expires_at": expires_at},
                )
                created.append(self.get_entity(bottle_id, connection))
            self._insert_audit(
                connection,
                order_id,
                actor.user_id,
                actor.role,
                "record",
                None,
                "recorded",
                {"qc_lot_id": lot_id, "bottle_count": len(created)},
            )
            promoted = self._promote_queue(
                connection, lot["data"]["assay_id"], actor, capacities
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "order": self.get_entity(order_id),
            "bottles": created,
            "promoted": promoted,
            "idempotent_replay": False,
        }

    def request_bottle(self, actor, claim_no, instrument_id, assay_id, capacities=None):
        """Request the next usable bottle for an instrument/assay slot.

        Fulfils immediately while stock and capacity allow; otherwise the
        request is queued in claim order. Idempotent on ``claim_no``.
        """
        if not claim_no or not str(claim_no).strip():
            raise ValidationError("claim_no is required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_id = self._get_unique_key(connection, "claim_no", claim_no)
            if existing_id:
                claim = self.get_entity(existing_id, connection)
                connection.commit()
                return {"claim": claim, "idempotent_replay": True, "promoted": []}
            instrument = self.get_entity(instrument_id, connection)
            assay = self.get_entity(assay_id, connection)
            if not instrument or instrument["kind"] != "instrument":
                raise NotFoundError("instrument not found: " + str(instrument_id))
            if not assay or assay["kind"] != "assay":
                raise NotFoundError("assay not found: " + str(assay_id))
            capacity = self._capacity_for(capacities, instrument_id, assay_id)
            active = self._active_bottle_count(connection, instrument_id, assay_id)
            stock_row = None
            if active < capacity:
                stock_row = connection.execute(
                    "SELECT * FROM entities WHERE kind = 'working_bottle' AND status = 'in_stock' "
                    "AND json_extract(data, '$.assay_id') = ? "
                    "ORDER BY json_extract(data, '$.expires_at'), created_at, id",
                    (assay_id,),
                ).fetchone()
            claim_id = str(uuid4())
            claim_data = {
                "claim_no": claim_no,
                "instrument_id": instrument_id,
                "assay_id": assay_id,
                "requested_by": actor.user_id,
                "working_bottle_id": None,
            }
            if stock_row:
                bottle = _entity_from_row(stock_row)
                self._save_entity(
                    connection,
                    bottle,
                    status="in_use",
                    patch={"instrument_id": instrument_id, "claimed_by": actor.user_id, "claim_id": claim_id},
                )
                claim_data["working_bottle_id"] = bottle["id"]
                claim_data["fulfilled_at"] = utcnow()
                self._insert_entity(connection, claim_id, "bottle_claim", "fulfilled", claim_data, actor.user_id)
                self._claim_unique_key(connection, "claim_no", claim_no, claim_id)
                self._insert_audit(
                    connection,
                    bottle["id"],
                    actor.user_id,
                    actor.role,
                    "assign",
                    "in_stock",
                    "in_use",
                    {"claim_id": claim_id, "instrument_id": instrument_id},
                )
                self._insert_audit(
                    connection,
                    claim_id,
                    actor.user_id,
                    actor.role,
                    "fulfill",
                    "queued",
                    "fulfilled",
                    {"working_bottle_id": bottle["id"]},
                )
                promoted = self._promote_queue(connection, assay_id, actor, capacities)
                result_status = "fulfilled"
                result_bottle = bottle["id"]
            else:
                self._insert_entity(connection, claim_id, "bottle_claim", "queued", claim_data, actor.user_id)
                self._claim_unique_key(connection, "claim_no", claim_no, claim_id)
                self._insert_audit(
                    connection,
                    claim_id,
                    actor.user_id,
                    actor.role,
                    "request",
                    None,
                    "queued",
                    {"instrument_id": instrument_id, "capacity": capacity, "active": active},
                )
                promoted = []
                result_status = "queued"
                result_bottle = None
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "claim": self.get_entity(claim_id),
            "idempotent_replay": False,
            "promoted": promoted,
            "assigned_bottle_id": result_bottle,
            "request_status": result_status,
        }

    def claim_bottle(self, actor, claim_no, bottle_id, instrument_id, capacities=None):
        """Claim one specific bottle for an instrument.

        The conditional UPDATE of the bottle row serialises two people
        grabbing the last bottle: exactly one changes in_stock -> in_use;
        the loser gets a ConflictError reporting the current occupant.
        """
        if not claim_no or not str(claim_no).strip():
            raise ValidationError("claim_no is required")
        connection = self._connect()
        promoted = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_id = self._get_unique_key(connection, "claim_no", claim_no)
            if existing_id:
                claim = self.get_entity(existing_id, connection)
                connection.commit()
                return {"claim": claim, "idempotent_replay": True}
            bottle = self.get_entity(bottle_id, connection)
            if not bottle or bottle["kind"] != "working_bottle":
                raise NotFoundError("working bottle not found: " + str(bottle_id))
            instrument = self.get_entity(instrument_id, connection)
            if not instrument or instrument["kind"] != "instrument":
                raise NotFoundError("instrument not found: " + str(instrument_id))
            assay_id = bottle["data"].get("assay_id")
            if bottle["status"] == "in_use":
                raise ConflictError(
                    "bottle %s is already assigned to instrument %s (claim %s)"
                    % (
                        bottle["data"].get("bottle_no"),
                        bottle["data"].get("instrument_id"),
                        bottle["data"].get("claim_id"),
                    )
                )
            if bottle["status"] == "consumed":
                raise ConflictError("bottle %s has been consumed" % bottle["data"].get("bottle_no"))
            if bottle["status"] == "expired":
                raise ConflictError("bottle %s has expired" % bottle["data"].get("bottle_no"))
            capacity = self._capacity_for(capacities, instrument_id, assay_id)
            active = self._active_bottle_count(connection, instrument_id, assay_id)
            queued_ahead = connection.execute(
                "SELECT COUNT(*) FROM entities WHERE kind = 'bottle_claim' AND status = 'queued' "
                "AND json_extract(data, '$.instrument_id') = ? "
                "AND json_extract(data, '$.assay_id') = ?",
                (instrument_id, assay_id),
            ).fetchone()[0]
            if active >= capacity:
                # Slot full: the request keeps its place in the FIFO queue.
                claim_id = str(uuid4())
                claim_data = {
                    "claim_no": claim_no,
                    "instrument_id": instrument_id,
                    "assay_id": assay_id,
                    "requested_by": actor.user_id,
                    "working_bottle_id": None,
                    "preferred_bottle_id": bottle_id,
                }
                self._insert_entity(connection, claim_id, "bottle_claim", "queued", claim_data, actor.user_id)
                self._claim_unique_key(connection, "claim_no", claim_no, claim_id)
                self._insert_audit(
                    connection,
                    claim_id,
                    actor.user_id,
                    actor.role,
                    "request",
                    None,
                    "queued",
                    {"preferred_bottle_id": bottle_id, "capacity": capacity, "queued_ahead": queued_ahead},
                )
                connection.commit()
                return {
                    "claim": self.get_entity(claim_id),
                    "idempotent_replay": False,
                    "request_status": "queued",
                    "assigned_bottle_id": None,
                }
            claim_id = str(uuid4())
            cursor = connection.execute(
                "UPDATE entities SET status = 'in_use', version = version + 1, updated_at = ? "
                "WHERE id = ? AND kind = 'working_bottle' AND status = 'in_stock' AND version = ?",
                (utcnow(), bottle_id, bottle["version"]),
            )
            if cursor.rowcount != 1:
                fresh = self.get_entity(bottle_id, connection)
                raise ConflictError(
                    "bottle %s was just taken by instrument %s"
                    % (fresh["data"].get("bottle_no"), fresh["data"].get("instrument_id"))
                )
            bottle_data = dict(bottle["data"])
            bottle_data.update(
                {"instrument_id": instrument_id, "claimed_by": actor.user_id, "claim_id": claim_id}
            )
            connection.execute(
                "UPDATE entities SET data = ? WHERE id = ?",
                (json.dumps(bottle_data, ensure_ascii=False, sort_keys=True), bottle_id),
            )
            claim_data = {
                "claim_no": claim_no,
                "instrument_id": instrument_id,
                "assay_id": assay_id,
                "requested_by": actor.user_id,
                "working_bottle_id": bottle_id,
                "preferred_bottle_id": bottle_id,
            }
            self._insert_entity(connection, claim_id, "bottle_claim", "fulfilled", claim_data, actor.user_id)
            self._claim_unique_key(connection, "claim_no", claim_no, claim_id)
            self._insert_audit(
                connection,
                bottle_id,
                actor.user_id,
                actor.role,
                "assign",
                "in_stock",
                "in_use",
                {"claim_id": claim_id, "instrument_id": instrument_id},
            )
            self._insert_audit(
                connection,
                claim_id,
                actor.user_id,
                actor.role,
                "fulfill",
                "queued",
                "fulfilled",
                {"working_bottle_id": bottle_id},
            )
            promoted = self._promote_queue(connection, assay_id, actor, capacities)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "claim": self.get_entity(claim_id),
            "idempotent_replay": False,
            "request_status": "fulfilled",
            "assigned_bottle_id": bottle_id,
            "promoted": promoted,
        }

    def consume_bottle(self, actor, bottle_id, reason=""):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            bottle = self.get_entity(bottle_id, connection)
            if not bottle or bottle["kind"] != "working_bottle":
                raise NotFoundError("working bottle not found: " + str(bottle_id))
            if bottle["status"] != "in_use":
                raise ConflictError("only an in-use bottle can be consumed")
            assay_id = bottle["data"].get("assay_id")
            instrument_id = bottle["data"].get("instrument_id")
            self._save_entity(connection, bottle, status="consumed", patch={"consumed_at": utcnow(), "consume_reason": reason})
            self._insert_audit(
                connection,
                bottle_id,
                actor.user_id,
                actor.role,
                "consume",
                "in_use",
                "consumed",
                {"reason": reason},
            )
            promoted = []
            if assay_id and instrument_id:
                # Fulfilled claims are counted by status; a consumed bottle no
                # longer occupies a slot, so the queue may advance here.
                capacities = self._load_capacities(connection)
                promoted = self._promote_queue(connection, assay_id, actor, capacities)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"bottle": self.get_entity(bottle_id), "promoted": promoted}

    @staticmethod
    def _load_capacities(connection):
        capacities = {}
        rows = connection.execute("SELECT * FROM entities WHERE kind = 'instrument_assay'").fetchall()
        for row in rows:
            data = json.loads(row["data"])
            capacities[data.get("scope")] = int(data.get("bottle_capacity", 1))
        return capacities

    def _expire_bottle_locked(self, connection, bottle, actor, reason, as_of, capacities):
        assay_id = bottle["data"].get("assay_id")
        instrument_id = bottle["data"].get("instrument_id")
        self._save_entity(
            connection,
            bottle,
            status="expired",
            patch={"expired_at": utcnow(), "expire_reason": reason},
        )
        self._insert_audit(
            connection,
            bottle["id"],
            actor.user_id,
            actor.role,
            "expire",
            "in_use" if instrument_id else "in_stock",
            "expired",
            {"reason": reason, "as_of": as_of},
        )
        # In-flight QC results referencing this bottle must be re-evaluated
        # and sent back for redo. Runs that already backed a released patient
        # batch are untouched: the release keeps its recorded basis.
        redone_runs = []
        preserved_batches = []
        intercepted_batches = []
        released_run_ids = set()
        for batch_row in connection.execute(
            "SELECT * FROM entities WHERE kind = 'result_batch' AND status = 'released'"
        ).fetchall():
            batch = _entity_from_row(batch_row)
            if batch["data"].get("qc_run_id"):
                released_run_ids.add(batch["data"].get("qc_run_id"))
        run_rows = connection.execute(
            "SELECT * FROM entities WHERE kind = 'qc_run' "
            "AND json_extract(data, '$.working_bottle_id') = ? ORDER BY created_at, id",
            (bottle["id"],),
        ).fetchall()
        for run_row in run_rows:
            run = _entity_from_row(run_row)
            if run["id"] in released_run_ids:
                for batch_row in connection.execute(
                    "SELECT * FROM entities WHERE kind = 'result_batch' AND status = 'released' "
                    "AND json_extract(data, '$.qc_run_id') = ?",
                    (run["id"],),
                ).fetchall():
                    preserved_batches.append(_entity_from_row(batch_row)["id"])
                continue
            if run["status"] in ("redo_required", "retesting", "resolved"):
                continue
            patch = {
                "invalidated_bottle_id": bottle["id"],
                "invalidated_reason": reason,
                "invalidated_at": utcnow(),
            }
            self._save_entity(connection, run, status="redo_required", patch=patch)
            self._insert_audit(
                connection,
                run["id"],
                actor.user_id,
                actor.role,
                "invalidate_for_redo",
                run["status"],
                "redo_required",
                {"working_bottle_id": bottle["id"], "reason": reason},
            )
            redone_runs.append(run["id"])
            for batch_row in connection.execute(
                "SELECT * FROM entities WHERE kind = 'result_batch' AND status = 'waiting' "
                "AND json_extract(data, '$.qc_run_id') = ?",
                (run["id"],),
            ).fetchall():
                batch = _entity_from_row(batch_row)
                intercept_reason = "intercepted: working bottle %s expired, QC result pending redo" % (
                    bottle["data"].get("bottle_no")
                )
                self._save_entity(
                    connection,
                    batch,
                    status="intercepted",
                    patch={"intercept_reason": intercept_reason, "held_for_run_id": run["id"]},
                )
                self._insert_audit(
                    connection,
                    batch["id"],
                    actor.user_id,
                    actor.role,
                    "intercept",
                    "waiting",
                    "intercepted",
                    {"reason": intercept_reason},
                )
                intercepted_batches.append(batch["id"])
        promoted = []
        if assay_id:
            promoted = self._promote_queue(connection, assay_id, actor, capacities)
        return {
            "bottle_id": bottle["id"],
            "redo_required_run_ids": redone_runs,
            "intercepted_batch_ids": intercepted_batches,
            "preserved_released_batch_ids": sorted(set(preserved_batches)),
            "promoted": promoted,
        }

    def expire_bottle(self, actor, bottle_id, reason, as_of=None):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            bottle = self.get_entity(bottle_id, connection)
            if not bottle or bottle["kind"] != "working_bottle":
                raise NotFoundError("working bottle not found: " + str(bottle_id))
            if bottle["status"] == "expired":
                return {"bottle": bottle, "idempotent_replay": True}
            if bottle["status"] == "consumed":
                raise ConflictError("consumed bottles cannot expire")
            as_of = as_of or utcnow()
            if _parse_iso(as_of) <= _parse_iso(bottle["data"]["expires_at"]):
                raise ConflictError("bottle has not reached its expiry instant")
            capacities = self._load_capacities(connection)
            outcome = self._expire_bottle_locked(connection, bottle, actor, reason, as_of, capacities)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        outcome["bottle"] = self.get_entity(bottle_id)
        return outcome

    def sweep_expired_bottles(self, actor, as_of=None):
        as_of = as_of or utcnow()
        connection = self._connect()
        outcomes = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            capacities = self._load_capacities(connection)
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'working_bottle' "
                "AND status IN ('in_stock', 'in_use') ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                bottle = _entity_from_row(row)
                if _parse_iso(as_of) > _parse_iso(bottle["data"]["expires_at"]):
                    outcomes.append(
                        self._expire_bottle_locked(
                            connection,
                            bottle,
                            actor,
                            "scheduled expiry sweep",
                            as_of,
                            capacities,
                        )
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"as_of": as_of, "expired_count": len(outcomes), "items": outcomes}

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
