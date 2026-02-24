import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios from "axios";
import * as cheerio from "cheerio";
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs/promises";
import { existsSync } from "fs";
import { CacheManager } from "./cacheManager.js";

// Initialize Server
const server = new McpServer({
  name: "SKYbrary Context Service",
  version: "1.0.0"
});

// Data Directory Setup (ESM-safe __dirname)
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DATA_DIR = path.resolve(__dirname, "..", "data");
const cache = new CacheManager("data/cache", 30);

// RAG config — override via environment variables if needed
const OLLAMA_URL        = process.env.OLLAMA_URL        ?? "http://localhost:11434";
const OLLAMA_MODEL      = process.env.OLLAMA_MODEL      ?? "mxbai-embed-large";
const CHROMA_URL        = process.env.CHROMA_URL        ?? "http://localhost:8000";
const CHROMA_TENANT     = process.env.CHROMA_TENANT     ?? "default_tenant";
const CHROMA_DATABASE   = process.env.CHROMA_DATABASE   ?? "default_database";
const CHROMA_COLLECTION = process.env.CHROMA_COLLECTION ?? "accidents_incidents";
const CHROMA_BASE       = `${CHROMA_URL}/api/v2/tenants/${CHROMA_TENANT}/databases/${CHROMA_DATABASE}`;

async function embedQuery(query: string): Promise<number[] | null> {
  try {
    const resp = await axios.post(`${OLLAMA_URL}/api/embeddings`, {
      model: OLLAMA_MODEL,
      prompt: query,
    }, { timeout: 30000 });
    return resp.data.embedding ?? null;
  } catch (e: any) {
    console.error("Ollama embedding error:", e.message);
    return null;
  }
}

async function queryChroma(embedding: number[], nResults: number): Promise<any[]> {
  // Look up the collection ID by name first
  const collResp = await axios.get(
    `${CHROMA_BASE}/collections/${CHROMA_COLLECTION}`,
    { timeout: 10000 }
  );
  const collectionId: string = collResp.data.id;

  const queryResp = await axios.post(
    `${CHROMA_BASE}/collections/${collectionId}/query`,
    {
      query_embeddings: [embedding],
      n_results: nResults,
      include: ["documents", "metadatas", "distances"],
    },
    { timeout: 30000 }
  );

  const data = queryResp.data;
  return data.ids[0].map((id: string, i: number) => ({
    id,
    distance:  data.distances[0][i],
    document:  data.documents[0][i],
    metadata:  data.metadatas[0][i],
  }));
}

// Helper to load JSON
async function loadJsonFile(filename: string): Promise<any> {
  const filePath = path.join(DATA_DIR, filename);
  if (!existsSync(filePath)) {
    console.error(`Warning: ${filename} not found in ${DATA_DIR}`);
    return {};
  }
  try {
    const data = await fs.readFile(filePath, "utf-8");
    return JSON.parse(data);
  } catch (e) {
    console.error(`Error: Failed to decode ${filename}`);
    return {};
  }
}

// Helper to load and merge operational + human performance keyword maps
async function loadCombinedMap(): Promise<any> {
  const [opMap, hpMap] = await Promise.all([
    loadJsonFile("operational_issues_map.json"),
    loadJsonFile("human_performance_map.json"),
  ]);
  return { ...opMap, ...hpMap };
}

// ==========================================
// Tools Definition
// ==========================================

server.tool(
  "list_operational_issues",
  "Get the top-level list of operational risk categories. This is the first step in analyzing accident reports. Before attempting to search for specific terms, you must call this tool to understand the standard risk categories defined in the SKYbrary database (e.g., 'Runway Incursion', 'Loss of Separation', etc.). Do not guess category names; you must use the exact strings from this list.",
  {},
  async () => {
    const data = await loadJsonFile("operational_issues.json");
    const result = Object.entries(data).map(([name, v]: [string, any]) => ({ name, code: v.code }));
    return {
      content: [{ type: "text", text: JSON.stringify(result) }]
    };
  }
);

