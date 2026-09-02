# lucid-attest-service

Serverless signing service (Lucid roadmap #12) — a versioned
`POST /v1/sign` endpoint replacing the SHA-pinned, per-caller signer
workflow every tenant CI platform runs today. Evolves out of
[lucid-attest](https://github.com/lucid-provenance/lucid-attest); see that
repo's `sign.yml` and Milestone #18 Dockerfile for the signing logic and
narrow-vendoring discipline this service builds on.

## Status: Phase 2 — real signing

`POST /v1/sign` calls straight into `cli.oidc_signer.sign_statement`,
vendored at deploy time from a pinned `lucid-assay` source SHA
(`.github/workflows/deploy.yml`'s `SIGNER_SOURCE_SHA`) — the same
narrow-file, build-time-checkout pattern `lucid-attest`'s own Milestone
#18 `build-signer-image.yml` uses, verified empirically to be an even
narrower list than that image's (`cli/__init__.py`, `cli/common.py`,
`cli/oidc_signer.py` only — this service calls `sign_statement()` on
bytes it already has in memory, never the file-based
`sign_file_to_envelope()` wrapper or the CLI subcommands built on it, so
`cli/sign.py`, `cli/provenance.py`, `cli/slsa_provenance.py`, and
`cli/parsers/lockfiles.py` aren't needed here). Never returns a
fabricated placeholder on a failure path — see `src/app.py`'s module
docstring.

## Request shape

`POST /v1/sign` takes a JSON array of unsigned in-toto Statement
payloads and returns `{"envelopes": [...]}`, a JSON array of signed DSSE
envelopes in the same order — one call handles every statement a
pipeline run needs signed (e.g. `assay/v1` + `slsa/v1` together), not one
call per envelope. See the Lucid vault's `#12` milestone note, "single
call vs one per envelope," for why.

**Authorization: Bearer \<token\>** is required on every request — the
caller's own already-minted OIDC identity token (e.g. a GitHub Actions
job's ambient token, fetched via its own `permissions: id-token: write`)
forwarded as-is. This service never fetches its own ambient OIDC token —
there is no ambient GitHub Actions/GitLab CI environment inside a Lambda
execution context for `fetch_ambient_oidc_token()` to read from, only the
caller (the actual CI runner) has one. Missing or malformed header → 401.

**All-or-nothing**: if any statement in the batch fails to sign, the
whole request fails closed with a 502 rather than returning a partial
list a caller would have to reconcile against its own request to figure
out which entries actually got signed.

| Status | Meaning |
|---|---|
| 200 | `{"envelopes": [...]}`, all statements signed |
| 400 | malformed request body (not JSON, not an array, non-object entry) |
| 401 | missing/malformed `Authorization` header |
| 502 | Sigstore signing failed for one or more statements — message names which index and why |

## Deploy

GitHub Actions (`.github/workflows/deploy.yml`) deploys on every push to
`main`, via OIDC — no local `sam deploy` credentials needed for normal
use. Two things happen before `sam build`:

1. A read-only checkout of `lucid-assay` at the pinned `SIGNER_SOURCE_SHA`
   into `_signer/`.
2. The narrow `cli/oidc_signer.py` file list (see Status above) copied
   from there into `src/cli/` — regenerated fresh from the pin on every
   deploy, never a static copy committed into this repo.

One manual, one-time prerequisite: the SAM artifacts bucket
(`lucid-attest-service-sam-artifacts-133307902115-us-east-1-an`,
`us-east-1` — created in S3's account-Regional namespace, hence the
account+region suffix on the name) must exist before the first deploy —
the deploy role deliberately has no `s3:CreateBucket` permission, since
bucket creation is a rarer, more sensitive operation kept as a human,
out-of-band step rather than something CI can do itself.

## Local development

Vendor the signer source by hand first (mirrors what `deploy.yml` does
at deploy time — see its own comments for exactly why this is a
build-time step, not a committed copy):

```bash
mkdir -p src/cli
cp /path/to/lucid-assay/cli/__init__.py    src/cli/__init__.py
cp /path/to/lucid-assay/cli/common.py      src/cli/common.py
cp /path/to/lucid-assay/cli/oidc_signer.py src/cli/oidc_signer.py
```

Then `sam build --use-container && sam local invoke SignFunction`
(`--use-container`: see `deploy.yml`'s own comment on why a plain local
build isn't used here). A real Sigstore round-trip additionally needs a
valid caller-supplied identity token in the invoke event's
`Authorization` header — see `events/` (if present) or construct one by
hand against the `handler(event, context)` shape in `src/app.py`.
