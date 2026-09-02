"""
lucid-attest-service: POST /v1/sign Lambda handler.

Phase 2: real signing. Calls straight into `cli.oidc_signer.sign_statement`
(vendored from a pinned lucid-assay source SHA -- see
.github/workflows/deploy.yml's "Vendor pinned cli.oidc_signer" step, and
its own comment for exactly why this narrow, three-file list and nothing
else). No fabricated placeholder is ever returned on a failure path --
same "no stub signature" discipline Phase 1 established here (a stub
would be worse than an error: it would look real) and the wider platform
already holds itself to everywhere else (oidc_signer's own --dry-run-sign,
lucid-episteme's fail-closed v0.1).

Caller-supplied identity. This service is invoked *by* a CI job (e.g. a
GitHub Actions workflow with `permissions: id-token: write`), not by the
CI runner itself -- there is no ambient OIDC environment inside a Lambda
execution context for `fetch_ambient_oidc_token()` to read from (see
cli.oidc_signer.sign_statement's own docstring on its `identity_token`
param, added specifically for this). The caller's already-minted token
travels in the standard `Authorization: Bearer <token>` header, not the
JSON body -- keeps Phase 1's existing request shape (a bare JSON array of
statement payloads) unchanged rather than reshaping it around a second
field every caller would need to add.

Request shape: unchanged from Phase 1 -- a JSON array of unsigned in-toto
Statement payloads, one call per pipeline run (not one per envelope).
Response shape: a JSON array of signed DSSE envelopes (`DSSEEnvelope.to_dict()`),
in the same order. All-or-nothing: if any statement in the batch fails to
sign, the whole request fails closed with a 502 rather than returning a
partial list a caller would have to reconcile against its own request to
figure out which entries actually got signed.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from cli.oidc_signer import AmbientIdentityError, sign_statement


class RequestValidationError(ValueError):
    """Raised when the request body doesn't match the expected shape --
    caught in `handler` and turned into a 400, never allowed to surface
    as an unhandled 500."""


class AuthorizationError(ValueError):
    """Raised when the Authorization header is missing or malformed --
    caught in `handler` and turned into a 401, distinct from
    RequestValidationError's 400 since this is a credentials problem,
    not a malformed-body one."""


class SigningError(RuntimeError):
    """Raised when Sigstore signing itself fails for one of the batch's
    statements -- caught in `handler` and turned into a 502, carrying
    which statement (by index) failed and why."""


def _parse_request_body(raw_body: str) -> List[Dict[str, Any]]:
    """Parses and validates the POST body: must be a JSON array of
    objects, each intended to become one signed DSSE envelope. Raises
    RequestValidationError on anything else -- malformed JSON, a
    non-list top level, or a non-object entry -- rather than letting a
    malformed request reach signing logic."""
    try:
        parsed = json.loads(raw_body or "")
    except json.JSONDecodeError as e:
        raise RequestValidationError(f"request body is not valid JSON: {e}") from e

    if not isinstance(parsed, list):
        raise RequestValidationError("request body must be a JSON array of unsigned statement payloads")
    if not parsed:
        raise RequestValidationError("request body must contain at least one statement payload")
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise RequestValidationError(f"entry {i} is not a JSON object")

    return parsed


def _extract_identity_token(headers: Dict[str, str]) -> str:
    """Extracts the caller's OIDC identity token from `Authorization:
    Bearer <token>`. API Gateway HTTP API (payload format 2.0) lowercases
    header names, but this also tolerates a mixed-case key defensively
    rather than assuming that normalization always holds.

    Raises AuthorizationError -- not AmbientIdentityError -- on a
    missing/malformed header: this is a credentials problem (401
    territory), never routed through sign_statement's own ambient-fetch
    fallback, which would raise a confusingly-unrelated "no ambient OIDC
    identity found" error about a fetch this service never attempts.
    """
    auth_header = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth_header = value
            break

    if not auth_header:
        raise AuthorizationError(
            "missing Authorization header -- the caller's own ambient OIDC "
            "identity token must be forwarded as 'Authorization: Bearer <token>'"
        )

    scheme, _, token = auth_header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthorizationError(
            "Authorization header must be in the form 'Bearer <token>'"
        )

    return token


def _sign_batch(statements: List[Dict[str, Any]], identity_token: str) -> List[Dict[str, Any]]:
    """Signs every statement in the batch, in order. All-or-nothing: the
    first failure raises SigningError immediately rather than returning a
    partial list -- see this module's docstring for why."""
    envelopes = []
    for i, statement in enumerate(statements):
        statement_bytes = json.dumps(statement).encode("utf-8")
        try:
            envelope = sign_statement(statement_bytes, identity_token=identity_token)
        except AmbientIdentityError as e:
            # Shouldn't happen -- identity_token is always non-empty by
            # the time this is called (_extract_identity_token already
            # validated it) -- but sign_statement() would only ever reach
            # its own ambient-fetch fallback on a falsy token, so this is
            # a real, if unexpected, signal worth its own diagnostic
            # rather than folding into the generic branch below.
            raise SigningError(f"statement {i}: no usable identity token reached Sigstore: {e}") from e
        except Exception as e:  # noqa: BLE001 - surface any signing failure uniformly, see module docstring
            raise SigningError(f"statement {i}: signing failed: {e}") from e
        envelopes.append(envelope.to_dict())
    return envelopes


def _response(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _handle_sign_request(statements: List[Dict[str, Any]], identity_token: str) -> Dict[str, Any]:
    try:
        envelopes = _sign_batch(statements, identity_token)
    except SigningError as e:
        return _response(502, {"error": "signing_failed", "message": str(e)})

    return _response(200, {"envelopes": envelopes})


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """API Gateway (HTTP API, payload format 2.0) Lambda entry point."""
    try:
        statements = _parse_request_body(event.get("body", ""))
    except RequestValidationError as e:
        return _response(400, {"error": "invalid_request", "message": str(e)})

    try:
        identity_token = _extract_identity_token(event.get("headers") or {})
    except AuthorizationError as e:
        return _response(401, {"error": "unauthorized", "message": str(e)})

    return _handle_sign_request(statements, identity_token)
