"""Critical-path layer: the real, deployed stack -- API Gateway, its JWT
authorizer, and the Lambda itself -- actually signs a statement through
real Fulcio/Rekor, end to end. This is the one thing
smoke-test-sign.yml already proved works manually, and assay.yml's
`deploy` job's own post-deploy step already proved as an inline check
(2026-09-14); ported here as an individually-reportable pytest case,
still the sole real-signing test in this suite's blocking tier -- see
conftest.py's own docstring for why that must stay true.

Needs LIVE_OIDC_TOKEN: skips itself rather than asserting against a
fabricated identity the authorizer would just as validly reject (that's
test_negative_security.py's job, not this file's)."""
from __future__ import annotations

import pytest

from tests.live.conftest import OIDC_TOKEN, build_statement, decode_envelope_payload, post_sign

pytestmark = pytest.mark.skipif(not OIDC_TOKEN, reason="LIVE_OIDC_TOKEN not set -- no real ambient identity to sign with")


def test_real_statement_is_signed_through_fulcio_and_rekor(client):
    statement = build_statement(
        subject_name="lucid-attest-service-live-suite",
        subject_sha256="1" * 64,
        predicate={"note": "tests/live/test_critical_path.py", "run_id": "pytest"},
    )

    resp = post_sign(client, [statement], token=OIDC_TOKEN)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    envelopes = body["envelopes"]
    assert len(envelopes) == 1
    envelope = envelopes[0]

    assert envelope["payloadType"] == "application/vnd.in-toto+json"

    # Round-trip fidelity: what actually got signed is really what was
    # sent, not silently corrupted or substituted en route.
    assert decode_envelope_payload(envelope) == statement

    sig = envelope["signatures"][0]["sig"]
    assert sig, "expected a non-empty signature"

    # A real Sigstore sign always produces a Rekor transparency-log
    # entry -- a non-null logIndex is what distinguishes a genuine sign
    # from a dry-run/fake response (see cli/oidc_signer.py's own
    # --dry-run-sign posture, which this must never be mistaken for).
    log_index = envelope["_rekor"]["logIndex"]
    assert log_index is not None, "expected a real Rekor transparency-log entry, got none"
