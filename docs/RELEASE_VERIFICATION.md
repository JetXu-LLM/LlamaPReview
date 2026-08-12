# Release and artifact verification

Each semantic release binds exactly three deployable ZIPs to one public repository, tag, and commit:

- `LlamaPReviewWebhookHandler.zip`;
- `LlamaPReviewPipeline.zip`;
- `LlamaPReviewPipelineDependencies.zip`.

The release also includes `SHA256SUMS`, `release-manifest.json`, a CycloneDX `sbom.cdx.json`, and `dependency-licenses.json`. GitHub Actions builds every asset twice, rejects nondeterministic bytes, scans the source history and unpacked artifacts for credential-shaped material, and publishes build-provenance attestations. The workflow has no official AWS production access.

## Verify an official release

Choose an exact semantic tag and obtain its commit before trusting any asset:

```bash
tag=v0.1.0
repo=JetXu-LLM/LlamaPReview
commit="$(gh api "repos/${repo}/commits/${tag}" --jq .sha)"
test "${#commit}" -eq 40
```

This works for both annotated and lightweight tags. Do not treat an annotated
tag-object SHA as the source commit; `v0.1.0` is annotated and resolves to its
referenced commit through the command above.

Download the exact release into a new directory:

```bash
release_dir="$(mktemp -d)"
gh release download "${tag}" --repo "${repo}" --dir "${release_dir}"
```

Verify every published checksum:

```bash
cd "${release_dir}"
sha256sum -c SHA256SUMS       # Linux
# shasum -a 256 -c SHA256SUMS # macOS
```

Verify GitHub provenance for every asset, constraining the repository, source commit, tag ref, and signer workflow:

```bash
for asset in ./*; do
  gh attestation verify "${asset}" \
    --repo "${repo}" \
    --source-digest "${commit}" \
    --source-ref "refs/tags/${tag}" \
    --signer-workflow "JetXu-LLM/LlamaPReview/.github/workflows/release.yml" \
    --deny-self-hosted-runners
done
```

Inspect `release-manifest.json` and require:

- `source.repository` is exactly `JetXu-LLM/LlamaPReview`;
- `source.commit` is the resolved 40-character commit;
- `source.tag` is the selected tag;
- the artifact set contains only the two functions and one Layer above;
- every recorded digest and size agrees with the downloaded files.

For the executable source-level verifier, check out the same tag and run:

```bash
python scripts/verify_release_artifacts.py "${release_dir}" \
  --expected-repository "${repo}" \
  --expected-commit "${commit}" \
  --expected-tag "${tag}"
python scripts/scan_secrets.py --artifacts "${release_dir}"
```

The verifier checks ZIP paths, modes, timestamps, exact function source bytes, Layer identity and size, retained license material, SBOM reproducibility, and the complete asset allowlist. Any mismatch is a stop condition: do not deploy a partially verified or locally rebuilt substitute.

## Build the same release locally

Release builders need the exact hash-bound `llama-github` wheel named in `lambda_functions.json`:

```bash
python -m pip download --no-deps --only-binary=:all: \
  llama-github==0.4.5 --dest /secure/wheels

make release \
  SDK_WHEEL=/secure/wheels/llama_github-0.4.5-py3-none-any.whl \
  RELEASE_DIR=dist/release \
  RELEASE_COMMIT="$(git rev-parse HEAD)" \
  RELEASE_TAG="$(git describe --tags --exact-match)"
```

Local reproducibility is useful evidence, but it does not replace verification of the GitHub-published provenance used for an official deployment.
