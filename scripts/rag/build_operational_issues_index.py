"""Build the standalone dense vector index for SKYbrary Operational Issues."""

import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TAXONOMY_FILE = DATA_DIR / "operational_issues.json"
KEYWORDS_FILE = DATA_DIR / "operational_issues_map.json"
DOCUMENTS_FILE = DATA_DIR / "rag" / "operational_issues_documents.json"

load_dotenv(PROJECT_ROOT / ".env")

SKYBRARY_BASE_URL = os.environ.get("SKYBRARY_BASE_URL", "https://skybrary.aero").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mxbai-embed-large")
CHROMA_URL = os.environ.get("CHROMA_URL", "http://localhost:8000").rstrip("/")
CHROMA_TENANT = os.environ.get("CHROMA_TENANT", "default_tenant")
CHROMA_DATABASE = os.environ.get("CHROMA_DATABASE", "default_database")
COLLECTION = os.environ.get("OPERATIONAL_ISSUES_COLLECTION", "operational_issues")
CHROMA_BASE = f"{CHROMA_URL}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DATABASE}"
TIMEOUT = float(os.environ.get("OLLAMA_REQUEST_TIMEOUT_SECONDS", "60"))


def build_documents() -> list[dict]:
    taxonomy = json.loads(TAXONOMY_FILE.read_text())
    keyword_map = json.loads(KEYWORDS_FILE.read_text())
    by_code: dict[str, dict] = {}

    for name, item in taxonomy.items():
        code = item.get("code", "").strip()
        if not code:
            continue
        definition = item.get("description", "").strip()
        slug = item.get("slug", "").strip()
        entry = by_code.setdefault(code, {"aliases": [], "keyword_names": set()})
        entry["aliases"].append(name)
        for keyword in keyword_map.get(name, {}).get("keywords", []):
            keyword_name = keyword.get("name", "").strip()
            if keyword_name:
                entry["keyword_names"].add(keyword_name)
        if definition and (not entry.get("definition") or slug):
            entry.update({"name": name, "definition": definition, "slug": slug})

    documents: list[dict] = []
    for code, item in sorted(by_code.items()):
        if not item.get("definition"):
            print(f"Skipping {code}: no official definition is available")
            continue
        keywords = sorted(item["keyword_names"])
        slug = item.get("slug", "")
        source_url = f"{SKYBRARY_BASE_URL}/operational-issues/{slug}" if slug else None
        parts = [
            f"Operational Issue: {item['name']}",
            f"Code: {code}",
            "",
            "Definition:",
            item["definition"],
        ]
        if keywords:
            parts.extend(["", "Keywords:", ", ".join(keywords)])
        documents.append({
            "id": slug or code.lower(),
            "name": item["name"],
            "code": code,
            "definition": item["definition"],
            "keywords": keywords,
            "aliases": sorted(set(item["aliases"]) - {item["name"]}),
            "sourceUrl": source_url,
            "embeddingText": "\n".join(parts),
        })
    return documents


async def embed(client: httpx.AsyncClient, text: str) -> list[float]:
    max_attempts = int(os.environ.get("OLLAMA_MAX_ATTEMPTS", "3"))
    for attempt in range(max_attempts):
        response = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": OLLAMA_MODEL, "input": text, "truncate": True},
            timeout=TIMEOUT,
        )
        if response.status_code >= 500 and attempt + 1 < max_attempts:
            delay = 2**attempt
            print(f"Ollama returned {response.status_code}; retrying in {delay}s")
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        embeddings = response.json().get("embeddings", [])
        vector = embeddings[0] if embeddings else None
        if not vector:
            raise RuntimeError("Ollama returned no embedding")
        return vector
    raise RuntimeError("Ollama embedding attempts exhausted")


async def main() -> None:
    documents = build_documents()
    DOCUMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_FILE.write_text(json.dumps(documents, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(documents)} canonical documents to {DOCUMENTS_FILE}")

    async with httpx.AsyncClient() as client:
        # Finish all expensive work before replacing the existing collection, so a
        # transient Ollama failure cannot leave an empty index behind.
        embeddings = []
        for index, document in enumerate(documents, 1):
            embeddings.append(await embed(client, document["embeddingText"]))
            print(f"Embedded [{index}/{len(documents)}] {document['code']} {document['name']}")

        existing = await client.get(f"{CHROMA_BASE}/collections/{COLLECTION}", timeout=15)
        if existing.status_code == 200:
            deleted = await client.delete(f"{CHROMA_BASE}/collections/{COLLECTION}", timeout=15)
            deleted.raise_for_status()
            print(f"Deleted existing '{COLLECTION}' collection")

        created = await client.post(
            f"{CHROMA_BASE}/collections",
            json={"name": COLLECTION, "metadata": {"hnsw:space": "cosine"}},
            timeout=15,
        )
        created.raise_for_status()
        collection_id = created.json()["id"]

        response = await client.post(
            f"{CHROMA_BASE}/collections/{collection_id}/add",
            json={
                "ids": [document["id"] for document in documents],
                "embeddings": embeddings,
                "documents": [document["embeddingText"] for document in documents],
                "metadatas": [{
                    "issue_id": document["id"],
                    "name": document["name"],
                    "code": document["code"],
                    "definition": document["definition"],
                    "source_url": document["sourceUrl"] or "",
                    "keywords": " | ".join(document["keywords"]),
                } for document in documents],
            },
            timeout=60,
        )
        response.raise_for_status()
        print(f"Indexed {len(documents)} documents in '{COLLECTION}'")


if __name__ == "__main__":
    asyncio.run(main())
