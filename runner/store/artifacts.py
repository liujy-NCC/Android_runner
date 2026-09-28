"""SHA-256 content-addressed artifact and evidence store.

Ordering guarantees (see IMPLEMENTATION-CONTRACT.md §4):

1. declare the artifact in SQLite (``state='staged'``) *before* publishing it
   into the content-addressed object tree;
2. write to ``staging/`` while computing length and SHA-256, fsync;
3. verify (length, SHA-256) against the declaration;
4. publish with an atomic rename into ``objects/<aa>/<sha256>``;
5. mark ``state='sealed'`` and append an event.

The Oracle may only read objects whose row is ``sealed`` *and* whose bytes
re-hash to the recorded digest. Anything else is reported as
``EVIDENCE_HASH_MISMATCH``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Any, Iterable, Iterator

from runner.contracts.ids import new_id
from runner.errors import StorageError
from runner.store.db import Store
from runner.store.events import EventLog

CHUNK = 65536


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, root: str, store: Store, events: EventLog, *, clock: Any) -> None:
        self.root = os.path.abspath(root)
        self.staging_dir = os.path.join(self.root, "staging")
        self.objects_dir = os.path.join(self.root, "objects")
        self.quarantine_dir = os.path.join(self.root, "quarantine")
        for path in (self.staging_dir, self.objects_dir, self.quarantine_dir):
            os.makedirs(path, exist_ok=True)
        self.store = store
        self.events = events
        self.clock = clock

    # -- path helpers --------------------------------------------------------
    def object_relpath(self, digest: str) -> str:
        return os.path.join("objects", digest[:2], digest)

    def object_path(self, digest: str) -> str:
        return os.path.join(self.objects_dir, digest[:2], digest)

    # -- hashing -------------------------------------------------------------
    @staticmethod
    def hash_file(path: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            while True:
                block = handle.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    # -- transactions --------------------------------------------------------
    def declare(
        self,
        *,
        session_id: str,
        kind: str,
        media_type: str,
        source: str,
        provenance: str = "runner_generated",
        attempt_id: str | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
    ) -> tuple[str, str]:
        """Create the staged row. Returns ``(artifact_id, staging_path)``.

        Must run inside the caller's transaction so that the declaration and
        the caller's budget reservation commit together.
        """

        artifact_id = new_id("artifact")
        staging_path = os.path.join(self.staging_dir, f"{artifact_id}.part")
        self.store.execute(
            """
            INSERT INTO artifacts(artifact_id, session_id, kind, content_sha256, size_bytes,
                                  media_type, state, staging_path, relative_object_path, source,
                                  provenance, attempt_id, candidate_id, build_id, created_utc)
            VALUES (?,?,?,NULL,0,?, 'staged', ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                session_id,
                kind,
                media_type,
                staging_path,
                source,
                provenance,
                attempt_id,
                candidate_id,
                build_id,
                self.clock.utc(),
            ),
        )
        return artifact_id, staging_path

    def write_staging(self, staging_path: str, data: bytes | Iterable[bytes]) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with open(staging_path, "wb") as handle:
            blocks: Iterable[bytes] = [data] if isinstance(data, (bytes, bytearray)) else data
            for block in blocks:
                if not block:
                    continue
                handle.write(block)
                digest.update(block)
                size += len(block)
            handle.flush()
            os.fsync(handle.fileno())
        return digest.hexdigest(), size

    def publish(
        self,
        *,
        artifact_id: str,
        session_id: str,
        digest: str,
        size: int,
        staging_path: str,
        attempt_id: str | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
    ) -> None:
        """Verify a staged object and move it into the content-addressed tree.

        Runs inside the caller's transaction: the SQLite state change to
        ``sealed`` and the filesystem rename are ordered so that a crash leaves
        either a recoverable ``staged`` row or a fully published object.
        """

        actual_digest, actual_size = self.hash_file(staging_path)
        if actual_digest != digest or actual_size != size:
            self._mark_corrupt(
                artifact_id, attempt_id=attempt_id, reason="staging_hash_mismatch", session_id=session_id
            )
            raise StorageError("staged object failed hash verification", artifact_id=artifact_id)

        target = self.object_path(digest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            existing_digest, existing_size = self.hash_file(target)
            if existing_digest != digest or existing_size != size:
                os.replace(target, os.path.join(self.quarantine_dir, f"{digest}.bad.{new_id('artifact')}"))
                raise StorageError("existing object failed hash verification", artifact_id=artifact_id)
            os.remove(staging_path)
        else:
            os.replace(staging_path, target)
            self._fsync_dir(os.path.dirname(target))

        self.store.execute(
            """
            UPDATE artifacts
               SET state='sealed', content_sha256=?, size_bytes=?, staging_path=NULL,
                   relative_object_path=?, sealed_utc=?
             WHERE artifact_id=?
            """,
            (digest, size, self.object_relpath(digest), self.clock.utc(), artifact_id),
        )
        self.events.append(
            session_id,
            "artifact_sealed",
            payload={"artifact_id": artifact_id, "kind": self._kind_of(artifact_id), "sha256": digest, "size": size},
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            build_id=build_id,
        )

    def put(
        self,
        *,
        session_id: str,
        kind: str,
        media_type: str,
        data: bytes | Iterable[bytes],
        source: str,
        provenance: str = "runner_generated",
        attempt_id: str | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> str:
        """Declare, write, verify and publish one object.

        Staging and verification happen *outside* the publish transaction so a
        verification failure can be recorded durably: a rejected object must
        leave a ``corrupt`` row behind, not a rolled-back one.
        """

        artifact_id, staging_path = self.declare(
            session_id=session_id,
            kind=kind,
            media_type=media_type,
            source=source,
            provenance=provenance,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            build_id=build_id,
        )
        digest, size = self.write_staging(staging_path, data)
        if expected_sha256 is not None and digest != expected_sha256:
            # Commit the corrupt marker in its own transaction, then refuse.
            with self.store.txn():
                self._mark_corrupt(
                    artifact_id,
                    attempt_id=attempt_id,
                    reason="expected_hash_mismatch",
                    session_id=session_id,
                )
            raise StorageError("object does not match expected digest", artifact_id=artifact_id)
        with self.store.txn():
            self.publish(
                artifact_id=artifact_id,
                session_id=session_id,
                digest=digest,
                size=size,
                staging_path=staging_path,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                build_id=build_id,
            )
        return artifact_id

    def put_in_txn(
        self,
        *,
        session_id: str,
        kind: str,
        media_type: str,
        data: bytes | Iterable[bytes],
        source: str,
        provenance: str = "runner_generated",
        attempt_id: str | None = None,
        candidate_id: str | None = None,
        build_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> str:
        """Same as :meth:`put` but reuses the caller's open transaction."""

        artifact_id, staging_path = self.declare(
            session_id=session_id,
            kind=kind,
            media_type=media_type,
            source=source,
            provenance=provenance,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            build_id=build_id,
        )
        digest, size = self.write_staging(staging_path, data)
        if expected_sha256 is not None and digest != expected_sha256:
            self._mark_corrupt(
                artifact_id,
                attempt_id=attempt_id,
                reason="expected_hash_mismatch",
                session_id=session_id,
            )
            raise StorageError("object does not match expected digest", artifact_id=artifact_id)
        self.publish(
            artifact_id=artifact_id,
            session_id=session_id,
            digest=digest,
            size=size,
            staging_path=staging_path,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            build_id=build_id,
        )
        return artifact_id

    # -- idempotent re-sealing ----------------------------------------------
    def sealed_artifact_id(
        self,
        *,
        session_id: str,
        kind: str,
        digest: str,
        attempt_id: str | None,
        candidate_id: str | None,
    ) -> str | None:
        """Find an already-sealed artifact for this exact content and scope.

        Sealing evidence is not atomic across the filesystem and SQLite: a crash
        between "object published" and "row marked sealed" (or between the seal
        and the attempt's ``evidence_sealed`` flag) must not create a *second*
        artifact row for the same bytes. Reusing the existing sealed row keeps
        recovery idempotent, so the Oracle cannot receive the same evidence twice.
        """

        row = self.store.query_one(
            """
            SELECT artifact_id FROM artifacts
             WHERE session_id=? AND kind=? AND content_sha256=? AND state='sealed'
               AND attempt_id IS ? AND candidate_id IS ?
             ORDER BY created_utc, artifact_id LIMIT 1
            """,
            (session_id, kind, digest, attempt_id, candidate_id),
        )
        return row["artifact_id"] if row is not None else None

    # -- reading -------------------------------------------------------------
    def read_object(self, artifact_id: str, *, require_sealed: bool = True) -> bytes:
        """Read an object, re-verifying its digest on every read."""

        row = self.store.query_one("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row is None:
            raise StorageError("no such artifact", artifact_id=artifact_id)
        if require_sealed and row["state"] != "sealed":
            raise StorageError("artifact is not sealed", artifact_id=artifact_id)
        digest = row["content_sha256"]
        if not digest:
            raise StorageError("artifact has no recorded digest", artifact_id=artifact_id)
        path = self.object_path(digest)
        if not os.path.exists(path):
            self._mark_corrupt(
                artifact_id, session_id=row["session_id"], attempt_id=row["attempt_id"], reason="object_missing"
            )
            raise StorageError("object file is missing", artifact_id=artifact_id)
        actual, size = self.hash_file(path)
        if actual != digest or size != row["size_bytes"]:
            self._mark_corrupt(
                artifact_id, session_id=row["session_id"], attempt_id=row["attempt_id"], reason="object_hash_mismatch"
            )
            raise StorageError("object failed hash verification", artifact_id=artifact_id)
        with open(path, "rb") as handle:
            return handle.read()

    def iter_object(self, artifact_id: str, *, require_sealed: bool = True) -> Iterator[bytes]:
        yield self.read_object(artifact_id, require_sealed=require_sealed)

    # -- seal groups ---------------------------------------------------------
    def open_seal_group(
        self, *, session_id: str, kind: str, attempt_id: str | None = None
    ) -> str:
        seal_id = new_id("seal")
        self.store.execute(
            """
            INSERT INTO seal_groups(seal_id, session_id, attempt_id, kind, state,
                                    member_hashes_json, created_utc)
            VALUES (?,?,?,?, 'open', '[]', ?)
            """,
            (seal_id, session_id, attempt_id, kind, self.clock.utc()),
        )
        return seal_id

    def seal_group(
        self, seal_id: str, *, member_artifact_ids: list[str], session_id: str, attempt_id: str | None = None
    ) -> str:
        """Seal a group of already-sealed objects and record the group digest.

        Every member must already be ``sealed``: an attempt cannot claim an
        evidence group while some of its evidence is still staging.
        """

        member_hashes: list[str] = []
        for artifact_id in member_artifact_ids:
            row = self.store.query_one(
                "SELECT state, content_sha256 FROM artifacts WHERE artifact_id=?", (artifact_id,)
            )
            if row is None or row["state"] != "sealed":
                raise StorageError("cannot seal group with unsealed member", artifact_id=artifact_id)
            member_hashes.append(row["content_sha256"])

        sorted_hashes = sorted(member_hashes)
        group_digest = hashlib.sha256("\n".join(sorted_hashes).encode("utf-8")).hexdigest()
        now = self.clock.utc()
        self.store.execute(
            """
            UPDATE seal_groups
               SET state='sealing', member_hashes_json=?, group_sha256=?
             WHERE seal_id=?
            """,
            (self.store.dumps(sorted_hashes), group_digest, seal_id),
        )
        self.store.execute(
            "UPDATE seal_groups SET state='sealed', sealed_utc=? WHERE seal_id=?", (now, seal_id)
        )
        self.events.append(
            session_id,
            "evidence_group_sealed",
            payload={
                "seal_id": seal_id,
                "member_count": len(sorted_hashes),
                "group_sha256": group_digest,
                "kind": self._seal_kind(seal_id),
            },
            attempt_id=attempt_id,
        )
        return group_digest

    # -- crash recovery ------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        """Reconcile staging/objects against SQLite after a crash.

        Returns a summary for the supervisor ledger. Never deletes evidence:
        unknown files are moved to ``quarantine/`` and recorded.
        """

        summary: dict[str, Any] = {
            "staged_completed": 0,
            "staged_missing": 0,
            "orphan_staging": 0,
            "orphan_objects": 0,
            "corrupt": 0,
        }
        with self.store.txn():
            staged = self.store.query_all(
                "SELECT * FROM artifacts WHERE state='staged'"
            )
            for row in staged:
                staging_path = row["staging_path"]
                if staging_path and os.path.exists(staging_path):
                    digest, size = self.hash_file(staging_path)
                    try:
                        self.publish(
                            artifact_id=row["artifact_id"],
                            session_id=row["session_id"],
                            digest=digest,
                            size=size,
                            staging_path=staging_path,
                            attempt_id=row["attempt_id"],
                            candidate_id=row["candidate_id"],
                            build_id=row["build_id"],
                        )
                        summary["staged_completed"] += 1
                    except StorageError:
                        summary["corrupt"] += 1
                else:
                    self._mark_corrupt(
                        row["artifact_id"],
                        session_id=row["session_id"],
                        attempt_id=row["attempt_id"],
                        reason="staging_missing",
                    )
                    summary["staged_missing"] += 1

            known_staging = {
                os.path.basename(r["staging_path"])
                for r in self.store.query_all(
                    "SELECT staging_path FROM artifacts WHERE staging_path IS NOT NULL"
                )
                if r["staging_path"]
            }
            for name in os.listdir(self.staging_dir):
                if name in known_staging:
                    continue
                src = os.path.join(self.staging_dir, name)
                if not os.path.isfile(src):
                    continue
                os.replace(src, os.path.join(self.quarantine_dir, f"orphan.{name}"))
                self.events.append(
                    _SYSTEM_SESSION,
                    "artifact_staging_orphan",
                    payload={"name": name},
                )
                summary["orphan_staging"] += 1

            known_objects = {
                r["content_sha256"]
                for r in self.store.query_all(
                    "SELECT content_sha256 FROM artifacts WHERE content_sha256 IS NOT NULL"
                )
            }
            for prefix in os.listdir(self.objects_dir):
                prefix_dir = os.path.join(self.objects_dir, prefix)
                if not os.path.isdir(prefix_dir):
                    continue
                for name in os.listdir(prefix_dir):
                    if name in known_objects:
                        continue
                    self.events.append(
                        _SYSTEM_SESSION,
                        "artifact_object_orphan",
                        payload={"sha256": name},
                    )
                    summary["orphan_objects"] += 1

            # Sealed rows whose bytes no longer verify must not reach the Oracle.
            sealed = self.store.query_all(
                "SELECT artifact_id, session_id, attempt_id, content_sha256, size_bytes "
                "FROM artifacts WHERE state='sealed'"
            )
            for row in sealed:
                path = self.object_path(row["content_sha256"])
                ok = False
                if os.path.exists(path):
                    actual, size = self.hash_file(path)
                    ok = actual == row["content_sha256"] and size == row["size_bytes"]
                if not ok:
                    self._mark_corrupt(
                        row["artifact_id"],
                        session_id=row["session_id"],
                        attempt_id=row["attempt_id"],
                        reason="sealed_object_unverifiable",
                    )
                    summary["corrupt"] += 1

            for group in self.store.query_all("SELECT * FROM seal_groups WHERE state='sealed'"):
                hashes = self.store.loads(group["member_hashes_json"], [])
                for digest in hashes:
                    bad = self.store.query_one(
                        "SELECT artifact_id FROM artifacts WHERE content_sha256=? AND state<>'sealed'",
                        (digest,),
                    )
                    if bad is not None:
                        self.store.execute(
                            "UPDATE seal_groups SET state='corrupt' WHERE seal_id=?", (group["seal_id"],)
                        )
                        break
        return summary

    # -- internals -----------------------------------------------------------
    def _mark_corrupt(
        self, artifact_id: str, *, session_id: str | None, attempt_id: str | None, reason: str
    ) -> None:
        self.store.execute(
            "UPDATE artifacts SET state='corrupt', staging_path=NULL WHERE artifact_id=?", (artifact_id,)
        )
        if session_id:
            self.events.append(
                session_id,
                "artifact_corrupt",
                payload={"artifact_id": artifact_id, "reason": reason},
                attempt_id=attempt_id,
            )

    def _kind_of(self, artifact_id: str) -> str:
        row = self.store.query_one("SELECT kind FROM artifacts WHERE artifact_id=?", (artifact_id,))
        return row["kind"] if row else "unknown"

    def _seal_kind(self, seal_id: str) -> str:
        row = self.store.query_one("SELECT kind FROM seal_groups WHERE seal_id=?", (seal_id,))
        return row["kind"] if row else "unknown"

    @staticmethod
    def _fsync_dir(path: str) -> None:
        if os.name != "posix":  # Windows has no directory fsync
            return
        try:  # pragma: no cover - posix only
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:  # pragma: no cover
            pass

    def quarantine_path(self, name: str) -> str:
        return os.path.join(self.quarantine_dir, name)


_SYSTEM_SESSION = "ses_SYSTEM0000000000000000000"