server.tool(
  "list_human_performance",
  "Get the top-level list of Human Performance categories from SKYbrary (e.g., 'Human Behaviour', 'Stress', 'Situational Awareness'). Call this when the analysis involves human factors, crew resource management, or cognitive/behavioural contributors to an incident. Do not guess category names; use the exact strings from this list.",
  {},
  async () => {
    const data = await loadJsonFile("human_performance.json");
    const result = Object.entries(data).map(([name, v]: [string, any]) => ({ name, code: v.code }));
    return {
      content: [{ type: "text", text: JSON.stringify(result) }]
    };
  }
);

server.tool(
  "list_keywords",
  "Get standard aviation terms (Keywords) and corresponding Slugs for a risk category. Covers both operational issue categories (from list_operational_issues) and human performance categories (from list_human_performance). Return format example: [{'name': 'Call Sign Confusion', 'slug': 'call-sign-confusion'}]. Important: 1. The `slug` field is required for get_safety_article. 2. Match the closest standard term based on the user description.",
  {
    issue_name: z.string().describe("The exact category name from list_operational_issues or list_human_performance")
  },
  async ({ issue_name }) => {
    const fullMap = await loadCombinedMap();
    const categoryData = fullMap[issue_name];

    if (!categoryData) {
      const validKeys = Object.keys(fullMap).join(", ");
      return {
        isError: true,
        content: [{ type: "text", text: `Category '${issue_name}' not found. Valid categories are: ${validKeys}` }]
      };
    }

    return {
      content: [{ type: "text", text: JSON.stringify(categoryData.keywords || []) }]
    };
  }
);

server.tool(
  "get_safety_article",
  "Get detailed safety article content from SKYbrary. Must use the `slug` string obtained from list_keywords (e.g., 'call-sign-confusion'). Also supports Operational Issue slugs. Do not use natural language names. Scrapes and parses core body content to provide factual basis.",
  {
    keyword_slug: z.string().describe("The slug of the article or issue to fetch")
  },
  async ({ keyword_slug }) => {
    const baseUrl = "https://skybrary.aero/articles/";
    const targetUrl = `${baseUrl}${keyword_slug}`;
    const headers = { "User-Agent": "MCP-Agent/1.0 (Aviation Safety Analysis Bot)" };

    const fetchCleanText = async (): Promise<string> => {
      try {
        const response = await axios.get(targetUrl, { headers, timeout: 15000 });
        
        // Capture final URL in case of redirect
        const finalUrl = response.request?.res?.responseUrl || targetUrl;
        const $ = cheerio.load(response.data);

        // Locate core content
        let articleNode = $("div.node-article");
        if (articleNode.length === 0) {
          return `Error: Could not find article content structure for slug '${keyword_slug}'.`;
        }

        let contentDiv = articleNode.find("div.group-inner");
        let contentText = "";
        if (contentDiv.length > 0) {
          contentText = contentDiv.text();
        } else {
          contentText = articleNode.text();
        }

        // Simple text cleaning
        const cleanText = contentText
          .split("\n")
          .map(line => line.trim())
          .filter(line => line.length > 0)
          .join("\n");
        
        // Return JSON string to store both URL and content
        return JSON.stringify({ url: finalUrl, text: cleanText });

      } catch (error: any) {
        if (axios.isAxiosError(error) && error.response?.status === 404) {
          return `Error: Article not found (404) for slug '${keyword_slug}'.`;
        }
        throw error; // Trigger cache fallback
      }
    };

    try {
      const cachedData = await cache.getContent(keyword_slug, fetchCleanText);
      
      let cleanText = cachedData;
      let sourceUrl = targetUrl;

      // Attempt to parse as JSON (new format with URL), fallback to plain text (old format)
      try {
        const parsed = JSON.parse(cachedData);
        if (parsed.url && parsed.text) {
          cleanText = parsed.text;
          sourceUrl = parsed.url;
        }
      } catch (e) {
        // Legacy cache format (just text)
      }

      return {
        content: [{ type: "text", text: `--- Article: ${keyword_slug} ---\nSource: ${sourceUrl}\n\n${cleanText}` }]
      };
    } catch (e: any) {
      return {
        isError: true,
        content: [{ type: "text", text: `Error fetching article: ${e.message}` }]
      };
    }
  }
);

