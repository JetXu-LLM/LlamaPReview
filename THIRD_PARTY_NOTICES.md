# Third-Party Notices

LlamaPReview source is licensed under Apache-2.0. Third-party components keep
their own licenses; they are not relicensed under the project license.

The deterministic Pipeline Layer build uses the exact versions below. The
inventory comes from `lambda_functions.json`,
`lambdas/LlamaPReviewPipeline/requirements-layer.lock`, and the license metadata
inside the corresponding wheels.

| Component | Version | License |
| --- | ---: | --- |
| llama-github | 0.4.5 | Apache-2.0 |
| certifi | 2026.6.17 | MPL-2.0 |
| cffi | 2.1.0 | MIT-0 |
| charset-normalizer | 3.4.9 | MIT |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| PyGithub | 2.9.1 | LGPL-3.0-or-later |
| PyJWT | 2.13.0 | MIT |
| PyNaCl | 1.6.2 | Apache-2.0; its bundled libsodium is ISC |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause |
| requests | 2.33.0 | Apache-2.0 |
| six | 1.17.0 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |

The Layer build preserves each installed distribution's license files. In
particular, the PyGithub wheel includes the GNU GPLv3 and LGPLv3 texts, PyNaCl
includes the libsodium license, and Requests includes the attribution reproduced
in `NOTICE`.

## Runtime-provided AWS SDK

The Lambda functions import `boto3` and `botocore`. The official deployment uses
the AWS Lambda Python runtime copies rather than bundling those packages in the
Function ZIPs. The repository declares `boto3>=1.34,<2.0` and
`botocore>=1.34,<2.0`; both projects are Apache-2.0 licensed. A self-hosted build
that bundles a different SDK closure must preserve that closure's notices and
license files.

## Development-only tools

`pip-audit==2.10.0` (Apache-2.0) and `ruff==0.14.14` (MIT) are pinned for CI.
They are not included in the Lambda release artifacts.

When a runtime lock, SDK wheel, or release-artifact composition changes, update
this inventory in the same pull request. The generated release SBOM is the
authoritative per-artifact inventory.
