# Deterministic replay corpus

This directory is the compact deterministic replay ledger for the production
Deep -> Final -> Projection path. It contains no credentials, provider response
bodies, private reasoning or production writes.

`manifest.json` lists exact executable test IDs. The loader rejects duplicate,
missing, or unresolved cases, and each receipt includes the manifest SHA-256.
The suites cover:

- supported blocker and merge-posture preservation;
- local degradation of invalid inline, suggestion, evidence and Mermaid
  surfaces without losing the main review;
- exact-head and CI-refresh survival;
- clear/negative controls, sanitation and public payload safety;
- exactly-once publication and accounting boundaries exercised by the unit
  suites that consume the same production capabilities.

Run the current and sealed regression cases:

```bash
python scripts/run_replay_corpus.py
```

Local replay proves deterministic parser, Projection, rendering and safety
behavior. It performs no network, provider, GitHub, or AWS product write and
does not claim to prove stochastic provider judgment quality.

The release value function accepts normal expert-model variance. A replay or
provider run blocks release only for a concrete hard system counterexample:
wrong/stale head, unrecoverable lifecycle, duplicate or missing publication,
false accounting, unsafe/malformed public payload, unauthorized external
mutation, or whole-review loss caused by a locally degradable surface.
