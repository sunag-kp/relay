# Semantic cache threshold: `fastembed:BAAI/bge-small-en-v1.5`

Run in GitHub Actions (the model downloads from Hugging Face, which the development sandbox
cannot reach): workflow `Semantic cache eval`, run
[39851913432](https://github.com/sunag-kp/relay/actions/runs/39851913432), commit `e35d4ef`.
Copied verbatim from the job log.

55 labelled pairs (30 same-intent, 25 different-intent). Command: `python -m benchmarks.semantic_threshold --embedder fastembed`

Highest similarity of any different-intent pair: **0.995** (**0.995** among pairs the number guard lets through).

Sweep with the number guard (production behaviour):

| threshold | false positives | precision | recall | formatting | paraphrase | small-edit |
|---|---|---|---|---|---|---|
| 0.60 | 13 | 0.683 | 0.933 | 1.0 | 0.8 | 1.0 |
| 0.65 | 13 | 0.683 | 0.933 | 1.0 | 0.8 | 1.0 |
| 0.70 | 13 | 0.675 | 0.9 | 1.0 | 0.7 | 1.0 |
| 0.75 | 13 | 0.667 | 0.867 | 1.0 | 0.6 | 1.0 |
| 0.80 | 12 | 0.676 | 0.833 | 1.0 | 0.5 | 1.0 |
| 0.85 | 9 | 0.735 | 0.833 | 1.0 | 0.5 | 1.0 |
| 0.90 | 4 | 0.84 | 0.7 | 1.0 | 0.1 | 1.0 |
| 0.95 | 2 | 0.9 | 0.6 | 1.0 | 0.0 | 0.8 |

False positives at 0.85 / 0.90 / 0.95 *without* the number guard: 11 / 4 / 2

**No threshold is free of false positives on this set.**

## Reading

A small dense model is what makes paraphrases hit (80% paraphrase recall at 0.60), but it
also scores "List three benefits of unit testing" vs "...drawbacks...", "boiling" vs
"freezing point", "capital of France" vs "capital of Germany" at 0.9+. Where its false
positives become rare (>= 0.95) it no longer matches paraphrases at all, so it buys nothing
over the lexical embedder there. That is why the gateway ships `EMBEDDING_PROVIDER=hashing`
(zero false positives at its 0.88 default on the same set) and treats dense embedders as an
opt-in for workloads that tolerate an occasional wrong cached answer (FAQ-style traffic).
