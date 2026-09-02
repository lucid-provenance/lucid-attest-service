# lucid-attest-service

Serverless signing service (Lucid roadmap #12) — a versioned
`POST /v1/sign` endpoint replacing the SHA-pinned, per-caller signer
workflow every tenant CI platform runs today. Evolves out of
[lucid-attest](https://github.com/lucid-provenance/lucid-attest); see that
repo's `sign.yml` and Milestone #18 Dockerfile for the signing logic and
narrow-vendoring discipline this service builds on.

## Status: Phase 2 — real signing, proven live in production

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

**Adopted in production, 2026-09-02**: `lucid-console` and
`lucid-dsse-collector` both cut over their `attest` job from
`lucid-attest`'s `sign.yml` to `sign-client.yml` the same day, replacing
`lucid-attest`'s pinned Docker signer container entirely for those two
repos. Both real cutovers, not just `test-sign-client.yml`'s synthetic
self-test, immediately surfaced two real bugs `sign-client.yml`'s own
self-test couldn't have caught (see "Adopting this service" below for
what they were and the minimum safe pin) — both fixed same day.

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

## Adopting this service: `sign-client.yml`

Callers should adopt this service via `.github/workflows/sign-client.yml`
(a reusable `workflow_call` workflow), **not** by inlining a token-mint +
`curl POST /v1/sign` step directly into their own workflow file. This
isn't a style preference — it's load-bearing: GitHub's OIDC
`job_workflow_ref` claim (and hence the Fulcio certificate identity
Sigstore issues) reflects *this file's own path* for a job that mints
its own token inside a reusable-workflow invocation, regardless of which
repo's `uses:` line called it. That's what lets a single,
individually-reviewed entry in `lucid-assay`'s `cli/verify.py`
(`TRUSTED_CONTROL_PLANE_BUILDER_IDS`) trust every caller of this file —
inlining the same logic into each caller's own workflow instead would
make every caller's own file its own unreviewed identity, which
`cli/verify.py` has no sound way to trust at scale. See the Lucid
vault's "Serverless signer needs a trustworthy provenance builder
identity" note for the full reasoning.

`sign-client.yml` is structurally the same role `lucid-attest`'s own
`sign.yml` plays today: given `subject-name`/`subject-digest`, it
constructs real SLSA v1.0 provenance (Build Level 3) from its own
trusted context (`cli.provenance`, checked out from a pinned
`lucid-assay` SHA — pure stdlib, no dependency install needed) before
signing both the caller's statement(s) and the provenance atomically —
same input/output contract as `sign.yml` (`artifact-name`,
`statement-files`, optional `subject-name`/`subject-digest`, outputs
`artifact-name: signed-statements`), so adopting it from an existing
`sign.yml` caller is a `uses:` swap, not a rewrite:

```yaml
jobs:
  attest:
    needs: build
    permissions:
      id-token: write
      contents: read
    # This line is the SOLE source of truth for the pinned signer commit --
    # see the verify-job snippet below for why nothing else should
    # duplicate it as a separately hand-kept-in-sync literal.
    uses: lucid-provenance/lucid-attest-service/.github/workflows/sign-client.yml@<pinned-sha>
    with:
      artifact-name: unsigned-statements
      statement-files: |
        my-repo.unsigned.json
      subject-name: ${{ needs.build.outputs.image-ref }}
      subject-digest: ${{ needs.build.outputs.image-digest }}
```

**Deriving `--cert-identity` for your own `verify` job: don't duplicate
the pin.** GitHub Actions won't accept an expression in a reusable-
workflow `uses:` line, which makes it tempting to also keep the SHA in a
separate `env:` var for `--cert-identity` to read — don't; a Dependabot
bump PR (see below) only ever touches the `uses:` line itself, and a
separately-maintained copy will silently go stale the moment one lands,
turning into a confusing identity-verification failure instead of a
clean bump. Parse the `attest` job's own `uses:` line at runtime instead
— `lucid-assay`, `lucid-console`, and `lucid-dsse-collector`'s own
`assay.yml` files all do exactly this (a `Derive expected signer
identity from the attest job's own uses: pin` step early in `verify`,
scoped to their own well-known job shape) — copy that pattern rather
than reinventing it.

**Tagged releases: `v1.0.0` baseline as of 2026-09-02.** Pin to an exact
commit SHA (not `@main` — see the note above on why that would defeat
the trust model). This repo now has a real tag for Dependabot's
`github-actions` ecosystem to bump toward — see `.github/dependabot.yml`
— but a bump PR still needs a human to merge it; nothing here auto-applies.

**Minimum safe pin, as of 2026-09-02: `231039b08b91b78788dba732a10355aafcaeaa11`.**
Anything before that has one of two real bugs found by this service's
first genuine external callers, neither of which `test-sign-client.yml`'s
synthetic self-test could catch (caller and callee are the same repo
there):

- **PR #8** — the composite action step referenced
  `mint-sigstore-oidc-token` via a local `./` path, which resolves
  against a checkout of `${{ github.repository }}` — inside a
  `workflow_call` job, that reflects the *caller's* repo, not this one.
  Broke immediately on `lucid-console`'s first real run. Fixed by
  referencing the composite action remotely
  (`owner/repo/path@sha`) instead.
- **PR #9** — the signing step passed the whole batch payload to
  `curl -d "$batch"`, a literal shell argument. A real statement (e.g. a
  lockfile-derived `resolved_dependencies` list plus its provenance
  sibling) can exceed the kernel's `ARG_MAX` this way — "Argument list
  too long", exit 126. Fixed by writing the batch to a file and posting
  it via `curl --data-binary @file` instead.

`.github/workflows/test-sign-client.yml` (`workflow_dispatch`-only, same
real-Rekor-entry caution as the smoke test) exercises the whole chain
end to end — a fake `build` job with no `id-token: write` at all, a real
`sign-client.yml` call with provenance construction opted in, and a
`verify` job confirming both resulting envelopes carry genuine
signatures/Rekor entries and that the provenance statement's
`runDetails.builder.id` matches exactly what `cli/verify.py` needs to
trust.

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

## Smoke testing against the real deployed endpoint

`.github/workflows/smoke-test-sign.yml` (manual `workflow_dispatch`
only, never on push/PR — every successful run mints a real, permanent,
public Rekor transparency-log entry) mints its own ambient GitHub
Actions OIDC token, forwards it to the live `POST /v1/sign` endpoint the
same way any real caller would, and verifies the response is a genuine
signed envelope (a real Sigstore signature and a non-null Rekor log
index/URL, not just an HTTP 200). This is also the platform's first real
"thin client snippet" — the pattern any future GitHub Actions caller
adopting this service would copy.

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
