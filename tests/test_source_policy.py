"""`and000008-java-v1` input validation and the generic freeze discipline.

These tests implement the input-validation position only. They deliberately do
NOT assert anything about `CandidateBehavior` method signatures — per
AUDIT-v0.6.md those must not be frozen from the archived FGS APK.
"""

from __future__ import annotations

import base64

from harness import JAVA_ROOT, Harness, make_source_files, mock_submission


def _files(entries: dict[str, bytes]) -> list[dict[str, str]]:
    return [
        {"relative_path": path, "content_base64": base64.b64encode(data).decode("ascii")}
        for path, data in entries.items()
    ]


def _and000008_harness() -> Harness:
    from runner.config import and000008_config

    return Harness(config=and000008_config())


def test_accepts_only_fixed_namespace_java() -> None:
    h = _and000008_harness()
    try:
        files = make_source_files()
        assert all(f["relative_path"].startswith(JAVA_ROOT) for f in files)
        sub = h.submit("submit-key-0000000201", files=files)
        assert sub["status"] == "accepted", sub
        assert sub["file_count"] == 2
    finally:
        h.close()


def test_rejects_manifest_gradle_resources_and_prebuilt() -> None:
    h = _and000008_harness()
    try:
        forbidden = {
            "src/main/AndroidManifest.xml": b"<manifest/>",
            "build.gradle": b"plugins {}",
            "gradle/wrapper/gradle-wrapper.properties": b"distributionUrl=x",
            "app/build.gradle.kts": b"plugins {}",
            "src/main/res/values/strings.xml": b"<resources/>",
            "libs/helper.jar": b"PK\x03\x04",
            "libs/helper.aar": b"PK\x03\x04",
            "prebuilt/reference.apk": b"PK\x03\x04",
            "src/main/jniLibs/x86_64/libx.so": b"\x7fELF",
        }
        # Each probe submission needs its own idempotency key: a key may only
        # ever be replayed with an identical body.
        for index, (path, data) in enumerate(sorted(forbidden.items())):
            sub = h.submit(
                f"submit-key-00000002{index:02d}",
                files=_files(
                    {
                        path: data,
                        JAVA_ROOT + "CandidateBehavior.java": (
                            b"package org.androidpoc.and000008.candidate;\n"
                            b"public final class CandidateBehavior {}\n"
                        ),
                    }
                ),
            )
            assert sub["status"] == "rejected", (path, sub)
            assert sub["reason_code"] == "SOURCE_POLICY_REJECTED", (path, sub)
    finally:
        h.close()


def test_rejects_kotlin_and_outside_namespace_java() -> None:
    h = _and000008_harness()
    try:
        sub = h.submit(
            "submit-key-0000000203",
            files=make_source_files(extra={JAVA_ROOT + "Helper.kt": "package org.androidpoc.and000008.candidate"}),
        )
        assert sub["status"] == "rejected", sub

        bad_package = _files(
            {
                JAVA_ROOT + "CandidateBehavior.java": (
                    b"package com.asus.aihome;\npublic final class CandidateBehavior {}\n"
                )
            }
        )
        sub = h.submit("submit-key-0000000204", files=bad_package)
        assert sub["status"] == "rejected", sub
    finally:
        h.close()


def test_rejects_path_traversal_and_absolute_paths() -> None:
    h = _and000008_harness()
    try:
        behaviour = (
            b"package org.androidpoc.and000008.candidate;\npublic final class CandidateBehavior {}\n"
        )
        for index, path in enumerate(
            (
                "../../../etc/passwd",
                "/etc/passwd",
                "C:\\Windows\\System32\\cmd.exe",
                "src/main/java/org/androidpoc/and000008/candidate/../../../../escape.java",
                "\\\\server\\share\\evil.java",
            )
        ):
            sub = h.submit(
                f"submit-key-00000003{index:02d}",
                files=[
                    {"relative_path": path, "content_base64": base64.b64encode(b"x").decode("ascii")},
                    {
                        "relative_path": JAVA_ROOT + "CandidateBehavior.java",
                        "content_base64": base64.b64encode(behaviour).decode("ascii"),
                    },
                ],
            )
            assert sub["status"] == "rejected", (path, sub)
    finally:
        h.close()


