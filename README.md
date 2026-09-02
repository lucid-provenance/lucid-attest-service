# lucid-attest-service

Serverless signing service (Lucid roadmap #12) — a versioned
`POST /v1/sign` endpoint replacing the SHA-pinned, per-caller signer
workflow every tenant CI platform runs today. Evolves out of
[lucid-attest](https://github.com/lucid-provenance/lucid-attest); see that
repo's `sign.yml` and Milestone #18 Dockerfile for the signing logic and
narrow-vendoring discipline this service builds on.

## Status: Phase 1 scaffold

The mechanical path is proven — Lambda + API Gateway (HTTP API) + a
GitHub Actions OIDC deploy role, no static AWS credentials anywhere — but
`POST /v1/sign` does not sign anything yet. It validates the request
shape and returns `501 Not Implemented` with an honest explanation. See
`src/app.py`'s module docstring for why a stub signature would be worse
than an error.

Phase 2 (not yet built): vendor `cli.oidc_signer.sign_file_to_envelope`
from a pinned `lucid-assay` source SHA, the same narrow, hand-picked file
list `lucid-attest`'s own Dockerfile already uses — not a rewrite.

## Request shape

`POST /v1/sign` takes a JSON array of unsigned in-toto Statement
payloads and (once Phase 2 lands) returns a JSON array of signed DSSE
envelopes, in the same order — one call handles every statement a
pipeline run needs signed (e.g. `assay/v1` + `slsa/v1` together), not one
call per envelope. See the Lucid vault's `#12` milestone note, "single
call vs one per envelope," for why.

## Deploy

GitHub Actions (`.github/workflows/deploy.yml`) deploys on every push to
`main`, via OIDC — no local `sam deploy` credentials needed for normal
use. One manual, one-time prerequisite: the SAM artifacts bucket
(`lucid-attest-service-sam-artifacts-133307902115-us-east-1-an`,
`us-east-1` — created in S3's account-Regional namespace, hence the
account+region suffix on the name) must exist before the first deploy —
the deploy role deliberately has no `s3:CreateBucket` permission, since
bucket creation is a rarer, more sensitive operation kept as a human,
out-of-band step rather than something CI can do itself.

Local development: `sam build && sam local invoke SignFunction`.
