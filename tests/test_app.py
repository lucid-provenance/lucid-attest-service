"""
Tests for src/app.py's Lambda handler.

No real network calls, no AWS/Sigstore credentials needed to run this
suite -- every "successful signing" test mocks Sigstore's own library
calls (SigningContext, IdentityToken, Statement, ClientTrustConfig) the
same way lucid-assay's own test suite mocks them for cli.oidc_signer, so
this can run safely in CI on a fork PR with no secrets at all.

Requires the vendored cli.oidc_signer.py to already be on sys.path (see
this repo's own CI workflow's "Vendor pinned cli.oidc_signer" step, or
README's "Local development" section for the equivalent by-hand steps)
-- this suite doesn't vendor it itself.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import app  # noqa: E402

_STATEMENT = {
    "_type": "https://in-toto.io/Statement/v1",
    "subject": [{"name": "r", "digest": {"sha256": "a" * 64}}],
    "predicateType": "test",
    "predicate": {},
}


def _event(body, headers=None):
    return {"body": json.dumps(body) if not isinstance(body, str) else body, "headers": headers or {}}


def _fake_sigstore_bundle_json():
    return json.dumps({
        "messageSignature": {"signature": "c2ln", "messageDigest": {}},
        "verificationMaterial": {"certificate": {"rawBytes": "Y2VydA=="}},
    })


class _MockedSigstoreTestCase(unittest.TestCase):
    """Base class providing a mocked, successful Sigstore signing path --
    fetch_ambient_oidc_token is mocked to raise if called at all, so any
    test relying on this base fails loudly if a code path accidentally
    falls back to ambient fetch instead of using the caller-supplied
    token."""

    def setUp(self):
        patchers = [
            mock.patch("cli.oidc_signer.fetch_ambient_oidc_token"),
            mock.patch("sigstore.sign.SigningContext"),
            mock.patch("sigstore.oidc.IdentityToken"),
            mock.patch("sigstore.dsse.Statement"),
            mock.patch("sigstore.models.ClientTrustConfig"),
        ]
        (
            self.mock_fetch,
            self.mock_signing_context_cls,
            self.mock_identity_token_cls,
            _,
            _,
        ) = (p.start() for p in patchers)
        for p in patchers:
            self.addCleanup(p.stop)

        self.mock_fetch.side_effect = AssertionError("fetch_ambient_oidc_token must not be called")

        mock_signer = mock.MagicMock()
        mock_bundle = mock.MagicMock()
        mock_bundle.to_json.return_value = _fake_sigstore_bundle_json()
        mock_signer.sign_dsse.return_value = mock_bundle
        self.mock_signing_context_cls.from_trust_config.return_value.signer.return_value.__enter__.return_value = (
            mock_signer
        )


class RequestValidationTests(unittest.TestCase):
    def test_malformed_json_body_returns_400(self):
        r = app.handler(_event("not json"), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["error"], "invalid_request")

    def test_non_array_body_returns_400(self):
        r = app.handler(_event({"not": "a list"}, {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_empty_array_body_returns_400(self):
        r = app.handler(_event([], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_non_object_entry_returns_400(self):
        r = app.handler(_event([_STATEMENT, "not an object"], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertIn("entry 1", json.loads(r["body"])["message"])


class AuthorizationTests(unittest.TestCase):
    def test_missing_authorization_header_returns_401(self):
        r = app.handler(_event([_STATEMENT]), None)
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(json.loads(r["body"])["error"], "unauthorized")

    def test_malformed_authorization_header_returns_401(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "garbage"}), None)
        self.assertEqual(r["statusCode"], 401)

    def test_empty_bearer_token_returns_401(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "Bearer "}), None)
        self.assertEqual(r["statusCode"], 401)

    def test_wrong_scheme_returns_401(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "Basic dXNlcjpwYXNz"}), None)
        self.assertEqual(r["statusCode"], 401)

    def test_mixed_case_header_key_is_accepted(self):
        """API Gateway HTTP API lowercases header names, but this
        shouldn't assume that normalization always holds."""
        r = app.handler(_event([_STATEMENT], {"Authorization": "Bearer x"}), None)
        # Gets past the auth check -- may still fail later on real
        # signing (no mock here), but must not be a 401.
        self.assertNotEqual(r["statusCode"], 401)


class SuccessfulSigningTests(_MockedSigstoreTestCase):
    def test_single_statement_batch_returns_200_with_one_envelope(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "Bearer caller-token"}), None)
        self.assertEqual(r["statusCode"], 200)
        envelopes = json.loads(r["body"])["envelopes"]
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0]["payloadType"], "application/vnd.in-toto+json")

    def test_batch_of_three_returns_envelopes_in_same_order(self):
        statements = [
            {**_STATEMENT, "predicateType": "a"},
            {**_STATEMENT, "predicateType": "b"},
            {**_STATEMENT, "predicateType": "c"},
        ]
        r = app.handler(_event(statements, {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 200)
        envelopes = json.loads(r["body"])["envelopes"]
        self.assertEqual(len(envelopes), 3)

    def test_caller_supplied_token_reaches_identity_token_not_ambient_fetch(self):
        app.handler(_event([_STATEMENT], {"authorization": "Bearer exact-caller-token"}), None)
        self.mock_fetch.assert_not_called()
        self.mock_identity_token_cls.assert_called_once_with("exact-caller-token")


class SigningFailureTests(unittest.TestCase):
    def test_signing_failure_returns_502_naming_the_failing_index(self):
        # Patched at app.sign_statement, not cli.oidc_signer.sign_statement --
        # app.py imports the name directly (`from cli.oidc_signer import
        # sign_statement`), so it's app's own module-level binding that
        # app._sign_batch() actually calls. Patching the origin module's
        # attribute instead would silently leave app.sign_statement
        # pointing at the real, unmocked function.
        with mock.patch("app.sign_statement", side_effect=RuntimeError("Sigstore signing failed: boom")):
            r = app.handler(_event([_STATEMENT], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 502)
        body = json.loads(r["body"])
        self.assertEqual(body["error"], "signing_failed")
        self.assertIn("statement 0", body["message"])

    def test_second_statement_failing_still_names_its_own_index_not_zero(self):
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return mock.MagicMock(to_dict=lambda: {"ok": True})
            raise RuntimeError("boom")

        with mock.patch("app.sign_statement", side_effect=side_effect):
            r = app.handler(_event([_STATEMENT, _STATEMENT], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 502)
        self.assertIn("statement 1", json.loads(r["body"])["message"])

    def test_failed_batch_response_never_carries_a_partial_envelopes_list(self):
        """All-or-nothing: a caller must never have to reconcile a partial
        success list against its own request to figure out what actually
        got signed."""
        with mock.patch("app.sign_statement", side_effect=RuntimeError("boom")):
            r = app.handler(_event([_STATEMENT], {"authorization": "Bearer t"}), None)
        self.assertNotIn("envelopes", json.loads(r["body"]))


if __name__ == "__main__":
    unittest.main()
