"""
Scrapes SKYbrary's Operational Issues section and produces two data files:

  data/operational_issues.json       — category list with descriptions
    {
      "Loss of Control": {"description": "...", "slug": "loss-control"},
      ...
    }

  data/operational_issues_map.json   — keyword map per category
    {
      "Loss of Control": {
        "code": "LOC",
        "keywords": [{"name": "...", "slug": "..."}, ...]
      },
      ...
    }

Categories are read from the existing operational_issues.json (slugs must already
be present). Codes in operational_issues_map.json are preserved across runs.

Run:
    python scripts/populate_operational_issues.py

Use --resume to skip categories whose keywords have already been written.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
DATA_DIR    = Path(__file__).parent.parent.parent / "data"
ISSUES_FILE = DATA_DIR / "operational_issues.json"
MAP_FILE    = DATA_DIR / "operational_issues_map.json"

BASE_URL = "https://skybrary.aero"

HEADERS       = {"User-Agent": "MCP-Scraper/1.0", "Accept-Language": "en-US,en;q=0.9"}
SLEEP_BETWEEN = 5   # seconds between normal requests
SLEEP_429     = 30  # seconds to wait after a 429 response

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_result_counts(html: str) -> tuple[int, int, int]:
    """Parses 'Showing below X results in range #Y to #Z'.
    Returns (total, range_start, range_end). Returns (0, 0, 0) if not found.
    """
    match = re.search(r"Showing below (\d+) results in range #(\d+) to #(\d+)", html)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return 0, 0, 0


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    """GET a URL, handling 429 with a single retry after SLEEP_429 seconds.
    Returns the response text, or None on unrecoverable error.
    """
    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code == 429:
            print(f"  Rate limited (429) — sleeping {SLEEP_429} s then retrying {url}")
            await asyncio.sleep(SLEEP_429)
            resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Step 1 - resolve the canonical URL for a category
# ---------------------------------------------------------------------------

async def resolve_category_url(client: httpx.AsyncClient, slug: str) -> str | None:
    """Returns the first reachable URL for the given slug, trying
    /operational-issues/{slug} before /articles/{slug}.
    """
    candidates = [
        f"{BASE_URL}/operational-issues/{slug}",
        f"{BASE_URL}/articles/{slug}",
    ]
    for url in candidates:
        try:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                # Return the final URL after any redirects, strip query string
                return str(resp.url).split("?")[0]
        except Exception as e:
            print(f"  Error checking {url}: {e}")
    return None

# ---------------------------------------------------------------------------
# Step 2 - extract description from the first page of a category
# ---------------------------------------------------------------------------

def extract_description(html: str) -> str:
    """Extracts description text by looking for a 'Description' heading
    and collecting the following paragraphs.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("div", class_="node-article") or soup.find("article") or soup

    for header in article.find_all(["h2", "h3"]):
        if "description" in header.get_text(strip=True).lower():
            parts = []
            curr = header.find_next_sibling()
            while curr and curr.name not in ["h2", "h3"]:
                if curr.name == "p":
                    parts.append(curr.get_text(strip=True))
                curr = curr.find_next_sibling()
            if parts:
                return " ".join(parts)

    return ""

# ---------------------------------------------------------------------------
# Step 3 - extract keywords from a category listing page
# ---------------------------------------------------------------------------

def extract_keywords(html: str) -> list[dict[str, str]]:
    """Extracts article keyword entries from a category listing page."""
    soup = BeautifulSoup(html, "html.parser")
    keywords: list[dict[str, str]] = []

    view_contents = soup.find_all("div", class_="view-content")
    if not view_contents:
        return keywords

    for vc in view_contents:
        for item in vc.find_all("div", class_="masonry-item"):
            for link in item.find_all("a"):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                if not href or not text:
                    continue
                path = urlparse(href).path
                slug = path.strip("/").split("/")[-1]
                if slug and not any(k["slug"] == slug for k in keywords):
                    keywords.append({"name": text, "slug": slug})

    return keywords

# ---------------------------------------------------------------------------
# Step 4 - scrape description + all keyword pages for one category
# ---------------------------------------------------------------------------

async def scrape_category(
    client: httpx.AsyncClient,
    base_url: str,
) -> tuple[str, list[dict[str, str]]]:
    """Fetches all pages for a category.
    Returns (description, keywords_list).
    """
    description = ""
    all_keywords: list[dict[str, str]] = []

    page = 0
    range_end = 0
    total = 1  # will be updated; start > 0 so loop runs at least once

    while range_end < total:
        url = f"{base_url}?page={page}" if page > 0 else base_url
        print(f"    Fetching {url}")
        html = await fetch(client, url)
        if not html:
            break

        if page == 0:
            description = extract_description(html)

        kws = extract_keywords(html)
        all_keywords.extend(kws)

        total_found, _, range_end_found = parse_result_counts(html)
        if total_found == 0:
            break  # single page, no pagination text
        total     = total_found
        range_end = range_end_found

        if range_end >= total:
            break

        page += 1
        await asyncio.sleep(SLEEP_BETWEEN)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for k in all_keywords:
        if k["slug"] not in seen:
            unique.append(k)
            seen.add(k["slug"])

    return description, unique

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    if not ISSUES_FILE.exists():
        print(f"ERROR: {ISSUES_FILE} not found.")
        sys.exit(1)

    resume = "--resume" in sys.argv

    with open(ISSUES_FILE) as f:
        issues_data: dict = json.load(f)

    # Load existing map to preserve per-category codes
    existing_map: dict = {}
    if MAP_FILE.exists():
        with open(MAP_FILE) as f:
            existing_map = json.load(f)

    if resume:
        done = sum(1 for v in existing_map.values() if v.get("keywords"))
        print(f"Resuming: {done}/{len(issues_data)} categories already done")

    map_data: dict = dict(existing_map)

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        categories = list(issues_data.items())

        for idx, (name, info) in enumerate(categories, 1):
            # When resuming, skip categories whose keywords are already populated
            if resume and name in existing_map and existing_map[name].get("keywords"):
                print(f"[{idx}/{len(categories)}] Skipping (already done): {name}")
                continue

            slug = info.get("slug") if isinstance(info, dict) else name.lower().replace(" ", "-")
            print(f"[{idx}/{len(categories)}] Processing: {name}  (slug: {slug})")

            base_url = await resolve_category_url(client, slug)
            if not base_url:
                print(f"  Could not resolve URL for '{name}' — skipping.")
                continue

            description, keywords = await scrape_category(client, base_url)
            print(f"  → {len(keywords)} keywords, description: {len(description)} chars")

            # Update issues file entry (preserves slug)
            issues_data[name] = {"description": description, "slug": slug}

            # Update map entry, preserving any existing code
            existing_code = existing_map.get(name, {}).get("code", "TODO")
            map_data[name] = {"code": existing_code, "keywords": keywords}

            # Incremental save after each category
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(ISSUES_FILE, "w") as f:
                json.dump(issues_data, f, indent=2, ensure_ascii=False)
            with open(MAP_FILE, "w") as f:
                json.dump(map_data, f, indent=2, ensure_ascii=False)

            if idx < len(categories):
                print(f"  Sleeping {SLEEP_BETWEEN} s...")
                await asyncio.sleep(SLEEP_BETWEEN)

    print(f"\nDone.")
    print(f"  {ISSUES_FILE}: {len(issues_data)} categories")
    print(f"  {MAP_FILE}:    {len(map_data)} categories")


if __name__ == "__main__":
    asyncio.run(main())
