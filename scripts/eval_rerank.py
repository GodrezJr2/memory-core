"""Offline experiment: does a local cross-encoder improve memory-core's ordering?

Retrieval already puts the answer in the top 5 for 93% of the eval questions, but only 66% at rank 1:
the weak part is ordering, not finding. This script re-scores the top candidates of the real recall path
with a small cross-encoder (ms-marco-MiniLM-L-6-v2, quantised ONNX, CPU only) and reports both orderings
on the same candidate lists, so the comparison is apples to apples.

No memory text leaves the machine: the model runs locally with onnxruntime.

  PG_PASSWORD=$(cat ./data/pg_password) \
  ~/.venv-rerank/bin/python scripts/eval_rerank.py --json /tmp/eval_rerank.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("PG_HOST", "127.0.0.1")
os.environ.setdefault("PG_PORT", "5433")
os.environ.setdefault("EMBED_URL", "http://127.0.0.1:8421")
if not os.environ.get("DATABASE_URL") and os.environ.get("PG_PASSWORD"):
    pw = quote(os.environ["PG_PASSWORD"], safe="")
    os.environ["DATABASE_URL"] = f"postgresql://memory:{pw}@{os.environ['PG_HOST']}:{os.environ['PG_PORT']}/memory"

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from tokenizers import Tokenizer  # noqa: E402

from app import store as store_mod  # noqa: E402
from app.config import load  # noqa: E402
from app.rules import Agent, default_scopes  # noqa: E402

QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "eval" / "recall_questions.json"
MODEL_DIR = Path.home() / ".cache" / "rerank"
MAX_LEN = 256


async def live_ids(store: store_mod.Store, expect: list[int]) -> list[int]:
    pool = await store.pool()
    out = []
    async with pool.acquire() as con:
        for mid in expect:
            seen: set[int] = set()
            while mid is not None and mid not in seen:
                seen.add(mid)
                row = await con.fetchrow("select valid_to, superseded_by from memory where id = $1", mid)
                if row is None:
                    mid = None
                elif row["valid_to"] is None:
                    out.append(mid)
                    break
                else:
                    mid = row["superseded_by"]
    return out


def encode_pairs(tok: Tokenizer, query: str, docs: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cls, sep = tok.token_to_id("[CLS]"), tok.token_to_id("[SEP]")
    q = tok.encode(query, add_special_tokens=False).ids
    ids, types, masks = [], [], []
    for doc in docs:
        d = tok.encode(doc, add_special_tokens=False).ids
        qq = q[: MAX_LEN - 2 - min(len(d), MAX_LEN - 2 - len(q))]
        dd = d[: MAX_LEN - 2 - len(qq)]
        row = [cls] + qq + dd + [sep]
        ids.append(row)
        types.append([0] * (len(qq) + 1) + [1] * (len(dd) + 1))
        masks.append([1] * len(row))
    width = max(len(r) for r in ids)
    pad = lambda rows: np.array([r + [0] * (width - len(r)) for r in rows], dtype=np.int64)  # noqa: E731
    return pad(ids), pad(masks), pad(types)


def hit_metrics(rows: list[dict], key: str) -> dict:
    scored = [r for r in rows if r["want"]]
    n = len(scored)

    def hit(k: int) -> float:
        return round(sum(1 for r in scored if r[key] and r[key] <= k) / n, 3)

    return {
        "hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5),
        "mrr@10": round(sum(1 / r[key] for r in scored if r[key] and r[key] <= 10) / n, 3),
    }


async def run(args: argparse.Namespace) -> dict:
    settings = load()
    store = store_mod.Store(settings)
    agent = Agent("eval", default_scopes("eval"))
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]

    tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    sess = ort.InferenceSession(str(MODEL_DIR / "model.onnx"), providers=["CPUExecutionProvider"])
    ins = {i.name for i in sess.get_inputs()}
    out_name = sess.get_outputs()[0].name

    rows, recall_ms, rerank_ms = [], [], []
    for item in questions:
        want = await live_ids(store, item["expect"])
        if not want:
            continue
        t0 = time.perf_counter()
        res = await store.recall(agent, item["q"], item.get("scope"), None, args.k, expand=False, prefer=None)
        recall_ms.append((time.perf_counter() - t0) * 1000)
        cands = res["results"]
        if not cands:
            continue
        ids = [c["id"] for c in cands]
        sims = [c["similarity"] for c in cands]

        t0 = time.perf_counter()
        ids_arr, mask, types = encode_pairs(tok, item["q"], [c["text"] for c in cands])
        feeds = {"input_ids": ids_arr, "attention_mask": mask}
        if "token_type_ids" in ins:
            feeds["token_type_ids"] = types
        logits = sess.run([out_name], feeds)[0].reshape(-1)
        rerank_ms.append((time.perf_counter() - t0) * 1000)

        base_rank = next((i + 1 for i, mid in enumerate(ids) if mid in want), None)
        order = list(np.argsort(-logits))
        rr_rank = next((i + 1 for i, idx in enumerate(order) if ids[idx] in want), None)
        margin = sims[0] - sims[1] if len(sims) > 1 else 1.0
        gated_ids = ids if margin >= args.margin else [ids[i] for i in order]
        gated_rank = next((i + 1 for i, mid in enumerate(gated_ids) if mid in want), None)
        rows.append({
            "q": item["q"], "want": want, "base": base_rank, "rerank": rr_rank, "gated": gated_rank,
            "margin": round(margin, 3), "base_top": ids[0], "rerank_top": ids[order[0]],
            "ce": round(float(logits[order[0]]), 2),
        })

    n = len(rows)
    summary = {
        "questions": n,
        "k": args.k,
        "gate_margin": args.margin,
        "ceiling_hit@k": round(sum(1 for r in rows if r["base"]) / n, 3),
        "baseline": hit_metrics(rows, "base"),
        "rerank": hit_metrics(rows, "rerank"),
        "gated": hit_metrics(rows, "gated"),
        "reorder_rate": round(sum(1 for r in rows if r["rerank_top"] != r["base_top"]) / n, 3),
        "recall_ms_median": round(statistics.median(recall_ms)),
        "rerank_ms_median": round(statistics.median(rerank_ms)),
        "rerank_ms_p95": round(sorted(rerank_ms)[max(0, int(len(rerank_ms) * 0.95) - 1)]),
    }
    return {"summary": summary, "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", default=str(QUESTIONS))
    ap.add_argument("--k", type=int, default=20, help="candidates per question to rerank")
    ap.add_argument("--margin", type=float, default=0.02, help="skip reranking when the top-1 cosine leads by this much")
    ap.add_argument("--json", help="write the full run here")
    args = ap.parse_args()
    out = asyncio.run(run(args))
    print(json.dumps(out["summary"], indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    moved = [r for r in out["rows"] if (r["base"] or 99) > 1 and r["rerank"] == 1]
    broke = [r for r in out["rows"] if r["base"] == 1 and (r["rerank"] or 99) > 1]
    print(f"fixed: {len(moved)}  broken: {len(broke)}")
    for r in moved[:5]:
        print(f"  + {r['q']}  {r['base']} -> 1")
    for r in broke[:5]:
        print(f"  - {r['q']}  1 -> {r['rerank']}")


if __name__ == "__main__":
    main()
