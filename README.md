# SKYbrary MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives AI assistants structured access to SKYbrary aviation safety data, including a semantic search index over ~1,500 accident and incident reports.

## What it does

The MCP server exposes eight tools to Claude (or any MCP-compatible AI):

| Tool | Description |
|------|-------------|
| `get_accident_analysis_template` | Returns a step-by-step workflow and blank template for analysing a user-supplied accident/incident report. **Call this first.** |
| `list_operational_issues` | Lists SKYbrary operational risk categories (e.g. CFIT, Runway Incursion, LOC-I) with their event-type codes. |
| `list_human_performance` | Lists SKYbrary Human Performance categories (e.g. Situational Awareness, Stress, Crew Resource Management). |
| `list_keywords` | Returns standard aviation terms and slugs for a given risk category. Required before calling `get_safety_article`. |
| `get_safety_article` | Fetches and returns the full text of a SKYbrary safety article by slug (e.g. `call-sign-confusion`). Results are cached locally for 30 days. |
| `search_operational_issues` | Retrieves the closest official Operational Issue definitions as grounded candidates; it does not make the final classification. |
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
        ├──► Ollama  (query and indexing embeddings)
        │
        └──► ChromaDB  (separate accident and Operational Issue collections)
```

The MCP server runs locally. Ollama and ChromaDB can run locally or on a remote machine (e.g. a Linux box with a GPU). See [README-remote-rag.md](README-remote-rag.md) for the remote setup guide.

## Prerequisites

- Node.js 22.19+ (Node.js 24 LTS recommended)
- pnpm 12.9.1
- A SKYbrary account with access to the protected accident/incident content
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

### 4. Build the RAG indexes (one-time)

```bash
pip install -r scripts/rag/requirements.txt
python -m playwright install chromium

# Create local configuration, add SKYbrary credentials, and set service URLs
cp .env.example .env

# 4a. Fetch the list of all accident/incident slugs
python scripts/rag/populate_accidents_incidents.py

# 4b. Fetch and parse each article into structured JSON
python scripts/rag/process_accidents.py --resume

# 4c. Embed and store in ChromaDB
python scripts/rag/embed_accidents.py

