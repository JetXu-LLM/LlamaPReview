# Project scope

LlamaPReview is an evidence-first pull request reviewer with deterministic safety and publication boundaries.

In scope:

- one signed public-repository Webhook path;
- exact-head repository retrieval and PFR;
- Deep engineering judgment and Final presentation;
- deterministic review Projection, Mermaid, and inline placement;
- direct DeepSeek transport with complete accounting;
- durable AWS recovery and exactly-once GitHub publication;
- a generic AWS self-hosting reference and reproducible release artifacts.

Not part of the current product:

- a dashboard or trace website;
- a plugin, policy, or rule marketplace;
- repository-specific keyword review rules;
- additional model providers or a generic provider framework;
- automatic public-CI deployment to official AWS production;
- legacy handlers, shadow/canary routing, private replay orchestration, or production-observation tooling.

Roadmap proposals should start with a concrete user or operator failure and an executable acceptance test. A new permanent mechanism is justified only when current required behavior would fail without it.
