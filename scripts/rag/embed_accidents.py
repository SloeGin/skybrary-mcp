"""
Reads processed accident/incident JSON files, embeds them via Ollama, and
stores the results in a ChromaDB collection via the ChromaDB HTTP API (v1).

Input : data/rag/processed/{slug}.json   (produced by process_accidents.py)
Output: ChromaDB collection via HTTP     (served by Docker, see docker-compose.yml)

Each article is split into one chunk per section plus a metadata header chunk:

  Chunk ID format:  {slug}::{section_key}
  e.g.  a109-vicinity-london-heliport-london-uk-2013::metadata
        a109-vicinity-london-heliport-london-uk-2013::Description
        a109-vicinity-london-heliport-london-uk-2013::Safety Recommendations

Chunk text is prefixed with report context so the embedding captures it:

  "Report: A109, vicinity London Heliport ...\n\n[Description]\nOn 16 Jan ..."

Metadata stored per chunk (usable for RAG pre-filtering):
  slug, title, section, event_types (comma-sep), aircraft_types (comma-sep),
  location, date, url

Config (environment variables):
  OLLAMA_URL        (default http://localhost:11434)
  OLLAMA_MODEL      (default mxbai-embed-large)
  CHROMA_URL        (default http://localhost:8000)
  CHROMA_COLLECTION (default accidents_incidents)

Run:
    python scripts/rag/embed_accidents.py

Use --resume to skip chunks already present in ChromaDB.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
import os

DATA_DIR      = Path(__file__).parent.parent.parent / "data"
PROCESSED_DIR = DATA_DIR / "rag" / "processed"

OLLAMA_URL        = os.environ.get("OLLAMA_URL",        "http://localhost:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL",      "mxbai-embed-large")
CHROMA_URL        = os.environ.get("CHROMA_URL",        "http://localhost:8000")
CHROMA_TENANT     = os.environ.get("CHROMA_TENANT",     "default_tenant")
CHROMA_DATABASE   = os.environ.get("CHROMA_DATABASE",   "default_database")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "accidents_incidents")
CHROMA_BASE       = f"{CHROMA_URL}/api/v2/tenants/{CHROMA_TENANT}/databases/{CHROMA_DATABASE}"

SLEEP_BETWEEN_ARTICLES = 1   # seconds between articles (embedding is fast)
SLEEP_429 = 30               # seconds if Ollama returns 429

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

# Sections we always want — everything else in the article is also kept
PRIORITY_SECTIONS = {
    "Description", "Investigation",
    "Causal Factors", "Findings", "Probable Cause",
    "Safety Recommendations", "Safety Actions",
}

# Sections that are noisy / not useful for RAG
SKIP_SECTIONS = {"Related Articles", "See Also", "Further Reading"}

# Minimum section length to bother embedding (chars)
MIN_SECTION_CHARS = 80

# mxbai-embed-large (and most local embedding models) have a 512-token limit.
# ~4 chars/token → truncate at ~1800 chars to stay safely under the limit.
MAX_CHUNK_CHARS = 1800


def _truncate(text: str, max_chars: int = MAX_CHUNK_CHARS) -> str:
    """Truncate to max_chars at a word boundary."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] if last_space > 0 else truncated


def build_chunks(article: dict) -> list[dict]:
    """
    Returns a list of chunk dicts, each with:
      id       - unique string
      text     - the text to embed
      metadata - dict of fields for ChromaDB metadata
    """
    slug   = article["slug"]
    title  = article["title"]
    chunks: list[dict] = []

    base_meta = {
        "slug":          slug,
        "title":         title,
        "url":           article.get("url", ""),
        "event_types":   ", ".join(article.get("event_types", [])),
        "aircraft_types": ", ".join(article.get("aircraft", [])),
        "location":      article.get("location", ""),
        "date":          article.get("date", ""),
    }

    # ── Chunk 0: metadata / header ─────────────────────────────────────────
    meta_text = (
        f"Report: {title}\n"
        f"Date: {base_meta['date']}\n"
        f"Location: {base_meta['location']}\n"
        f"Aircraft: {base_meta['aircraft_types']}\n"
        f"Event Types: {base_meta['event_types']}\n"
    )
    # Append per-event-type tags so the header chunk captures them
    for code, tags in article.get("event_type_tags", {}).items():
        if tags:
            meta_text += f"{code} Tags: {', '.join(tags)}\n"

    summary = article.get("summary", "").strip()
    if summary:
        meta_text += f"\nSummary: {summary}"

    chunks.append({
        "id":       f"{slug}::metadata",
        "text":     _truncate(meta_text),
        "metadata": {**base_meta, "section": "metadata"},
    })

    # ── Section chunks ──────────────────────────────────────────────────────
    for section_name, section_text in article.get("sections", {}).items():
        if section_name in SKIP_SECTIONS:
            continue
        if len(section_text) < MIN_SECTION_CHARS:
            continue

        chunk_text = _truncate(f"Report: {title}\n\n[{section_name}]\n{section_text}")
        chunks.append({
            "id":       f"{slug}::{section_name}",
            "text":     chunk_text,
            "metadata": {**base_meta, "section": section_name},
        })

    return chunks