# 4d. Build the separate Operational Issues candidate index
python scripts/rag/build_operational_issues_index.py
```

The scraper uses a real browser to pass SKYbrary's JavaScript check, submit the
login form, and retain the authenticated cookies required for complete article
content. If Chrome is already installed, you can set
`SKYBRARY_BROWSER_EXECUTABLE` instead of installing Playwright's Chromium.
Processed files are marked `content_access: authenticated`; `--resume`
automatically re-fetches older preview files that do not have this marker.

Output: `data/rag/processed/*.json`, accident vectors in `accidents_incidents`,
and 16 canonical taxonomy vectors in the separate `operational_issues` collection.

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

If Ollama and ChromaDB are on a remote machine, set their URLs in the repository
root `.env` file:

```dotenv
OLLAMA_URL=http://YOUR_RAG_HOST:11434
CHROMA_URL=http://YOUR_RAG_HOST:8000
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SKYBRARY_USER` | — | SKYbrary account username or email; required by the accident pipeline |
| `SKYBRARY_PASS` | — | SKYbrary account password; required by the accident pipeline |
| `SKYBRARY_BASE_URL` | `https://skybrary.aero` | SKYbrary base URL |
| `SKYBRARY_LOGIN_URL` | `https://skybrary.aero/user/login` | Browser login form URL |
| `SCRAPER_USER_AGENT` | `MCP-Scraper/1.0` | Scraper HTTP user agent |
| `SCRAPER_ACCEPT_LANGUAGE` | `en-US,en;q=0.9` | Scraper language header |
| `SCRAPER_REQUEST_TIMEOUT_SECONDS` | `30` | Scraper request timeout |
| `SCRAPER_DELAY_SECONDS` | `5` | Delay between SKYbrary requests |
| `SCRAPER_MAX_ATTEMPTS` | `5` | Maximum browser fetch attempts after rate limiting |
| `SCRAPER_RETRY_BASE_SECONDS` | `5` | Initial exponential-backoff delay after HTTP 429 |
| `SCRAPER_RATE_LIMIT_DELAY_SECONDS` | `30` | Retry delay after HTTP 429 |
| `SKYBRARY_BROWSER_HEADLESS` | `true` | Run the SKYbrary browser without a visible window |
| `SKYBRARY_BROWSER_CHANNEL` | — | Optional Playwright browser channel, such as `chrome` |
| `SKYBRARY_BROWSER_EXECUTABLE` | — | Optional absolute path to an installed Chromium browser |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `mxbai-embed-large` | Embedding model name |
| `OLLAMA_PORT` | `11434` | Host port exposed by Docker Compose |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `60` | Embedding request timeout |
| `OLLAMA_MAX_ATTEMPTS` | `3` | Maximum attempts for transient Ollama 5xx responses |
| `EMBEDDING_DELAY_SECONDS` | `1` | Delay between embedded articles |
| `EMBEDDING_RATE_LIMIT_DELAY_SECONDS` | `30` | Ollama retry delay after HTTP 429 |
| `CHROMA_URL` | `http://localhost:8000` | ChromaDB server URL |
| `CHROMA_PORT` | `8000` | Host port exposed by Docker Compose |
| `CHROMA_TENANT` | `default_tenant` | ChromaDB tenant |
| `CHROMA_DATABASE` | `default_database` | ChromaDB database |
| `CHROMA_COLLECTION` | `accidents_incidents` | ChromaDB collection name |
| `OPERATIONAL_ISSUES_COLLECTION` | `operational_issues` | Separate Operational Issues collection name |
| `MIN_SECTION_CHARS` | `80` | Minimum section length to embed |
| `MAX_CHUNK_CHARS` | `1800` | Maximum embedding chunk length |

The Python scripts, compiled MCP server, and Docker Compose all read the root
`.env` file. Existing system environment variables take precedence.

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
│       ├── build_operational_issues_index.py
│       ├── build_accident_label_map.py
│       ├── build_operational_issue_eval_set.py
│       ├── evaluate_operational_issues.py
│       └── requirements.txt
├── docker-compose.yml    # Ollama + ChromaDB services
├── README.md
└── README-remote-rag.md  # Guide for running Ollama/ChromaDB on a remote Linux machine
```

## Typical AI workflow

When a user pastes an accident/incident report, the AI should:

1. Call `get_accident_analysis_template` to get the analysis workflow
2. Call `list_operational_issues` and `list_human_performance` to get category names and codes
3. Call `search_operational_issues` with the incident facts to retrieve grounded candidates
4. Map any explicit event type codes to categories; call `list_keywords` for each
5. Optionally call `get_safety_article` for full definitions of relevant keywords
6. Call `search_accidents` to find similar historical events
7. Call `get_accident_report` on any interesting result to read the full report sections
8. Produce a structured analysis with Safety Recommendations generated from the findings

## Updating the index

### RAG index (new accident/incident reports)

When new SKYbrary reports are published:

```bash
python scripts/rag/populate_accidents_incidents.py --resume
python scripts/rag/process_accidents.py --resume
# Run without --resume after replacing previews so existing chunk IDs are updated
python scripts/rag/embed_accidents.py
python scripts/rag/build_operational_issues_index.py
```

### Operational Issue retrieval evaluation

The baseline evaluates dense retrieval only. Queries contain the report
`Description`; labels come from the authenticated report's official Event Type
field and are never included in the query text. For multi-label reports,
Recall@K counts a hit when any expected code appears in the top K.

```bash
python scripts/rag/build_operational_issue_eval_set.py
python scripts/rag/evaluate_operational_issues.py
```

The deterministic 20-case set and per-case Top-5 output are written under
`data/eval/`. This first baseline intentionally has no reranker, LLM classifier,
query rewriting, synonym expansion, metadata filters, or score threshold.

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
