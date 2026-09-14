"""Stress layer: signs a genuinely multi-statement batch through the real
deployed Lambda -- `_sign_batch` signs sequentially, in-process, inside
one invocation bounded by template.yaml's own 30s function timeout (see
Globals.Function.Timeout), so this is the one realistic way that budget
could ever actually get exhausted in production (a caller batching many
statements in one call, not a single large one -- there is no per-
statement size limit in this service worth stress-testing the way
lucid-dsse-collector's own tests/live/test_payload_stress.py stress-
tests payload *size*).

`@pytest.mark.stress`, same convention as lucid-dsse-collector's own
suite: on-demand/nightly only (see regression-live.yml), never part of
assay.yml's `deploy` job's blocking gate. Doubly so here: this produces
BATCH_SIZE real, permanent, public Rekor transparency-log entries in one
run, not just one -- see conftest.py's own docstring for why the
blocking tier is held to exactly one. BATCH_SIZE is deliberately modest
(5, not lucid-dsse-collector's "tens of MB" scale) -- large enough to
exercise real sequential-signing behavior across multiple statements,
small enough that five real Fulcio/Rekor round-trips comfortably clear
the 30s function timeout even on a slow day, rather than turning a
latency blip in Sigstore's own public infrastructure into a false
failure here."""
from __future__ import annotations

import pytest

from tests.live.conftest import OIDC_TOKEN, build_statement, post_sign

pytestmark = [
    pytest.mark.stress,
    pytest.mark.skipif(not OIDC_TOKEN, reason="LIVE_OIDC_TOKEN not set -- no real ambient identity to sign with"),
]

BATCH_SIZE = 5


def test_multi_statement_batch_is_signed_in_one_call(client):
    statements = [
        build_statement(subject_name=f"lucid-attest-service-stress-{i}", subject_sha256=f"{i:064x}")
        for i in range(BATCH_SIZE)
    ]

    resp = post_sign(client, statements, token=OIDC_TOKEN)
    assert resp.status_code == 200, resp.text

    envelopes = resp.json()["envelopes"]
    assert len(envelopes) == BATCH_SIZE

    for i, envelope in enumerate(envelopes):
        assert envelope["signatures"][0]["sig"], f"entry {i}: expected a non-empty signature"
        assert envelope["_rekor"]["logIndex"] is not None, f"entry {i}: expected a real Rekor transparency-log entry"
