# Remote RAG Setup

This document describes how to build and run the full RAG pipeline on a remote
Linux machine with GPU, avoiding ChromaDB version mismatches between platforms.
The Mac only runs the MCP server; everything else lives on Linux.

## Architecture

```
Mac (dev machine)                     Linux (remote, e.g. YOUR_RAG_HOST)
─────────────────                     ─────────────────────────────────
MCP server (Node.js)       ──HTTP──>  ChromaDB  (Docker, port 8000)
Claude Desktop             ──HTTP──>  Ollama    (Docker, port 11434, GPU)

                                      scripts/rag/
                                        populate_accidents_incidents.py
                                        process_accidents.py
                                        embed_accidents.py
                                              ↓
                                        data/rag/processed/*.json
                                              ↓
                                        data/rag/chroma/  ←── Docker volume
```

## File structure

```
scripts/
  mcp/                          # Populate static MCP data files
    populate_operational_issues.py
    populate_human_performance.py
    requirements.txt            # httpx, beautifulsoup4
  rag/                          # Build the ChromaDB vector index
    populate_accidents_incidents.py
    process_accidents.py
    embed_accidents.py
    requirements.txt            # httpx, beautifulsoup4, chromadb
docker-compose.yml              # Ollama + ChromaDB services
```

## Step 1 — Set up Docker services on the remote machine

Copy the `docker-compose.yml` to the Linux machine and start both services:

```bash
# From Mac
rsync -av docker-compose.yml user@YOUR_RAG_HOST:~/skybrary-rag/

# On Linux
ssh user@YOUR_RAG_HOST
cd ~/skybrary-rag
docker compose up -d
```

> **AMD GPU**: replace the `deploy.resources` block in `docker-compose.yml` with:
> ```yaml
> devices:
>   - /dev/kfd:/dev/kfd
>   - /dev/dri:/dev/dri
> ```

Verify both containers are running:

```bash
curl http://localhost:11434/api/tags          # Ollama
curl http://localhost:8000/api/v2/heartbeat   # ChromaDB
```

## Step 2 — Pull the embedding model

```bash
docker exec -it skybrary-rag-ollama-1 ollama pull mxbai-embed-large
```

Verify GPU is being used — you should see `GPU layers > 0` in the Ollama logs:

```bash
docker logs skybrary-rag-ollama-1 --tail 20
```

## Step 3 — Scrape & process articles (Linux)

Copy the RAG scripts to the Linux machine and install dependencies (one-time):

```bash
# From Mac
rsync -av scripts/rag/ user@YOUR_RAG_HOST:~/skybrary-rag/scripts/rag/

# On Linux
cd ~/skybrary-rag
pip install -r scripts/rag/requirements.txt
```

Then run the pipeline with your SKYbrary credentials:

```bash
# Set credentials once for this shell session
export SKYBRARY_USER="you@example.com"
read -s SKYBRARY_PASS
export SKYBRARY_PASS

# 3a. Fetch the list of all accident/incident report slugs
python scripts/rag/populate_accidents_incidents.py

# 3b. Fetch and parse each article into a structured JSON file
#     Use --resume to continue an interrupted run
python scripts/rag/process_accidents.py --resume
```

Output: `data/rag/processed/*.json` — one file per accident/incident report.

### Bootstrap: using already-processed files from Mac

If you have already run the pipeline on the Mac and want to skip re-scraping,
rsync the processed files over instead:

```bash
rsync -av --progress data/rag/processed/ \
      user@YOUR_RAG_HOST:~/skybrary-rag/data/rag/processed/
```

## Step 4 — Embed on the remote machine

```bash
# On Linux, from ~/skybrary-rag
# OLLAMA_URL defaults to http://localhost:11434 which reaches the Docker container
python scripts/rag/embed_accidents.py

# Use --resume to skip chunks already present in ChromaDB
python scripts/rag/embed_accidents.py --resume
```

The `docker-compose.yml` mounts `./data/rag/chroma` as the ChromaDB data
directory, so ChromaDB serves the freshly embedded data immediately — no
container restart needed.

## Step 5 — Configure the MCP server (Mac)

Set the remote endpoints in:
`claude_desktop_config.json`

```json
{
  "mcpServers": {
    "skybrary": {
      "command": "node",
      "args": ["/path/to/SKYbrary-MCP/dist/index.js"],
      "env": {
        "OLLAMA_URL": "http://YOUR_RAG_HOST:11434",
        "OLLAMA_MODEL": "mxbai-embed-large",
        "CHROMA_URL": "http://YOUR_RAG_HOST:8000",
        "CHROMA_COLLECTION": "accidents_incidents"
      }
    }
  }
}
```

## Updating the index

When new SKYbrary reports are published, run on Linux:

```bash
cd ~/skybrary-rag
python scripts/rag/populate_accidents_incidents.py --resume
python scripts/rag/process_accidents.py --resume
python scripts/rag/embed_accidents.py --resume
```

No Docker restart needed — ChromaDB persists changes to the volume immediately.

## Updating MCP data files

The static data files (`operational_issues.json`, `human_performance.json`, etc.)
only need to be regenerated if SKYbrary changes its taxonomy. Run on Mac:

```bash
pip install -r scripts/mcp/requirements.txt
python scripts/mcp/populate_operational_issues.py
python scripts/mcp/populate_human_performance.py
```
