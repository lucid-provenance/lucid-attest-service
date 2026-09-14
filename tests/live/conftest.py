"""Shared fixtures/helpers for the *live* post-deployment functional suite
(tests/live/) -- distinct from tests/test_app.py, which exercises
`handler()` in-process with a hand-built `event` dict and no real network
call. Everything here makes a real, over-the-wire HTTPS request against
the actually-deployed API Gateway + Lambda (SIGN_ENDPOINT) -- catching
deployment-config drift (a broken IAM permission, a stale Lambda code
package, a misconfigured JWT authorizer) that an in-process unit test
structurally cannot see. Mirrors lucid-dsse-collector's own tests/live/
suite in spirit and structure; see this module's own comments for the
specific places this service's shape (stateless, single-endpoint,
OIDC-gated) makes a direct file-for-file port wrong rather than just
different.

Configuration (env vars):
  LIVE_SIGN_ENDPOINT -- default: this repo's own known-stable endpoint
                        (same literal assay.yml's own SIGN_ENDPOINT uses).
  LIVE_OIDC_TOKEN -- a real, already-minted ambient GitHub Actions OIDC
                     identity token (audience "sigstore"), set by the
                     calling workflow step (the same
                     .github/actions/mint-sigstore-oidc-token composite
                     action smoke-test-sign.yml and assay.yml's `deploy`
                     job already use) immediately before invoking pytest.
                     There is no ambient OIDC environment inside this
                     process to mint one itself (that's only ever
                     available to a real `id-token: write` GitHub Actions
                     job, not an arbitrary pytest run) -- optional, no
                     default: every test that needs a real token to clear
                     the API Gateway edge authorizer skips itself when
                     this is unset, rather than assuming a local run has
                     one available (see test_request_validation.py and
                     test_critical_path.py).

Why there's no tests/live/test_idempotency.py or
tests/live/test_deployment_verification.py here, unlike
lucid-dsse-collector's own suite:
  - Idempotency: dsse-collector dedups by content-addressed
    report_sha256, a real property of its own storage layer worth
    regression-testing against a live DB. This service has no storage
    layer and no dedup semantics to test -- every real sign call mints a
    genuinely new Fulcio certificate and a new Rekor transparency-log
    entry, even for byte-identical input, by design (Sigstore's own
    ephemeral-identity model, not something this service could make
    idempotent even if it wanted to).
  - Deployment-verification/digest-binding: dsse-collector's version
    checks that a submitted statement's subject digest round-trips
    unchanged through a real Postgres write-then-read. This service has
    no persisted row to read back -- the closest honest equivalent
    (payload round-trip fidelity through a real sign call) is folded
    into test_critical_path.py instead of its own file, and the
    orthogonal question of "does the live Lambda's own AWS-reported
    CodeSha256 match what CI attested" is answered by a diagnostic CI
    step (assay.yml's `deploy` job, "Capture live Lambda code
    identity"), not a pytest case -- there is no live HTTP surface that
    exposes CodeSha256 for a test here to assert against.

A cost/hygiene note the dsse-collector suite has no equivalent of: every
test in this package that reaches `_sign_batch` (a request that clears
both the API Gateway authorizer and app.py's own body validation) mints a
REAL, PERMANENT, PUBLIC Rekor transparency-log entry -- not a
side-effect-free assertion against a disposable test double.
test_critical_path.py's one happy-path case does this once;
test_batch_stress.py's does it BATCH_SIZE more times. Every other test
here is rejected before signing (by the edge authorizer, for
test_negative_security.py, or by app.py's own body validation, for
test_request_validation.py) and produces no Rekor entry at all.

assay.yml's `deploy` job runs every file in this package, on every real
push to main -- there is no separate nightly/scheduled workflow (one was
tried and removed the same day, 2026-09-14: this platform doesn't run
tests on a clock, only against a real deploy). What keeps this cheap on
the common case is staging: each file runs as its own step, in ascending
order of real cost (negative-security -> request-validation ->
critical-path -> stress), so GitHub Actions' own "stop the job at the
first failing step" behavior means a real-signing step never runs at all
once an earlier, free step has already failed.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx
import pytest

SIGN_ENDPOINT = os.environ.get(
    "LIVE_SIGN_ENDPOINT", "https://84qfszyk5h.execute-api.us-east-1.amazonaws.com/v1/sign"
)
# No default: absent means "not running from inside a real id-token:
# write GitHub Actions job" -- tests that need a real token to clear the
# API Gateway edge authorizer skip themselves rather than asserting
# against a fabricated one, which the authorizer would just as validly
# reject as no token at all (indistinguishable from the thing
# test_negative_security.py already covers on purpose).
OIDC_TOKEN = os.environ.get("LIVE_OIDC_TOKEN")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(timeout=35.0) as c:  # > this function's own 30s Lambda timeout
        yield c


def post_sign(client: httpx.Client, statements: list[dict[str, Any]], *, token: str | None) -> httpx.Response:
    """POSTs the raw JSON array `handler()` expects. `token=None` sends no
    Authorization header at all (the missing-header case); an
    empty/malformed string exercises the malformed-header case -- both
    are for test_negative_security.py to construct, not this helper to
    special-case."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(SIGN_ENDPOINT, headers=headers, content=json.dumps(statements))


def build_statement(*, subject_name: str, subject_sha256: str, predicate: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": subject_name, "digest": {"sha256": subject_sha256}}],
        "predicateType": "https://lucid-provenance.io/attestations/live-suite-probe/v1",
        "predicate": predicate if predicate is not None else {},
    }


def decode_envelope_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Decodes a signed DSSE envelope's own base64 payload back into the
    statement dict -- used to assert round-trip fidelity (what came back
    is really what was sent, not silently corrupted or substituted)."""
    return json.loads(base64.b64decode(envelope["payload"]))
