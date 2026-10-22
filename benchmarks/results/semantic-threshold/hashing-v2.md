# Semantic cache threshold: `hashing-v2`

55 labelled pairs (30 same-intent, 25 different-intent). Command: `python -m benchmarks.semantic_threshold --embedder hashing`

Highest similarity of any different-intent pair: **0.8307** (**0.8307** among pairs the number guard lets through).

Sweep with the number guard (production behaviour):

| threshold | false positives | precision | recall | formatting | paraphrase | small-edit |
|---|---|---|---|---|---|---|
| 0.60 | 7 | 0.731 | 0.633 | 1.0 | 0.0 | 0.9 |
| 0.65 | 5 | 0.792 | 0.633 | 1.0 | 0.0 | 0.9 |
| 0.70 | 5 | 0.792 | 0.633 | 1.0 | 0.0 | 0.9 |
| 0.75 | 2 | 0.905 | 0.633 | 1.0 | 0.0 | 0.9 |
| 0.80 | 1 | 0.95 | 0.633 | 1.0 | 0.0 | 0.9 |
| 0.84 | 0 | 1.0 | 0.6 | 1.0 | 0.0 | 0.8 |
| 0.85 | 0 | 1.0 | 0.567 | 1.0 | 0.0 | 0.7 |
| 0.90 | 0 | 1.0 | 0.5 | 0.9 | 0.0 | 0.6 |
| 0.95 | 0 | 1.0 | 0.467 | 0.9 | 0.0 | 0.5 |

False positives at 0.85 / 0.90 / 0.95 *without* the number guard: 0 / 0 / 0

Lowest threshold with zero false positives (and so the best safe recall): **0.84**. Ship a margin above it: a wrong cached answer is worse than a miss.
