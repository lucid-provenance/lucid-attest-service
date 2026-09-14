"""Request-validation layer: confirms the actually-deployed Lambda's own
body-shape checks (app.py's `_parse_request_body`) are really wired up
end to end -- not re-deriving that logic (tests/test_app.py already
covers it exhaustively by calling `handler()` directly, in-process). The
value-add here is catching a real deployment gap an in-process test
can't see: a packaging mistake that drops app.py's own validation, a
misrouted API Gateway integration, or a response-shape drift between
what's actually returned and what a caller like sign-client.yml expects.

Every case here needs LIVE_OIDC_TOKEN -- a real token is required to
clear the API Gateway edge authorizer before Lambda is even invoked
(see test_negative_security.py for the "no/garbage token" cases, which
correctly never reach here). Crucially, none of these produce a real
Sigstore/Rekor side effect: `handler()` calls `_parse_request_body`
before it ever calls `_extract_identity_token` or reaches signing (see
app.py's own `handler()`), so a malformed body is rejected before
signing is even attempted, real token or not -- see conftest.py's own
docstring for why that matters here specifically."""
from __future__ import annotations

import pytest

from tests.live.conftest import OIDC_TOKEN, SIGN_ENDPOINT, post_sign

pytestmark = pytest.mark.skipif(not OIDC_TOKEN, reason="LIVE_OIDC_TOKEN not set -- no real ambient identity to clear the edge authorizer with")


def test_malformed_json_body_is_rejected(client):
    resp = client.post(
        SIGN_ENDPOINT,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {OIDC_TOKEN}"},
        content="{not valid json",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


def test_non_list_top_level_is_rejected(client):
    resp = post_sign(client, {"not": "a list"}, token=OIDC_TOKEN)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


def test_empty_array_is_rejected(client):
    resp = post_sign(client, [], token=OIDC_TOKEN)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


def test_non_object_entry_is_rejected(client):
    resp = post_sign(client, ["not-an-object"], token=OIDC_TOKEN)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"
