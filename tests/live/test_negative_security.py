"""Negative-security layer: confirms the *actually-deployed* edge (API
Gateway's JWT authorizer, added to template.yaml 2026-09-14) genuinely
rejects a request before it ever reaches Lambda -- not re-deriving the
authorizer's own logic (that's AWS's, not this repo's, to test) but
catching real deployment-config drift (the authorizer misconfigured,
disabled, or pointed at the wrong issuer/audience) that an in-process
test of app.py alone structurally cannot see, since app.py never runs at
all for a request the authorizer rejects.

Doubles as this suite's cheapest reachability signal -- the same role
lucid-dsse-collector's own test_health.py plays, adapted: this service
has no unauthenticated /healthz endpoint to hit (it has exactly one
route, and it's gated), so "an unauthenticated request gets a real,
prompt 401" is the cheapest real proof the stack is up at all, as
opposed to hanging/erroring in a way that would suggest a genuine outage.

Neither test here needs LIVE_OIDC_TOKEN -- they're deliberately sent with
no token or a garbage one, so they run unconditionally, including from a
bare local `pytest tests/live/` with no ambient GitHub Actions identity
available."""
from __future__ import annotations

from tests.live.conftest import build_statement, post_sign


def test_missing_authorization_header_is_rejected(client):
    statement = build_statement(subject_name="probe", subject_sha256="0" * 64)
    resp = post_sign(client, [statement], token=None)
    assert resp.status_code == 401, resp.text


def test_garbage_bearer_token_is_rejected(client):
    """Not a real JWT at all -- the authorizer must reject this before
    app.py's own _extract_identity_token or sign_statement ever runs, so
    this produces no Sigstore/Rekor side effect regardless of how
    malformed the token is."""
    statement = build_statement(subject_name="probe", subject_sha256="0" * 64)
    resp = post_sign(client, [statement], token="not-a-real-jwt")
    assert resp.status_code == 401, resp.text