# ---------------------------------------------------------------------------
# Ollama embedding
# ---------------------------------------------------------------------------

async def embed_text(client: httpx.AsyncClient, text: str) -> list[float] | None:
    """Call Ollama /api/embeddings and return the embedding vector."""
    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": OLLAMA_MODEL, "prompt": text},
            timeout=60,
        )
        if resp.status_code == 429:
            print(f"  Ollama 429 — sleeping {SLEEP_429} s ...")
            await asyncio.sleep(SLEEP_429)
            resp = await client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": OLLAMA_MODEL, "prompt": text},
                timeout=60,
            )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except Exception as e:
        print(f"  Embedding error: {e}")
        return None

# ---------------------------------------------------------------------------
# ChromaDB HTTP helpers
# ---------------------------------------------------------------------------

async def get_or_create_collection(client: httpx.AsyncClient) -> str:
    """Return the collection UUID, creating the collection if needed."""
    # Try to get existing collection
    resp = await client.get(
        f"{CHROMA_BASE}/collections/{CHROMA_COLLECTION}",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()["id"]

    # Create it
    resp = await client.post(
        f"{CHROMA_BASE}/collections",
        json={"name": CHROMA_COLLECTION, "metadata": {"hnsw:space": "cosine"}},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def get_existing_ids(client: httpx.AsyncClient, collection_id: str) -> set[str]:
    """Fetch all IDs already stored in the collection."""
    ids: set[str] = set()
    offset = 0
    limit  = 1000
    while True:
        resp = await client.post(
            f"{CHROMA_BASE}/collections/{collection_id}/get",
            json={"include": [], "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json().get("ids", [])
        ids.update(batch)
        if len(batch) < limit:
            break
        offset += limit
    return ids


async def collection_count(client: httpx.AsyncClient, collection_id: str) -> int:
    resp = await client.get(
        f"{CHROMA_BASE}/collections/{collection_id}/count",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


async def add_chunks(
    client: httpx.AsyncClient,
    collection_id: str,
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict],
) -> None:
    resp = await client.post(
        f"{CHROMA_BASE}/collections/{collection_id}/add",
        json={
            "ids":        ids,
            "embeddings": embeddings,
            "documents":  documents,
            "metadatas":  metadatas,
        },
        timeout=60,
    )
    resp.raise_for_status()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    resume = "--resume" in sys.argv

    if not PROCESSED_DIR.exists() or not any(PROCESSED_DIR.glob("*.json")):
        print(f"ERROR: No processed articles found in {PROCESSED_DIR}.")
        print("       Run process_accidents.py first.")
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        # Verify Ollama is reachable
        try:
            ping = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            ping.raise_for_status()
        except Exception as e:
            print(f"ERROR: Cannot reach Ollama at {OLLAMA_URL}: {e}")
            sys.exit(1)

        # Verify ChromaDB is reachable and get/create collection
        try:
            collection_id = await get_or_create_collection(client)
        except Exception as e:
            print(f"ERROR: Cannot reach ChromaDB at {CHROMA_URL}: {e}")
            sys.exit(1)

        existing_ids: set[str] = set()
        if resume:
            existing_ids = await get_existing_ids(client, collection_id)
            print(f"Resuming: {len(existing_ids)} chunks already in ChromaDB")

        # ── Process files ────────────────────────────────────────────────────
        files = sorted(PROCESSED_DIR.glob("*.json"))
        print(f"Found {len(files)} processed articles\n")

        total_chunks_added = 0

        for file_idx, path in enumerate(files, 1):
            with open(path) as f:
                article = json.load(f)

            slug   = article["slug"]
            chunks = build_chunks(article)

            # Filter out chunks already in ChromaDB when resuming
            if resume:
                chunks = [c for c in chunks if c["id"] not in existing_ids]

            if not chunks:
                print(f"[{file_idx}/{len(files)}] {slug}: all chunks already embedded, skipping")
                continue

            print(f"[{file_idx}/{len(files)}] {slug}: {len(chunks)} chunk(s) to embed")

            # Embed each chunk
            ids_batch        : list[str]         = []
            embeddings_batch : list[list[float]] = []
            documents_batch  : list[str]         = []
            metadatas_batch  : list[dict]        = []

            for chunk in chunks:
                vec = await embed_text(client, chunk["text"])
                if vec is None:
                    print(f"  Skipping chunk '{chunk['id']}' (embedding failed)")
                    continue
                ids_batch.append(chunk["id"])
                embeddings_batch.append(vec)
                documents_batch.append(chunk["text"])
                metadatas_batch.append(chunk["metadata"])

            if ids_batch:
                await add_chunks(
                    client, collection_id,
                    ids_batch, embeddings_batch, documents_batch, metadatas_batch,
                )
                total_chunks_added += len(ids_batch)
                count = await collection_count(client, collection_id)
                print(f"  Stored {len(ids_batch)} chunk(s). Collection total: {count}")

            if file_idx < len(files):
                await asyncio.sleep(SLEEP_BETWEEN_ARTICLES)

    print(f"\nDone. Added {total_chunks_added} new chunks.")


if __name__ == "__main__":
    asyncio.run(main())
