# Security Policy

`lucid-attest-service` is the isolated signer: `POST /v1/sign` holds real
Sigstore signing privilege on behalf of every tenant that calls it, and its
whole reason to exist is to keep that privilege out of an untrusted build
job. A vulnerability here — an auth bypass, a way to get an unintended
statement signed, or a way to impersonate a trusted builder identity — is
about as high-stakes as it gets on this platform. Please report it
privately rather than filing a public issue.

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting for this repo:

**[github.com/lucid-provenance/lucid-attest-service/security/advisories/new](https://github.com/lucid-provenance/lucid-attest-service/security/advisories/new)**
(also reachable from the repo's **Security** tab → **Report a vulnerability**)

Please include, where you can:
- A description of the issue and its potential impact
- Steps to reproduce, or a minimal proof of concept
- The affected commit SHA or file(s)

## Scope

In scope:
- The `POST /v1/sign` endpoint itself and anything gating who's allowed to
  call it
- OIDC/identity verification of the calling workflow before a signature is
  issued
- The isolated-builder-identity trust boundary this service exists to
  enforce (`TRUSTED_CONTROL_PLANE_BUILDER_IDS` on the `lucid-assay` side)
- Vendored signing code (`src/cli/oidc_signer.py`, pinned from
  `lucid-assay` via `SIGNER_SOURCE_SHA`) and how it's wired into this
  service's Lambda handler
- Deployment configuration (`.github/workflows/deploy.yml`,
  `sign-client.yml`) — pinning, permissions, secret handling

Out of scope:
- Vulnerabilities in third-party dependencies themselves — please report
  those upstream (Dependabot already tracks and patches these here)
- The `sigstore-python` library's own cryptographic implementation

## Supported Versions

This project doesn't yet publish versioned releases — only the latest
commit on `main` is supported. A fix lands as a normal PR, not a backport.

## Response

This is maintained on a best-effort basis, not under a formal SLA. Given
what this service holds, a credible report will get priority attention —
but we'll acknowledge and keep you updated as promptly as we can, not on a
guaranteed timeline.