def test_tree_hash_is_stable_and_content_addressed() -> None:
    h = _and000008_harness()
    try:
        a = h.submit("submit-key-0000000206", files=make_source_files(body="public final class A {}"))
        # Same content under a new key and a fresh candidate must collapse to the
        # same tree hash and must NOT create a new candidate.
        b = h.submit("submit-key-0000000207", files=make_source_files(body="public final class A {}"))
        assert a["tree_hash"] == b["tree_hash"]
        assert a["candidate_id"] == b["candidate_id"], (a, b)
        assert b["status"] in ("duplicate", "accepted"), b

        c = h.submit("submit-key-0000000208", files=make_source_files(body="public final class C {}"))
        assert c["tree_hash"] != a["tree_hash"]
        assert c["candidate_id"] != a["candidate_id"]
    finally:
        h.close()


def test_required_behaviour_source_is_enforced() -> None:
    h = _and000008_harness()
    try:
        sub = h.submit(
            "submit-key-0000000209",
            files=_files({JAVA_ROOT + "Other.java": b"package org.androidpoc.and000008.candidate;\n"}),
        )
        assert sub["status"] == "rejected", sub
        _s, feedback = h.get(
            "/v1/feedback",
            query={"candidate_id": "", "kind": "build"},
        )
    finally:
        h.close()


def test_generic_profile_keeps_the_same_freeze_discipline() -> None:
    """The mock profile is permissive about language but not about paths/size."""

    h = Harness()
    try:
        sub = h.submit("submit-key-0000000210", files=mock_submission())
        assert sub["status"] == "accepted", sub

        # Each probe needs its own idempotency key: a key replay must be identical.
        for index, path in enumerate(("../../escape.txt", "/abs/escape.txt", "C:\\escape.txt")):
            bad = h.submit(
                f"submit-key-00000004{index:02d}",
                files=[{"relative_path": path, "content_base64": base64.b64encode(b"x").decode("ascii")}],
            )
            assert bad["status"] == "rejected", (path, bad)

        # Duplicate and case-conflicting paths are refused.
        dup = h.submit(
            "submit-key-0000000212",
            files=[
                {"relative_path": "a/x.txt", "content_base64": base64.b64encode(b"1").decode("ascii")},
                {"relative_path": "a/x.txt", "content_base64": base64.b64encode(b"2").decode("ascii")},
            ],
        )
        assert dup["status"] == "rejected", dup

        # Build scripts and prebuilt binaries are refused by the generic profile
        # too, so the generic path is not a weaker door.
        script = h.submit(
            "submit-key-0000000213",
            files=[{"relative_path": "build.gradle", "content_base64": base64.b64encode(b"plugins {}").decode("ascii")}],
        )
        assert script["status"] == "rejected", script
    finally:
        h.close()


def test_and000008_profile_does_not_freeze_behaviour_signature() -> None:
    """Only the file's presence is required; no method signature is asserted."""

    h = _and000008_harness()
    try:
        # A behaviour file with an entirely different shape still validates.
        unusual = _files(
            {
                JAVA_ROOT + "CandidateBehavior.java": (
                    b"package org.androidpoc.and000008.candidate;\n"
                    b"public final class CandidateBehavior {\n"
                    b"    public void somethingEntirelyDifferent(int a, String b) {}\n"
                    b"    private static Object helper() { return null; }\n"
                    b"}\n"
                )
            }
        )
        sub = h.submit("submit-key-0000000213", files=unusual)
        assert sub["status"] == "accepted", sub
    finally:
        h.close()
