import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            """)

    @staticmethod
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

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
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
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

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

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    # --- working bottle operations ---

    def aliquot_bottles(self, lot_id, count, opened_at, expires_at, actor_id):
        """Idempotently create working bottles from a lot.

        ``count`` is the **total** number of bottles to aliquot from this
        lot.  Bottle IDs are deterministic (``wb_{lot_id}_{seq:03d}``),
        so retries after a write failure skip already-created bottles
        instead of double-occupying or double-bookkeeping.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lot = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (lot_id,)
            ).fetchone()
            if not lot:
                raise NotFoundError("qc lot not found: " + lot_id)
            lot_data = json.loads(lot["data"])
            total = int(lot_data.get("total_bottles", 0) or 0)
            if int(count) > total:
                raise ValidationError(
                    "cannot aliquot %d bottles: total is %d" % (count, total)
                )
            created = []
            for seq in range(1, int(count) + 1):
                bottle_id = "wb_%s_%03d" % (lot_id, seq)
                existing = connection.execute(
                    "SELECT 1 FROM entities WHERE id = ?", (bottle_id,)
                ).fetchone()
                if existing:
                    continue
                bottle_no = "%s-%03d" % (lot_data["lot_no"], seq)
                data = {
                    "qc_lot_id": lot_id,
                    "bottle_no": bottle_no,
                    "assay_id": lot_data["assay_id"],
                    "opened_at": opened_at,
                    "expires_at": expires_at,
                    "instrument_id": None,
                    "claimed_by": None,
                }
                payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
                now = utcnow()
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, "
                    "created_by, created_at, updated_at) "
                    "VALUES (?, 'working_bottle', 'in_storage', 1, ?, ?, ?, ?)",
                    (bottle_id, payload, actor_id, now, now),
                )
                created.append(bottle_id)
            lot_data["aliquoted_count"] = int(count)
            payload = json.dumps(lot_data, ensure_ascii=False, sort_keys=True)
            now = utcnow()
            connection.execute(
                "UPDATE entities SET version = version + 1, data = ?, "
                "updated_at = ? WHERE id = ?",
                (payload, now, lot_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return created

    def claim_bottle(self, bottle_id, instrument_id, actor_id):
        """Atomically claim a bottle for an instrument.

        Uses ``BEGIN IMMEDIATE`` so the capacity check and the status
        update happen in one transaction.  Two concurrent claims for the
        last bottle cannot both succeed: the loser sees a ConflictError
        because the bottle is no longer ``in_storage``.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (bottle_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("bottle not found: " + bottle_id)
            if row["kind"] != "working_bottle":
                raise ValidationError("entity is not a working bottle: " + bottle_id)
            if row["status"] != "in_storage":
                raise ConflictError(
                    "bottle %s is already occupied (status: %s)"
                    % (bottle_id, row["status"])
                )
            data = json.loads(row["data"])
            if data.get("expires_at") and str(data["expires_at"]) < utcnow():
                raise ValidationError("bottle is expired, cannot claim")
            instrument = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (instrument_id,)
            ).fetchone()
            if not instrument:
                raise NotFoundError("instrument not found: " + instrument_id)
            inst_data = json.loads(instrument["data"])
            capacity = int(inst_data.get("bottle_capacity", 1) or 1)
            count = connection.execute(
                "SELECT COUNT(*) FROM entities "
                "WHERE kind = 'working_bottle' AND status = 'in_use' "
                "AND json_extract(data, '$.instrument_id') = ? "
                "AND json_extract(data, '$.assay_id') = ?",
                (instrument_id, data["assay_id"]),
            ).fetchone()[0]
            new_status = "in_use" if int(count) < capacity else "queued"
            data["instrument_id"] = instrument_id
            data["claimed_by"] = actor_id
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            now = utcnow()
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, "
                "data = ?, updated_at = ? WHERE id = ?",
                (new_status, payload, now, bottle_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(bottle_id)

    def release_bottle(self, bottle_id, actor_id):
        """Return a bottle to storage, freeing capacity.

        Promotes the oldest queued bottle for the same
        (instrument, assay) so the queue drains in order.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (bottle_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("bottle not found: " + bottle_id)
            if row["kind"] != "working_bottle":
                raise ValidationError("entity is not a working bottle: " + bottle_id)
            if row["status"] not in ("in_use", "queued"):
                raise ConflictError(
                    "bottle %s is not in use or queued (status: %s)"
                    % (bottle_id, row["status"])
                )
            data = json.loads(row["data"])
            instrument_id = data.get("instrument_id")
            assay_id = data.get("assay_id")
            data["instrument_id"] = None
            data["claimed_by"] = None
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            now = utcnow()
            connection.execute(
                "UPDATE entities SET status = 'in_storage', version = version + 1, "
                "data = ?, updated_at = ? WHERE id = ?",
                (payload, now, bottle_id),
            )
            promoted = None
            if instrument_id and assay_id:
                queued = connection.execute(
                    "SELECT id FROM entities "
                    "WHERE kind = 'working_bottle' AND status = 'queued' "
                    "AND json_extract(data, '$.instrument_id') = ? "
                    "AND json_extract(data, '$.assay_id') = ? "
                    "ORDER BY created_at, id LIMIT 1",
                    (instrument_id, assay_id),
                ).fetchone()
                if queued:
                    q_row = connection.execute(
                        "SELECT * FROM entities WHERE id = ?", (queued["id"],)
                    ).fetchone()
                    q_data = json.loads(q_row["data"])
                    q_data["claimed_by"] = actor_id
                    q_payload = json.dumps(q_data, ensure_ascii=False, sort_keys=True)
                    connection.execute(
                        "UPDATE entities SET status = 'in_use', version = version + 1, "
                        "data = ?, updated_at = ? WHERE id = ?",
                        (q_payload, now, queued["id"]),
                    )
                    promoted = queued["id"]
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(bottle_id), promoted

    def expire_bottle(self, bottle_id):
        """Mark a bottle as expired.

        Returns the list of in-transit ``qc_run`` IDs that reference this
        bottle.  In-transit means the run has not reached a final state
        (``pending``, ``rejected``, ``retesting``, ``investigated``).
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (bottle_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("bottle not found: " + bottle_id)
            if row["kind"] != "working_bottle":
                raise ValidationError("entity is not a working bottle: " + bottle_id)
            if row["status"] == "expired":
                raise ConflictError("bottle is already expired: " + bottle_id)
            now = utcnow()
            connection.execute(
                "UPDATE entities SET status = 'expired', version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (now, bottle_id),
            )
            runs = connection.execute(
                "SELECT id FROM entities WHERE kind = 'qc_run' "
                "AND json_extract(data, '$.bottle_id') = ?",
                (bottle_id,),
            ).fetchall()
            run_ids = [r["id"] for r in runs]
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return run_ids

    def find_bottles_by_lot(self, lot_id):
        return [
            entity
            for entity in self.list_entities(kind="working_bottle")
            if entity["data"].get("qc_lot_id") == lot_id
        ]

    def find_expired_bottles(self):
        now = utcnow()
        return [
            entity
            for entity in self.list_entities(kind="working_bottle")
            if entity["status"] != "expired"
            and entity["data"].get("expires_at")
            and str(entity["data"]["expires_at"]) < now
        ]