server.tool(
  "get_accident_analysis_template",
  "Get the structured analysis template and step-by-step workflow for analyzing an accident/incident report. Call this as the FIRST step whenever the user provides a report for analysis — before calling any other tool. The template instructs you how to use list_operational_issues, list_human_performance, and list_keywords to classify the event and identify contributing factors directly from the report text.",
  {},
  async () => {
    const template = `## Analysis Workflow

Follow these steps in order to complete the analysis:

STEP 1 — CLASSIFY EVENT TYPES & TAGS
  - Call list_operational_issues to get all operational risk category names.
  - Call list_human_performance to get all human factor category names.
  - Read the report's "Event Type" field (codes like CFIT, LOC, HF, FIRE, WX, RE, etc.)
    and map each code to the closest category name from those two lists.
  - Extract the Tags listed under each event type code directly from the report.

STEP 2 — IDENTIFY CONTRIBUTING KEYWORDS
  - For each category identified in Step 1, call list_keywords('<category name>').
  - From the returned keyword list, select all keywords whose name matches
    evidence found in the report's Investigation, Findings, or Causal Factors sections.
  - If you need the full definition of a keyword, call get_safety_article('<slug>').

STEP 3 — EXTRACT REPORT DETAILS
  - Date/Time and Location: from the report header or Description section.
  - Aircraft details (type, registration, operator): from the Aircraft Involved section.

---

## Event Overview
  - Date/Time: [extract from report]
  - Location: [airport or area name, country]
  - Phase of Flight: [e.g. Approach / Cruise / Takeoff / Landing]

## Aircraft Involved  (repeat block for each aircraft)
  - Type: [manufacturer + model]
  - Registration: [e.g. G-CRST]
  - Operator: [airline or operator name]
  - Type of Flight: [Commercial Air Transport / Private / Training / etc.]
  - Origin → Destination: [departure airport → intended destination]

## Event Types & Tags
  (Map each event type code from the report to its category; copy tags verbatim)
  - [CODE] <Category Name> — Tags: [tag 1], [tag 2], ...

## Contributory Factor Analysis
  (Repeat block for each event type identified above)

  [CODE] <Category Name>
    Report tags: [from Event Types & Tags above]
    Analysis: [cite evidence from the report's Investigation/Findings that explains
               how these tags contributed to the event]
    Applicable keywords (from list_keywords('<Category Name>'), select all that apply;
                         call get_safety_article('<slug>') for full definitions):
      [ ] <keyword name> (slug: <slug>)

## Safety Recommendations
  (Generate actionable recommendations based on the contributing factors and keywords
   identified above. Each recommendation should address a specific finding from the report.)
  - [Target audience, e.g. Operators / Regulators / ATC]: [recommendation text]

## Narrative Summary
  [2–4 sentence summary covering what happened, why, and the outcome]`;

    return {
      content: [{ type: "text", text: template }]
    };
  }
);

