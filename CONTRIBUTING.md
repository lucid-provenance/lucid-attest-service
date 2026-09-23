# Contributing

How to run and test `lucid-attest-service`, and what is different about changing
a reusable workflow that other repositories depend on. Read
[`ARCHITECTURE.md`](ARCHITECTURE.md) first.

## Ground rules

- **Work on a branch; never commit to `main`.** Every merge to `main` runs the
  real pipeline, which deploys the Lambda.
- **A change to `sign-client.yml` is a change to every caller.** It is consumed by
  commit SHA, so nothing reaches a caller until that caller repins. Treat the
  inputs and outputs as a public contract: add optional inputs with a default that
  preserves current behaviour, and don't rename or remove one without a migration.
- **Never invent signing output.** No placeholder envelope on any failure path.
- **Pinned actions.** Every third-party `uses:` is a full commit SHA with a
  version comment. Dependabot proposes weekly bumps and a human merges them.
- There is no configured linter or type-checker. Don't add one as a side effect.

## Setup

Python 3.13+ (the Lambda runs `python3.13`).

```bash
uv sync --frozen --extra dev
uv run --no-sync python3 -m pytest -v tests/
```

`pyproject.toml` exists so CI has a real uv-managed environment to test and
mutation-test against. It is **not** what ships: the deployed dependencies come
from the hash-pinned `src/requirements.txt`. Don't treat one as a replacement for
the other, and keep `uv.lock` in sync with `pyproject.toml` (regenerate with
`uv lock`, commit both).

CI runs the suite with coverage:

```bash
uv run --no-sync --no-build python3 -m pytest -v \
  --junitxml=build/junit.xml --cov=src --cov-report=xml:build/coverage.xml tests/
```

## Test suite taxonomy

| | `pytest tests/` | `pytest tests/live/` |
|---|---|---|
| **Question** | Does `handler()` do the right thing? | Does the *deployed* endpoint do the right thing? |
| **Talks to** | The handler in-process with a hand-built `event` dict. No network. | The real API Gateway and Lambda over HTTPS. |
| **Catches** | Request validation, header parsing, batch semantics, the error contract. | Deployment drift: a broken IAM permission, a stale package, a misconfigured authorizer. |
| **Runs** | On every PR, in `build`. | Only after a real deploy, in `post-deploy-tests` (never on a `pull_request`). |

`tests/live/` is excluded from a bare `pytest tests/` by `norecursedirs` in
`pyproject.toml`.

### The live suite

It runs as staged steps in ascending cost, and a job stops at its first failing
step:

1. `test_negative_security.py`: no identity, rejected at the API Gateway
   authorizer. Zero Lambda invocations.
2. `test_request_validation.py`: needs a real token to clear the authorizer, but
   `app.py` rejects the request before any signing. Zero Sigstore cost.
3. `test_critical_path.py`: the first stage that actually signs, and mints one
   real, permanent Rekor entry.
4. `test_batch_stress.py` (`-m stress`): a multi-statement batch, several real
   Rekor entries. The priciest stage, run last.

Configuration (environment variables):

| Variable | Meaning |
|---|---|
| `LIVE_SIGN_ENDPOINT` | Defaults to this stack's known endpoint. |
| `LIVE_OIDC_TOKEN` | A real, already-minted GitHub Actions OIDC token with audience `sigstore`. Optional. Tests that need one **skip** when it's unset. |

You cannot mint that token from an arbitrary shell; only a real
`id-token: write` GitHub Actions job has one (the composite action in
`.github/actions/mint-sigstore-oidc-token/` mints it in CI). So the token-gated
stages are effectively a CI-only run, and locally you will normally only exercise
the negative-security stage. Don't point the live suite at anything you don't
mean to write permanent transparency-log entries to.

## Changing `sign-client.yml`

1. Make the change on a branch and keep it backwards compatible (optional input,
   unchanged default).
2. Run `test-sign-client.yml` (manual, `workflow_dispatch`). It is the only test of
   the reusable-workflow path itself: a fake `build` job with no `id-token`, a real
   `sign-client.yml` call, and a `verify` job checking the envelopes carry genuine
   signatures and Rekor entries. It mints real, permanent Rekor entries, so run it
   deliberately, not on every push.
   - Note its limit: caller and callee are the same repository there, so it cannot
     catch bugs that only appear for an *external* caller. Two real bugs (a local
     `./` action path resolving against the caller's repo, and a payload exceeding
     `ARG_MAX` when passed as `curl -d`) were only found by the first external
     callers.
3. After it merges, callers adopt it by repinning to the merge commit. To share
   the same artifact name across two calls in one run, a caller must pass a
   distinct `signed-artifact-name`.
4. Remember a caller's `verify` job derives its expected signer identity by
   parsing its `attest` job's `uses:` line. Anything that changes the workflow's
   path or name changes that identity everywhere.

## Changing the Lambda

- `src/app.py` keeps the request contract: a bare JSON array in, `{"envelopes":
  [...]}` out in the same order, `400` for a malformed body, `401` for a
  missing or malformed `Authorization` header, `502` for any signing failure.
- The deployed package is built once in `build` and only ever *deployed* in
  `deploy`. If you touch how it is vendored or built, keep the "re-derive the
  digest and assert it matches" step intact.
- Mutation testing runs against your diff. Some survivors are genuine
  equivalent mutants (documented in `app.py`'s module docstring, e.g. a
  case-insensitive codec name). Document new ones there rather than chasing them.

## Functional verification

This repository does not declare a `.lucid/functional-verification.json`
contract, and its tests aren't tagged with `@pytest.mark.cuj`. The functional
contract mechanism belongs to `lucid-assay`, and callers opt in by adding a
contract and tagging tests. If you want that here, add both together and it is
scored per tier: `ci` journeys by `tests/`, `cd` journeys by `tests/live/`.
