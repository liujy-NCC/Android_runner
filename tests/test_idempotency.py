"""Idempotency: replay returns the original result, side effects happen once."""

from __future__ import annotations

import threading

from harness import Harness, make_source_files, mock_submission


def test_same_key_same_body_returns_original_result() -> None:
    h = Harness()
    try:
        first = h.submit("submit-key-0000000401")
        second = h.submit("submit-key-0000000401")
        assert first == second, (first, second)

        build_first = h.build("build-key-00000000401", first["candidate_id"])
        build_second = h.build("build-key-00000000401", first["candidate_id"])
        assert build_first == build_second

        begin_first = h.begin("attempt-key-0000000401", build_first["build_id"])
        begin_second = h.begin("attempt-key-0000000401", build_first["build_id"])
        assert begin_first == begin_second

        # No duplicate budget consumption.
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 1
        assert snapshot["session:effective_attempts"]["consumed"] == 1
        assert snapshot["session:distinct_candidates"]["consumed"] == 1
    finally:
        h.close()


def test_same_key_different_body_is_a_conflict() -> None:
    h = Harness()
    try:
        h.submit("submit-key-0000000402")
        status, body = h.post(
            "/v1/candidates",
            {
                "idempotency_key": "submit-key-0000000402",
                "files": make_source_files(body="public final class Different {}"),
            },
        )
        assert status == 409, body
        assert body["error"]["code"] == "idempotency_conflict"
    finally:
        h.close()


def test_concurrent_same_key_executes_side_effect_once() -> None:
    """N threads, one key: exactly one candidate/build/attempt side effect."""

    h = Harness()
    try:
        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(h.submit("submit-key-0000000403"))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert len(results) == 8
        ids = {r.get("candidate_id") for r in results}
        assert len(ids) == 1, ids
        assert all(r.get("tree_hash") == results[0].get("tree_hash") for r in results)

        rows = h.service.store.query_all(
            "SELECT candidate_id FROM candidates WHERE session_id=?", (h.session_id,)
        )
        assert len(rows) == 1, rows
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:distinct_candidates"]["consumed"] == 1
    finally:
        h.close()


def test_concurrent_same_key_build_executes_once() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000404")
        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(h.build("build-key-00000000404", sub["candidate_id"]))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        build_ids = {r.get("build_id") for r in results}
        assert len(build_ids) == 1, build_ids
        assert len(h.build_backend.calls) == 1, h.build_backend.calls
        snapshot = h.service.store.counter_snapshot(h.session_id)
        assert snapshot["session:builds"]["consumed"] == 1
    finally:
        h.close()


def test_concurrent_same_key_launch_executes_once() -> None:
    h = Harness()
    try:
        sub = h.submit("submit-key-0000000405")
        build = h.build("build-key-00000000405", sub["candidate_id"])
        attempt_id = h.begin("attempt-key-0000000405", build["build_id"])["attempt_id"]

        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(h.launch("launch-key-00000000405", attempt_id))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        action_ids = {r.get("action_id") for r in results}
        assert len(action_ids) == 1, action_ids
        # The device saw exactly one launch command.
        assert h.device.state.candidate_launch_count == 1
    finally:
        h.close()


def test_finalize_replay_and_conflicting_reselection() -> None:
    h = Harness()
    try:
        sub_a = h.submit("submit-key-0000000406", files=make_source_files(body="public final class FA {}"))
        build_a = h.build("build-key-00000000406", sub_a["candidate_id"])
        sub_b = h.submit("submit-key-0000000407", files=make_source_files(body="public final class FB {}"))
        build_b = h.build("build-key-00000000407", sub_b["candidate_id"])

        payload_a = {
            "idempotency_key": "finalize-key-000000406",
            "candidate_id": sub_a["candidate_id"],
            "build_id": build_a["build_id"],
        }
        _s, first = h.post("/v1/finalize", payload_a)
        _s, replay = h.post("/v1/finalize", payload_a)
        assert first == replay, (first, replay)

        # A different key pointing at a different candidate is refused.
        status, body = h.post(
            "/v1/finalize",
            {
                "idempotency_key": "finalize-key-000000407",
                "candidate_id": sub_b["candidate_id"],
                "build_id": build_b["build_id"],
            },
        )
        assert status == 403, body
        assert body["error"]["code"] == "permission_denied"
    finally:
        h.close()


def test_reads_do_not_require_idempotency_keys() -> None:
    h = Harness()
    try:
        status, _body = h.get("/v1/session")
        assert status == 200
        status, _body = h.get("/v1/capabilities")
        assert status == 200
    finally:
        h.close()