server.tool(
  "search_accidents",
  "Semantically search the RAG index of ~1500 SKYbrary accident/incident reports. Returns the most relevant report excerpts for a given query. Use this to find real accident cases related to specific event types, contributing factors, aircraft types, or operational scenarios.",
  {
    query: z.string().describe("Natural language search query, e.g. 'CFIT approach in low visibility' or 'runway excursion after landing gear failure'"),
    n_results: z.number().int().min(1).max(20).optional().describe("Number of results to return (default 5)")
  },
  async ({ query, n_results = 5 }) => {
    const embedding = await embedQuery(query);
    if (!embedding) {
      return { isError: true, content: [{ type: "text", text: "Failed to embed query via Ollama." }] };
    }

    // Fetch more candidates than needed so deduplication still yields n_results unique reports
    let raw: any[];
    try {
      raw = await queryChroma(embedding, n_results * 4);
    } catch (e: any) {
      return { isError: true, content: [{ type: "text", text: `ChromaDB search failed: ${e.message}` }] };
    }

    // Deduplicate by slug — keep the highest-similarity (lowest distance) chunk per report
    const seen = new Map<string, any>();
    for (const r of raw) {
      const slug = r.metadata?.slug ?? r.id;
      if (!seen.has(slug) || r.distance < seen.get(slug).distance) {
        seen.set(slug, r);
      }
    }
    const results = Array.from(seen.values()).slice(0, n_results);

    if (results.length === 0) {
      return { content: [{ type: "text", text: "No matching accident reports found." }] };
    }

    const lines: string[] = [`Found ${results.length} matching accident reports:\n`];
    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      const meta = r.metadata ?? {};
      const slug = meta.slug ?? r.id;
      const similarity = (1 - (r.distance ?? 1)).toFixed(3);
      lines.push(`${i + 1}. [similarity: ${similarity}] ${meta.title ?? r.id}`);
      lines.push(`   Slug: ${slug}`);
      lines.push(`   URL: ${meta.url ?? "N/A"}`);
      lines.push(`   Date: ${meta.date ?? "N/A"} | Location: ${meta.location ?? "N/A"}`);
      lines.push(`   Event Types: ${meta.event_types ?? "N/A"}`);
      lines.push(`   Aircraft: ${meta.aircraft_types ?? "N/A"}`);
      if (r.document) {
        const snippet = (r.document as string).slice(0, 300).replace(/\n/g, " ").trim();
        lines.push(`   Excerpt: ${snippet}...`);
      }
      lines.push("");
    }

    return { content: [{ type: "text", text: lines.join("\n") }] };
  }
);

server.tool(
  "get_accident_report",
  "Retrieve the full pre-processed text of an accident/incident report stored in the RAG index, by slug. Use this after search_accidents to read the complete sections (Description, Investigation, Findings, Safety Recommendations, etc.) for a specific report without truncation. The slug is shown in search_accidents results (e.g. 'a321-en-route-near-pamplona-spain-2014').",
  {
    slug: z.string().describe("The accident/incident slug from search_accidents results")
  },
  async ({ slug }) => {
    let collectionId: string;
    try {
      const collResp = await axios.get(
        `${CHROMA_BASE}/collections/${CHROMA_COLLECTION}`,
        { timeout: 10000 }
      );
      collectionId = collResp.data.id;
    } catch (e: any) {
      return { isError: true, content: [{ type: "text", text: `ChromaDB error: ${e.message}` }] };
    }

    let chunks: any[];
    try {
      const resp = await axios.post(
        `${CHROMA_BASE}/collections/${collectionId}/get`,
        {
          where: { slug: { $eq: slug } },
          include: ["documents", "metadatas"],
        },
        { timeout: 30000 }
      );
      const data = resp.data;
      chunks = data.ids.map((id: string, i: number) => ({
        id,
        document: data.documents[i],
        metadata: data.metadatas[i],
      }));
    } catch (e: any) {
      return { isError: true, content: [{ type: "text", text: `ChromaDB query failed: ${e.message}` }] };
    }

    if (chunks.length === 0) {
      return { content: [{ type: "text", text: `No report found for slug '${slug}'. Use search_accidents to find valid slugs.` }] };
    }

    // Put metadata chunk first, then sections in alphabetical order
    const metaChunk = chunks.find(c => c.metadata?.section === "metadata");
    const sectionChunks = chunks
      .filter(c => c.metadata?.section !== "metadata")
      .sort((a, b) => (a.id as string).localeCompare(b.id as string));

    const lines: string[] = [];
    if (metaChunk) {
      lines.push(metaChunk.document);
      lines.push("\n---\n");
    }
    for (const chunk of sectionChunks) {
      lines.push(chunk.document);
      lines.push("\n");
    }

    return { content: [{ type: "text", text: lines.join("\n") }] };
  }
);

// Start Server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
