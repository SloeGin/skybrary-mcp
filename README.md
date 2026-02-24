# SKYbrary MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives AI assistants structured access to SKYbrary aviation safety data, including a semantic search index over ~1,500 accident and incident reports.

## What it does

The MCP server exposes six tools to Claude (or any MCP-compatible AI):

| Tool | Description |
|------|-------------|
| `get_accident_analysis_template` | Returns a step-by-step workflow and blank template for analysing a user-supplied accident/incident report. **Call this first.** |
| `list_operational_issues` | Lists SKYbrary operational risk categories (e.g. CFIT, Runway Incursion, LOC-I) with their event-type codes. |
| `list_human_performance` | Lists SKYbrary Human Performance categories (e.g. Situational Awareness, Stress, Crew Resource Management). |
| `list_keywords` | Returns standard aviation terms and slugs for a given risk category. Required before calling `get_safety_article`. |
| `get_safety_article` | Fetches and returns the full text of a SKYbrary safety article by slug (e.g. `call-sign-confusion`). Results are cached locally for 30 days. |
| `search_accidents` | Semantically searches the RAG index of ~1,500 accident/incident reports and returns the most relevant unique reports. |
| `get_accident_report` | Returns the full pre-processed sections of a specific report by slug, for use after `search_accidents`. |

## Architecture

```
Claude Desktop / AI client
        │
        │  MCP (stdio)
        ▼
  SKYbrary MCP server (Node.js)
        │
        ├──► skybrary.aero  (live article fetch + 30-day cache)
        │
        ├──► Ollama  (embeddings for search_accidents queries)
        │
        └──► ChromaDB  (vector index of ~1,500 accident reports)
```

The MCP server runs locally. Ollama and ChromaDB can run locally or on a remote machine (e.g. a Linux box with a GPU). See [README-remote-rag.md](README-remote-rag.md) for the remote setup guide.

## Prerequisites

- Node.js 20+
- A SKYbrary account (for building the RAG index)
- [Ollama](https://ollama.com) with `mxbai-embed-large` pulled
- [ChromaDB](https://www.trychroma.com) (Docker recommended)
- Python 3.11+ (for the data pipeline scripts)

## Quick start

### 1. Install and build

```bash
pnpm install
pnpm run build
```

### 2. Start Ollama and ChromaDB

```bash
docker compose up -d
```

This starts:
- **Ollama** on port 11434 (with GPU if available) and auto-pulls `mxbai-embed-large`
- **ChromaDB** on port 8000, persisting data to `data/rag/chroma/`

### 3. Build the static MCP data files (one-time)

```bash
pip install -r scripts/mcp/requirements.txt
python scripts/mcp/populate_operational_issues.py
python scripts/mcp/populate_human_performance.py
```

Output: `data/operational_issues.json`, `data/human_performance.json`, and keyword map files.

### 4. Build the RAG index (one-time, SKYbrary credentials required)

```bash
pip install -r scripts/rag/requirements.txt

# Set credentials once for this shell session
export SKYBRARY_USER="you@example.com"
read -s SKYBRARY_PASS
export SKYBRARY_PASS

# 4a. Fetch the list of all accident/incident slugs
python scripts/rag/populate_accidents_incidents.py

# 4b. Fetch and parse each article into structured JSON
python scripts/rag/process_accidents.py --resume

# 4c. Embed and store in ChromaDB
python scripts/rag/embed_accidents.py --resume
```

Output: `data/rag/processed/*.json` (one file per report), then vectors in ChromaDB.

### 5. Configure Claude Desktop

Add to `claude_desktop_config.json` (usually at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "skybrary": {
      "command": "node",
      "args": ["/absolute/path/to/SKYbrary-MCP/dist/index.js"]
    }
  }
}
```

If Ollama and ChromaDB are on a remote machine, add the `env` block:

```json
{
  "mcpServers": {
    "skybrary": {
      "command": "node",
      "args": ["/absolute/path/to/SKYbrary-MCP/dist/index.js"],
      "env": {
        "OLLAMA_URL": "http://YOUR_RAG_HOST:11434",
        "CHROMA_URL": "http://YOUR_RAG_HOST:8000"
      }
    }
  }
}
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `mxbai-embed-large` | Embedding model name |
| `CHROMA_URL` | `http://localhost:8000` | ChromaDB server URL |
| `CHROMA_TENANT` | `default_tenant` | ChromaDB tenant |
| `CHROMA_DATABASE` | `default_database` | ChromaDB database |
| `CHROMA_COLLECTION` | `accidents_incidents` | ChromaDB collection name |

## File structure

```
SKYbrary-MCP/
├── src/
│   ├── index.ts          # MCP server — all tools defined here
│   └── cacheManager.ts   # 30-day disk cache for safety articles
├── dist/                 # Compiled JS (npm run build)
├── data/
│   ├── operational_issues.json
│   ├── operational_issues_map.json
│   ├── human_performance.json
│   ├── human_performance_map.json
│   ├── accidents_incidents.json   # slug list (from pipeline step 4a)
│   ├── cache/                     # cached safety articles
│   └── rag/
│       ├── processed/             # structured JSON per report (step 4b)
│       └── chroma/                # ChromaDB vector store (step 4c)
├── scripts/
│   ├── mcp/              # Scripts for static MCP data files
│   │   ├── populate_operational_issues.py
│   │   ├── populate_human_performance.py
│   │   └── requirements.txt
│   └── rag/              # Scripts for the RAG index
│       ├── populate_accidents_incidents.py
│       ├── process_accidents.py
│       ├── embed_accidents.py
│       └── requirements.txt
├── docker-compose.yml    # Ollama + ChromaDB services
├── README.md
└── README-remote-rag.md  # Guide for running Ollama/ChromaDB on a remote Linux machine
```

## Typical AI workflow

When a user pastes an accident/incident report, the AI should:

1. Call `get_accident_analysis_template` to get the analysis workflow
2. Call `list_operational_issues` and `list_human_performance` to get category names and codes
3. Map the report's event type codes to categories; call `list_keywords` for each
4. Optionally call `get_safety_article` for full definitions of relevant keywords
5. Call `search_accidents` to find similar historical events
6. Call `get_accident_report` on any interesting result to read the full report sections
7. Produce a structured analysis with Safety Recommendations generated from the findings

## Updating the index

### RAG index (new accident/incident reports)

When new SKYbrary reports are published:

```bash
python scripts/rag/populate_accidents_incidents.py --resume
python scripts/rag/process_accidents.py --resume
python scripts/rag/embed_accidents.py --resume
```

### MCP data files (taxonomy changes)

If SKYbrary updates its operational issue or human performance taxonomy, regenerate the static JSON files:

```bash
pip install -r scripts/mcp/requirements.txt
python scripts/mcp/populate_operational_issues.py
python scripts/mcp/populate_human_performance.py
```

Output: `data/operational_issues.json`, `data/operational_issues_map.json`, `data/human_performance.json`, `data/human_performance_map.json`.

## Development

```bash
pnpm run dev   # Launch MCP Inspector for interactive tool testing
```
