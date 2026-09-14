"""Stress layer: signs a genuinely multi-statement batch through the real
deployed Lambda -- `_sign_batch` signs sequentially, in-process, inside
one invocation bounded by template.yaml's own 30s function timeout (see
Globals.Function.Timeout), so this is the one realistic way that budget
could ever actually get exhausted in production (a caller batching many
statements in one call, not a single large one -- there is no per-
statement size limit in this service worth stress-testing the way
lucid-dsse-collector's own tests/live/test_payload_stress.py stress-
tests payload *size*).

`@pytest.mark.stress` -- borrowed from lucid-dsse-collector's own
marker name, but a different meaning here: that repo uses it to keep an
expensive case off a nightly/on-demand schedule; this platform doesn't
run tests on a clock at all (see assay.yml's `deploy` job's own comment
for why a schedule trigger was tried and removed the same day,
2026-09-14). Here the marker exists so `pytest -m "not stress"` can
still isolate this case locally -- assay.yml's `deploy` job runs it as
its own last-and-priciest staged step (Stage 4/4), reached only once
every cheaper stage has already passed. This produces BATCH_SIZE real,
permanent, public Rekor transparency-log entries in one run, not just
one -- see conftest.py's own docstring for why every earlier stage is
held to zero. BATCH_SIZE is deliberately modest
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
