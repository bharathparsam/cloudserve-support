"""Compare real MiniLM chunk settings on development calibration data and persist Chroma."""

import argparse, hashlib, json, os
from pathlib import Path
import numpy as np
from src.retrieve import MODEL, REVISION, chunks, encode_queries
from src.ingest import sanitize_text


def run(a):
    development_raw = Path(a.development).read_bytes()
    split = json.loads(Path(a.models, "split-manifest.json").read_text())
    trusted_hash = "5a0d8912238ee9fde07b4b793bda4672261fbf3ab965bf3eb418ef6e87f95d78"
    if (
        hashlib.sha256(development_raw).hexdigest() != trusted_hash
        or split["data_hash"] != trusted_hash
    ):
        raise ValueError("Input does not match authorized development fingerprint")
    dev = json.loads(development_raw)
    import chromadb
    from sentence_transformers import SentenceTransformer

    corpus = Path(a.corpus)
    raw = corpus.read_bytes()
    docs = json.loads(raw)
    corpus_hash = hashlib.sha256(raw).hexdigest()
    snapshot = (
        Path(a.cache)
        / "hub"
        / ("models--" + MODEL.replace("/", "--"))
        / "snapshots"
        / a.revision
    )
    if not snapshot.is_dir():
        raise RuntimeError(
            "Pinned local embedding snapshot required; prepare cache explicitly"
        )
    model = SentenceTransformer(str(snapshot), device="cpu", local_files_only=True)
    model.max_seq_length = 256
    rows = [dev[i] for i in split["calibration"]]
    texts = [
        sanitize_text(
            r.get("subject", "") + "\n" + r["body"],
            [r.get("customer_name", ""), r.get("customer_id", "")],
        )
        for r in rows
    ]
    query = encode_queries(model, texts)
    comparisons = []
    candidates = []
    for size, overlap in [(600, 80), (1000, 120)]:
        passages = chunks(docs, model.tokenizer, size, overlap)
        emb = model.encode(
            [p["content"] for p in passages],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        sims = query @ emb.T
        order = np.argsort(-sims, axis=1, kind="stable")[:, :4]
        hits = []
        nonfluent = []
        for i, r in enumerate(rows):
            if r["labels"]["answerable_from_docs"]:
                hit = bool(
                    set(r["labels"]["expected_doc_ids"])
                    & {passages[j]["doc_id"] for j in order[i]}
                )
                hits.append(hit)
                if r.get("language_fluency") == "non_fluent":
                    nonfluent.append(hit)
        result = {
            "size": size,
            "overlap": overlap,
            "chunks": len(passages),
            "answerable_count": len(hits),
            "expected_doc_hit_at_4": float(np.mean(hits)) if hits else None,
            "non_fluent_count": len(nonfluent),
            "non_fluent_hit_at_4": float(np.mean(nonfluent)) if nonfluent else None,
            "max_tokens": max(
                len(model.tokenizer.encode(p["content"], add_special_tokens=True))
                for p in passages
            ),
        }
        comparisons.append(result)
        candidates.append((result, passages, emb, sims, order))
    selected = max(
        candidates, key=lambda c: (c[0]["expected_doc_hit_at_4"], -c[0]["chunks"])
    )
    result, passages, emb, sims, order = selected
    # Maximize balanced support discrimination on calibration only; no labels enter index.
    truth = np.array([bool(r["labels"]["answerable_from_docs"]) for r in rows])
    top = sims.max(axis=1)
    floors = []
    for floor in sorted(set(top.tolist())):
        predicted = top >= floor
        tpr = float(predicted[truth].mean()) if truth.any() else 0
        tnr = float((~predicted[~truth]).mean()) if (~truth).any() else 0
        floors.append(((tpr + tnr) / 2, float(floor), tpr, tnr))
    balanced, floor, tpr, tnr = max(floors, key=lambda x: (x[0], x[1]))
    collection_name = (
        "cloudserve-"
        + hashlib.sha256(
            f'{corpus_hash}:{a.revision}:{result["size"]}:{result["overlap"]}'.encode()
        ).hexdigest()[:20]
    )
    client = chromadb.PersistentClient(path=a.chroma)
    collection = client.get_or_create_collection(
        collection_name, metadata={"hnsw:space": "cosine"}
    )
    collection.upsert(
        ids=[p["chunk_id"] for p in passages],
        embeddings=emb.tolist(),
        documents=[p["content"] for p in passages],
        metadatas=[
            {k: p[k] for k in ("doc_id", "title", "start", "end")} for p in passages
        ],
    )
    out = corpus.parent
    (out / "passages.json").write_text(
        json.dumps(passages, ensure_ascii=False, indent=2)
    )
    np.save(out / "passage-embeddings.npy", emb, allow_pickle=False)
    manifest = {
        "embeddings_hash": hashlib.sha256(
            (out / "passage-embeddings.npy").read_bytes()
        ).hexdigest(),
        "corpus_hash": corpus_hash,
        "embedding_model": MODEL,
        "embedding_revision": a.revision,
        "collection": collection_name,
        "chunker": "offset-newline-token-capped-v1",
        "size": result["size"],
        "overlap": result["overlap"],
        "max_tokens": 256,
        "similarity": "1 - Chroma cosine distance",
        "collection_fingerprint": hashlib.sha256(
            json.dumps(passages, sort_keys=True).encode()
        ).hexdigest(),
        "document_count": len(docs),
        "chunk_count": len(passages),
        "retrieval_floor": floor,
    }
    (out / "index-manifest.json").write_text(json.dumps(manifest, indent=2))
    policy_path = Path(a.policy)
    policy = json.loads(policy_path.read_text())
    policy.update(
        {
            "retrieval_floor": floor,
            "relevance_floor": floor,
            "retrieval_ready": True,
            "corpus_hash": corpus_hash,
        }
    )
    policy_path.write_text(json.dumps(policy, indent=2))
    report = {
        "selection_split": "development calibration only",
        "comparisons": comparisons,
        "selected": result,
        "floor_selection": {
            "balanced_accuracy": balanced,
            "retrieval_floor": floor,
            "answerable_recall": tpr,
            "no_support_specificity": tnr,
            "answerable_n": int(truth.sum()),
            "no_support_n": int((~truth).sum()),
        },
        "limitations": [
            "Expected-doc hit is a retrieval proxy, not semantic support.",
            "Queries exceeding 254 tokens use all token windows with normalized mean aggregation; no tokens omitted.",
            "Small development calibration cohort; no validation used.",
        ],
        "manifest": manifest,
        "runtime_floor_policy": "Only top-4 candidates >= frozen retrieval floor returned; no support when all below. Raw distances retained in retrieval audit metadata.",
        "floor_effect": {
            "answerable_support_recall": tpr,
            "no_support_specificity": tnr,
            "note": "Before-floor expected-doc hit@4 is not post-floor support or semantic grounding.",
        },
    }
    Path("docs/ml-report-retrieval.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="assets/corpus/documentation.json")
    p.add_argument("--development", required=True)
    p.add_argument("--models", default="assets/models")
    p.add_argument("--chroma", default="storage/chroma")
    p.add_argument("--policy", default="policies/routing.json")
    p.add_argument("--revision", default=REVISION)
    p.add_argument("--cache", default="storage/cache/huggingface")
    run(p.parse_args())
