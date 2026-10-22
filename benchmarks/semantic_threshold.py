"""Calibrate the semantic-cache similarity threshold on labelled prompt pairs.

A semantic hit on a pair that means something different returns a *wrong answer*, so the
threshold is chosen for zero false positives first and recall second.

    uv run python -m benchmarks.semantic_threshold --embedder hashing
    uv run --extra local-embeddings python -m benchmarks.semantic_threshold --embedder fastembed
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

from relay.domain.cache import number_signature, semantic_text
from relay.domain.chat import ChatCompletionRequest
from relay.infra.embeddings import FastEmbedEmbedder, HashingEmbedder, OpenAIEmbedder, cosine

DATA = Path(__file__).parent / "data" / "semantic_pairs.jsonl"
OUT = Path(__file__).parent / "results" / "semantic-threshold"


def as_cache_text(prompt: str) -> str:
    """Embed exactly what the gateway embeds for a single-turn request."""
    request = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": prompt}]}
    )
    text = semantic_text(request)
    assert text is not None
    return text


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedder", choices=["hashing", "fastembed", "openai"], default="hashing")
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    args = parser.parse_args()

    if args.embedder == "fastembed":
        embedder = FastEmbedEmbedder(args.model)
    elif args.embedder == "openai":
        embedder = OpenAIEmbedder()
    else:
        embedder = HashingEmbedder()

    pairs = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
    scored = []
    for pair in pairs:
        text_a, text_b = as_cache_text(pair["a"]), as_cache_text(pair["b"])
        a = await embedder.embed(text_a)
        b = await embedder.embed(text_b)
        scored.append(
            {
                **pair,
                "similarity": round(cosine(a, b), 4),
                # In production, prompts with different numbers live in different
                # namespaces and are never compared.
                "comparable": number_signature(text_a) == number_signature(text_b),
            }
        )

    raw_rows = sweep(scored, guarded=False)
    rows = sweep(scored, guarded=True)
    safe = [r for r in rows if r["false_positives"] == 0]
    best = max(safe, key=lambda r: (r["recall"], -r["threshold"])) if safe else None
    comparable_negatives = [s["similarity"] for s in scored if not s["same"] and s["comparable"]]
    max_negative = max(comparable_negatives) if comparable_negatives else 0.0
    raw_max_negative = max(s["similarity"] for s in scored if not s["same"])

    OUT.mkdir(parents=True, exist_ok=True)
    name = embedder.id.replace("/", "_").replace(":", "_")
    report = {
        "embedder": embedder.id,
        "pairs": scored,
        "sweep": rows,
        "sweep_without_number_guard": raw_rows,
        "chosen": best,
    }
    (OUT / f"{name}.json").write_text(json.dumps(report, indent=2))
    lines = [
        f"# Semantic cache threshold: `{embedder.id}`",
        "",
        f"{len(scored)} labelled pairs ({sum(s['same'] for s in scored)} same-intent, "
        f"{sum(not s['same'] for s in scored)} different-intent). Command: "
        f"`python -m benchmarks.semantic_threshold --embedder {args.embedder}`",
        "",
        f"Highest similarity of any different-intent pair: **{raw_max_negative}** "
        f"(**{max_negative}** among pairs the number guard lets through).",
        "",
        "Sweep with the number guard (production behaviour):",
        "",
        "| threshold | false positives | precision | recall | "
        + " | ".join(k.removeprefix("recall_") for k in rows[0] if k.startswith("recall_"))
        + " |",
        "|---|---|---|---|" + "---|" * sum(k.startswith("recall_") for k in rows[0]),
    ]
    shown = rows[::5] + ([best] if best and best not in rows[::5] else [])
    lines.extend(
        f"| {r['threshold']:.2f} | {r['false_positives']} | {r['precision']} | {r['recall']} | "
        + " | ".join(str(v) for k, v in r.items() if k.startswith("recall_"))
        + " |"
        for r in sorted(shown, key=lambda r: r["threshold"])
    )
    unguarded_fp = [r["false_positives"] for r in raw_rows if r["threshold"] in (0.85, 0.9, 0.95)]
    lines += [
        "",
        "False positives at 0.85 / 0.90 / 0.95 *without* the number guard: "
        + " / ".join(str(fp) for fp in unguarded_fp),
    ]
    if best is None:
        lines += ["", "**No threshold is free of false positives on this set.**"]
    else:
        lines += [
            "",
            f"Lowest threshold with zero false positives (and so the best safe recall): "
            f"**{best['threshold']:.2f}**. Ship a margin above it: a wrong cached answer is "
            "worse than a miss.",
        ]
    (OUT / f"{name}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def sweep(scored: list[dict[str, object]], *, guarded: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    positives = sum(1 for s in scored if s["same"])
    for step in range(60, 100):
        threshold = step / 100

        def hit(s: dict[str, object], threshold: float = threshold) -> bool:
            return float(s["similarity"]) >= threshold and (s["comparable"] or not guarded)  # type: ignore[arg-type]

        tp = sum(1 for s in scored if s["same"] and hit(s))
        fp = sum(1 for s in scored if not s["same"] and hit(s))
        recall_by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for s in scored:
            if s["same"]:
                recall_by_kind[str(s["kind"])][1] += 1
                recall_by_kind[str(s["kind"])][0] += hit(s)
        rows.append(
            {
                "threshold": threshold,
                "false_positives": fp,
                "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
                "recall": round(tp / positives, 3),
                **{f"recall_{k}": round(v[0] / v[1], 3) for k, v in sorted(recall_by_kind.items())},
            }
        )
    return rows


if __name__ == "__main__":
    asyncio.run(main())
