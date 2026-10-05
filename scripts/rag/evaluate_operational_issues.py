"""Evaluate the minimal dense Operational Issue retriever with Recall@K."""

import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
EVAL_FILE = DATA_DIR / "eval" / "operational_issues_eval.json"
RESULTS_FILE = DATA_DIR / "eval" / "operational_issues_results.json"

load_dotenv(PROJECT_ROOT / ".env")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mxbai-embed-large")
CHROMA_URL = os.environ.get("CHROMA_URL", "http://localhost:8000").rstrip("/")
TENANT = os.environ.get("CHROMA_TENANT", "default_tenant")
DATABASE = os.environ.get("CHROMA_DATABASE", "default_database")
COLLECTION = os.environ.get("OPERATIONAL_ISSUES_COLLECTION", "operational_issues")
CHROMA_BASE = f"{CHROMA_URL}/api/v2/tenants/{TENANT}/databases/{DATABASE}"


async def main() -> None:
    cases = json.loads(EVAL_FILE.read_text())
    hits = {1: 0, 3: 0, 5: 0}
    outputs = []
    async with httpx.AsyncClient(timeout=60) as client:
        collection = await client.get(f"{CHROMA_BASE}/collections/{COLLECTION}")
        collection.raise_for_status()
        collection_id = collection.json()["id"]
        for case in cases:
            embedded = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": OLLAMA_MODEL, "input": case["queryText"], "truncate": True},
            )
            embedded.raise_for_status()
            queried = await client.post(
                f"{CHROMA_BASE}/collections/{collection_id}/query",
                json={
                    "query_embeddings": [embedded.json()["embeddings"][0]],
                    "n_results": 5,
                    "include": ["metadatas", "distances"],
                },
            )
            queried.raise_for_status()
            payload = queried.json()
            candidates = [{
                "code": metadata["code"],
                "name": metadata["name"],
                "score": round(1 - distance, 6),
            } for metadata, distance in zip(payload["metadatas"][0], payload["distances"][0])]
            expected = set(case["expectedIssueCodes"])
            retrieved = [candidate["code"] for candidate in candidates]
            case_hits = {}
            for k in hits:
                matched = bool(expected.intersection(retrieved[:k]))
                case_hits[str(k)] = matched
                hits[k] += int(matched)
            outputs.append({**case, "candidates": candidates, "hits": case_hits})
            print(f"{case['accidentId']} expected={sorted(expected)} top5={retrieved} hit@5={case_hits['5']}")

    metrics = {f"recall@{k}": hits[k] / len(cases) for k in hits}
    result = {
        "evaluationRule": "A multi-label case is a hit at K when any official expected issue appears in Top-K.",
        "caseCount": len(cases),
        "metrics": metrics,
        "cases": outputs,
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(metrics, indent=2))
    print(f"Saved per-case results to {RESULTS_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
