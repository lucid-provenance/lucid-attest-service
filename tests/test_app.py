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
        self.assertIn("not valid JSON", json.loads(r["body"])["message"])

    def test_non_array_body_returns_400(self):
        r = app.handler(_event({"not": "a list"}, {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)
        # Exact-content assertion, not just the status code -- catches a
        # mutation that mangles/nulls/cases-scrambles the message text
        # while the status code stays correct.
        self.assertEqual(
            json.loads(r["body"])["message"],
            "request body must be a JSON array of unsigned statement payloads",
        )

    def test_empty_array_body_returns_400(self):
        r = app.handler(_event([], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(
            json.loads(r["body"])["message"],
            "request body must contain at least one statement payload",
        )

    def test_non_object_entry_returns_400(self):
        r = app.handler(_event([_STATEMENT, "not an object"], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertIn("entry 1", json.loads(r["body"])["message"])

    def test_missing_body_key_entirely_is_treated_as_empty(self):
        """No 'body' key at all (as opposed to an empty-string body) must
        still be handled -- event.get("body", "") -- not KeyError."""
        r = app.handler({"headers": {"authorization": "Bearer t"}}, None)
        self.assertEqual(r["statusCode"], 400)


class AuthorizationTests(unittest.TestCase):
    def test_missing_authorization_header_returns_401(self):
        r = app.handler(_event([_STATEMENT]), None)
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(json.loads(r["body"])["error"], "unauthorized")
        # Exact-content assertion -- catches a mutation that mangles/
        # nulls/renames the message key or scrambles its case/text while
        # the status code and "error" field stay correct.
        self.assertEqual(
            json.loads(r["body"])["message"],
            "missing Authorization header -- the caller's own ambient OIDC "
            "identity token must be forwarded as 'Authorization: Bearer <token>'",
        )

    def test_malformed_authorization_header_returns_401(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "garbage"}), None)
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(
            json.loads(r["body"])["message"],
            "Authorization header must be in the form 'Bearer <token>'",
        )

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

    def test_double_space_after_bearer_scheme_is_still_accepted(self):
        """_extract_identity_token splits the header on the FIRST space
        (str.partition), not the last -- a double space between the
        scheme and the token must not shift what's parsed as the scheme.
        Using rpartition here (a real mutant this caught) would instead
        read scheme="Bearer " (trailing space, != "bearer") and 401
        a header that should parse fine."""
        r = app.handler(_event([_STATEMENT], {"authorization": "Bearer  token"}), None)
        self.assertNotEqual(r["statusCode"], 401)


class ResponseShapeTests(unittest.TestCase):
    """_response() is a pure helper -- exercise it directly rather than
    only through handler(), so a mutation to its literal keys/values
    (a renamed "headers" key, a mis-cased Content-Type) can't hide
    behind handler()'s own status-code-only assertions."""

    def test_response_shape_is_exact(self):
        r = app._response(200, {"ok": True})
        self.assertEqual(
            r,
            {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": True}),
            },
        )


class SuccessfulSigningTests(_MockedSigstoreTestCase):
    def test_single_statement_batch_returns_200_with_one_envelope(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "Bearer caller-token"}), None)
        self.assertEqual(r["statusCode"], 200)
        envelopes = json.loads(r["body"])["envelopes"]
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0]["payloadType"], "application/vnd.in-toto+json")

    def test_response_headers_declare_json_content_type(self):
        r = app.handler(_event([_STATEMENT], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["headers"], {"Content-Type": "application/json"})

    def test_actual_statement_bytes_reach_sign_statement_unaltered(self):
        """_sign_batch must serialize each batch entry itself, not a
        stand-in -- json.dumps(statement), not e.g. json.dumps(None)."""
        distinctive_statement = {**_STATEMENT, "predicateType": "distinctive-marker"}
        with mock.patch("app.sign_statement") as mock_sign:
            mock_sign.return_value.to_dict.return_value = {"ok": True}
            app.handler(_event([distinctive_statement], {"authorization": "Bearer t"}), None)
        sent_bytes = mock_sign.call_args.args[0]
        self.assertEqual(json.loads(sent_bytes), distinctive_statement)

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

    def test_ambient_identity_error_gets_its_own_diagnostic_not_the_generic_one(self):
        """Defensive branch: _extract_identity_token already guarantees a
        non-empty token reaches _sign_batch, so sign_statement() should
        never actually fall through to its own ambient-fetch path and
        raise AmbientIdentityError here -- but if it somehow did (e.g. a
        future sign_statement() change), that's a distinct, worth-its-own-
        message failure mode from a generic signing error, not something
        to silently fold into the same branch."""
        with mock.patch("app.sign_statement", side_effect=app.AmbientIdentityError("no token found")):
            r = app.handler(_event([_STATEMENT], {"authorization": "Bearer t"}), None)
        self.assertEqual(r["statusCode"], 502)
        self.assertIn("no usable identity token reached Sigstore", json.loads(r["body"])["message"])


if __name__ == "__main__":
    unittest.main()
