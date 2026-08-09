# Governance

LlamaPReview is a maintainer-led open-source project. Jet Xu
([@JetXu-LLM](https://github.com/JetXu-LLM)) is the initial maintainer and final
decision owner for project scope, merges, releases, security response, and the
official hosted service.

## How decisions are made

Changes are discussed in Issues, Discussions, and pull requests. Maintainers
evaluate them against the project's current scope and the practical balance of:

- maintainer decision value and user trust;
- visible, reproducible open-source evidence;
- contributor clarity;
- review noise and architecture complexity;
- privacy and operational surprise.

Passing checks or receiving an automated review does not by itself authorize a
merge. Maintainers remain responsible for tradeoffs and acceptance.

## Releases and official operations

Public CI may test source and build verifiable release artifacts. It does not
deploy the official AWS service. Production credentials, state, rollback data,
and deployment authority remain in a private least-privilege operator boundary.

Product releases use semantic version tags. Numeric AWS Lambda versions are
deployment identities, not project release versions.

## Maintainer growth

Additional maintainers may be invited after sustained, trustworthy
contributions and demonstrated care for review quality, privacy, and operational
safety. The project does not require a committee or voting process to accept
ordinary contributions.
