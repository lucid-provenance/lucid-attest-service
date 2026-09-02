"""
lucid-attest-service: POST /v1/sign Lambda handler.

Phase 1 (this file, as it stands today): proves the mechanical path only
-- Lambda receives a request through API Gateway, validates its shape,
and returns a clear, honest "not implemented yet" response. It never
signs anything and never returns a value that could be mistaken for a
real signed envelope -- same "no fabricated placeholder" discipline
lucid-assay/lucid-attest already hold themselves to everywhere else
(oidc_signer's --dry-run-sign is explicit about simulating, never silent;
lucid-episteme's v0.1 fails closed rather than faking a score). A stub
signature here would be worse than an error: it would look real.

Phase 2 (not yet built): replace the body of `_handle_sign_request`
below with real calls into `cli.oidc_signer.sign_file_to_envelope`,
vendored from a pinned lucid-assay source SHA the same narrow, hand-
picked way lucid-attest's own Milestone #18 Dockerfile already does
(cli/sign.py, cli/oidc_signer.py, cli/provenance.py, cli/common.py,
cli/slsa_provenance.py, cli/parsers/lockfiles.py -- see that repo's
Dockerfile header for exactly why each one and nothing else). Request
shape (a list of unsigned statement payloads in, a list of signed DSSE
envelopes out, in the same order) is designed now so Phase 2 doesn't
need to change the API contract, only what happens inside it -- see the
Lucid vault's #12 milestone note, "single call vs one per envelope."
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


class RequestValidationError(ValueError):
    """Raised when the request body doesn't match the expected shape --
    caught in `handler` and turned into a 400, never allowed to surface
    as an unhandled 500."""


def _parse_request_body(raw_body: str) -> List[Dict[str, Any]]:
    """Parses and validates the POST body: must be a JSON array of
    objects, each intended to become one signed DSSE envelope. Raises
    RequestValidationError on anything else -- malformed JSON, a
    non-list top level, or a non-object entry -- rather than letting a
    malformed request reach signing logic (today, or in Phase 2)."""
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


def _handle_sign_request(statements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Phase 1: deliberately does not sign anything. Returns 501 with an
    honest explanation, echoing back only the count and shape it
    validated -- never a value shaped like a real signed envelope, so a
    caller integrating against this endpoint early can't mistake this
    response for a working signer."""
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "error": "not_implemented",
                "message": (
                    "lucid-attest-service is a Phase 1 scaffold: the mechanical "
                    "path (Lambda + API Gateway + OIDC deploy) is proven, but "
                    "no signing logic is wired in yet. See the Lucid vault's "
                    "#12 milestone note for Phase 2 status."
                ),
                "received_statement_count": len(statements),
            }
        ),
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """API Gateway (HTTP API, payload format 2.0) Lambda entry point."""
    try:
        statements = _parse_request_body(event.get("body", ""))
    except RequestValidationError as e:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "invalid_request", "message": str(e)}),
        }

    return _handle_sign_request(statements)
