# lucid-attest-service

Serverless signing service (Lucid roadmap #12) — a versioned
`POST /v1/sign` endpoint replacing the SHA-pinned, per-caller signer
workflow every tenant CI platform runs today. Evolves out of
[lucid-attest](https://github.com/lucid-provenance/lucid-attest); see that
repo's `sign.yml` and Milestone #18 Dockerfile for the signing logic and
narrow-vendoring discipline this service builds on.

## Status: Phase 2 — real signing, proven live in production

`POST /v1/sign` calls straight into `cli.oidc_signer.sign_statement`,
vendored from a pinned `lucid-assay` source SHA — the same narrow-file
checkout pattern `lucid-attest`'s own Milestone #18
`build-signer-image.yml` uses, verified empirically to be an even
narrower list than that image's (`cli/__init__.py`, `cli/common.py`,
`cli/oidc_signer.py` only — this service calls `sign_statement()` on
bytes it already has in memory, never the file-based
`sign_file_to_envelope()` wrapper or the CLI subcommands built on it, so
`cli/sign.py`, `cli/provenance.py`, `cli/slsa_provenance.py`, and
`cli/parsers/lockfiles.py` aren't needed here). Never returns a
fabricated placeholder on a failure path — see `src/app.py`'s module
docstring.

**Vendoring and building both happen exactly once now, in `build`, not
in `deploy` (fixed 2026-09-14).** The deploy-consolidation pass that
folded `deploy.yml` into `assay.yml`'s `deploy` job (same day) carried
`deploy.yml`'s own independent `sam build --use-container` — and its own
separate signer pin, `SIGNER_SOURCE_SHA` — over unchanged, without
noticing that meant the attested subject-digest could never provably
describe what actually got deployed (`build` and `deploy` were two
independent builds, from two different pins, of what should have been
one artifact). `SIGNER_SOURCE_SHA` no longer exists: `build` job vendors
`cli.oidc_signer` from its own `ASSAY_CLI_SHA` and builds the real SAM
package once; `deploy` job downloads that exact `.aws-sam/build/`
output, independently re-derives its digest, and asserts it matches
what got attested before ever calling `sam deploy` — never rebuilding.
See assay.yml's `build`/`deploy` jobs' own comments for the mechanism,
which mirrors how `lucid-dsse-collector`'s own `deploy` job re-resolves
and deploys its image by digest rather than trusting a propagated value.

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
`artifact-name: signed-statements` by default; an optional
`signed-artifact-name` input renames it, required when one run calls this
workflow more than once, since a run can't hold two artifacts of one name), so adopting it from an existing
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

**Signing more than once in one run (2026-09-23).** A run can call this workflow
twice -- `lucid-dsse-collector` signs its build-time statements after `build`, and
its post-deploy deployment-verification statement after `post-deploy-tests` -- but a
run cannot hold two artifacts of the same name, and the default output artifact is
`signed-statements`. Give the second call its own `signed-artifact-name`, and give
its job a name your `verify` job's `uses:` parse won't match (the collector's is
`attest-cd`, so its awk on the job literally named `attest:` still finds the
build-time signer). Both calls sign as the same reviewed identity, so a verifier
that trusts this workflow trusts both:

```yaml
  attest-cd:
    needs: [deploy, post-deploy-tests]
    permissions:
      id-token: write
      contents: read
    uses: lucid-provenance/lucid-attest-service/.github/workflows/sign-client.yml@<pinned-sha>
    with:
      artifact-name: unsigned-deployment-verification
      statement-files: |
        deployment-verification.unsigned.json
      signed-artifact-name: signed-deployment-verification
```

The input first exists at PR #43's merge (`d487772`), so a caller passing it must pin at
or after that commit -- an older pin rejects the unknown input and the job never starts.
Omit `subject-name`/`subject-digest` when there is no build artifact to attach SLSA
provenance to (a deployment event has none).

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

GitHub Actions (`.github/workflows/assay.yml`'s `deploy` job — folded
in from a wholly separate `deploy.yml` on 2026-09-14, closing the same
build/deploy race `lucid-dsse-collector` had already found and fixed:
`deploy` now runs only after `build`/`attest`/`verify` all pass on the
exact same commit, via `needs:`, instead of an independently
`push`-triggered workflow with no ordering guarantee against this
one) deploys on every push to `main`, via OIDC — no local `sam deploy`
credentials needed for normal use.

**`deploy` never builds anything (fixed 2026-09-14, same day, a second
pass).** The consolidation above carried `deploy.yml`'s own independent
`sam build --use-container` over unchanged — meaning even after fixing
the ordering race, the package actually deployed was never provably the
same bytes `build` had already hashed and attested; it was a second,
separately-vendored build. Real fix: `build` job (which vendors
`cli/oidc_signer.py` from its own `ASSAY_CLI_SHA` and runs `sam build`
once) uploads the resulting `.aws-sam/build/` directory as a workflow
artifact; `deploy` downloads that exact artifact, independently
re-derives its digest from the downloaded bytes, and asserts it matches
`needs.build.outputs.subject-digest` before ever calling `sam deploy` —
failing closed on any mismatch rather than deploying an unverified
package. No signer checkout, no vendoring, no build step in `deploy` at
all any more.

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

Vendor the signer source by hand first (mirrors what `assay.yml`'s
`deploy` job does at deploy time — see its own comments for exactly
why this is a build-time step, not a committed copy):

```bash
mkdir -p src/cli
cp /path/to/lucid-assay/cli/__init__.py    src/cli/__init__.py
cp /path/to/lucid-assay/cli/common.py      src/cli/common.py
cp /path/to/lucid-assay/cli/oidc_signer.py src/cli/oidc_signer.py
```

Then `sam build --use-container && sam local invoke SignFunction`
(`--use-container`: see `assay.yml`'s `deploy` job's own comment on why
a plain local build isn't used here). A real Sigstore round-trip additionally needs a
valid caller-supplied identity token in the invoke event's
`Authorization` header — see `events/` (if present) or construct one by
hand against the `handler(event, context)` shape in `src/app.py`.

## Security

Vulnerability reports: see [`SECURITY.md`](https://github.com/lucid-provenance/.github/blob/main/SECURITY.md)
(org-wide default — GitHub Private Vulnerability Reporting is enabled on
this repo). The fuller vulnerability-management and secure-SDLC policy
governing how findings get triaged, fixed, or formally risk-accepted
lives in [`lucid-provenance/compliance`](https://github.com/lucid-provenance/compliance)
(private).

### Independent architectural review, 2026-09-14

An independent review (Gemini) raised four findings. Each was checked
against the real code/config before being accepted or rejected — see
lucid-assay's own CLAUDE.md "Independent code review" precedent for why
that verification step matters and isn't skipped.

- **API Gateway had no authorizer — confirmed and fixed.** `template.yaml`
  had no `Auth:` block at all: every request, including anonymous/junk
  ones, reached `SignFunction` and paid for a full Lambda invocation
  before `app.py`'s own header checks ever ran — a real billing/compute-
  exhaustion surface. Fixed via a native HTTP API JWT authorizer
  (`Globals.HttpApi.Auth`) scoped to GitHub Actions' own OIDC issuer and
  the exact `audience: sigstore` every real caller (`sign-client.yml`,
  `smoke-test-sign.yml`) already mints its token for — rejects a request
  that isn't even a well-formed, correctly-issued token at the API
  Gateway edge, before Lambda runs. **Deliberately does not and cannot**
  restrict *which* repo/workflow may call this endpoint — every
  legitimate caller mints its own token under its own identity, which is
  the whole point of a shared signing endpoint; that's a distinct
  problem (see the next item) a token-shaped-and-audience check can't
  solve. Not yet exercised against a real deploy + a real smoke-test run
  as of this writing — the next `smoke-test-sign.yml` run after this
  merges is the actual confirmation, not this description.
- **The signer blindly signs whatever statement it's handed — confirmed,
  not new, not fixed here.** Verified directly against
  `cli.oidc_signer.sign_statement`: it performs zero re-validation of a
  statement's *content* (RCS score, subject digest, test results, ...)
  before signing it — a compromised caller can request a signature over
  fabricated claims and get a validly-signed DSSE envelope back. Real,
  but this is the same, already-tracked platform-wide gap as lucid-assay
  itself shipping a fabricated subject digest in its own dogfood run
  (Bill's own priority-1 item going into this session) — the isolated
  signer was only ever designed to bind a trusted *identity* to the
  signature (which it does: see `TRUSTED_CONTROL_PLANE_BUILDER_IDS` and
  SLSA Build L3's control-plane-builder-identity/isolated-provenance-
  generation checks), never to independently re-derive or verify the
  claims it's asked to sign — that would require the signer to re-run
  scoring/verification logic server-side, a materially larger design
  change than anything else in this pass, and isn't attempted here.
- **Missing OIDC token validation — checked, rejected.** The claim that
  "no length limit or structural validation" occurs before a token
  reaches the signing context doesn't hold up against the actual
  installed library: `sigstore.oidc.IdentityToken.__init__` already
  calls `jwt.decode()` with `required=["aud","sub","iat","exp","iss"]`
  and raises a clean `IdentityError` on anything malformed, oversized,
  or garbage — caught by `oidc_signer.py`'s existing exception handling
  and turned into a normal 502, not a crash or unbounded resource
  consumption. No injection vector exists either (a JWT decode, not
  eval/exec). Not fixed, because there was nothing to fix as described.
- **SHA-pinned vendoring requires a manual bump — confirmed, not a new
  problem, "durable fix" not applied.** `SIGNER_SOURCE_SHA`/
  `ASSAY_CLI_SHA` are env-var pins, not `uses:` refs, so Dependabot's
  `github-actions` ecosystem can't auto-bump either one — true, and
  already this repo's real, lived experience (both pins have needed
  manual re-pinning multiple times). (`SIGNER_SOURCE_SHA` specifically no
  longer exists in *this* workflow as of the same day's later fix above —
  `deploy` no longer vendors anything itself — and `ci.yml` itself was
  folded into `assay.yml` and deleted the same day, taking its own copy
  of the same pin with it. `sign-client.yml` still carries its own
  separate, same-shaped pin, so this bullet's substance is unchanged.)
  The suggested fix (a real package
  dependency pulled from a registry or a git URL) doesn't obviously
  solve the automation gap either — Dependabot doesn't reliably auto-bump
  a git-commit-pinned Python dependency any better than an env-var SHA —
  and would pull a larger, harder-to-audit surface directly into a
  Lambda that holds real signing privilege, the opposite of this
  service's own narrow-vendoring rationale (see Status above). Left as
  the same known, deliberate tradeoff it already was.
