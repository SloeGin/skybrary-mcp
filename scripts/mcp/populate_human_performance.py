"""
Scrapes SKYbrary's Human Performance section and produces two data files:

  data/human_performance.json       — category list (mirrors operational_issues.json)
    {
      "Human Behaviour": {"description": "...", "slug": "human-behaviour"},
      ...
    }

  data/human_performance_map.json   — keyword map (mirrors operational_issues_map.json)
    {
      "Human Behaviour": {
        "code": "HP",
        "keywords": [{"name": "...", "slug": "..."}, ...]
      },
      ...
    }

Does NOT require login (human-performance pages are publicly accessible).

Run:
    python scripts/populate_human_performance.py

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
DATA_DIR = Path(__file__).parent.parent.parent / "data"
HP_ISSUES_FILE  = DATA_DIR / "human_performance.json"
HP_MAP_FILE     = DATA_DIR / "human_performance_map.json"

BASE_URL = "https://skybrary.aero"
HP_INDEX = "/human-performance"

HEADERS = {"User-Agent": "MCP-Scraper/1.0", "Accept-Language": "en-US,en;q=0.9"}
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
# Step 1 - discover HP categories from the index page
# ---------------------------------------------------------------------------

def extract_categories(html: str) -> list[dict[str, str]]:
    """Extracts HP category entries from the /human-performance index page.

    Structure on the page:
        <div class="view-content">
          <div class="masonry-item views-row">
            ...
            <h3><a href="/human-performance/human-behaviour">Human Behaviour</a></h3>
          </div>
          ...
        </div>

    Returns list of {"name": ..., "slug": ...}.
    """
    soup = BeautifulSoup(html, "html.parser")
    categories: list[dict[str, str]] = []
    seen: set[str] = set()

    view_content = soup.find("div", class_="view-content")
    if not view_content:
        return categories

    for row in view_content.find_all("div", class_=re.compile(r"masonry-item|views-row")):
        # The link sits inside an <h3> within each row
        heading = row.find("h3")
        link = heading.find("a") if heading else None
        if not link:
            continue

        name = link.get_text(strip=True)
        href = link.get("href", "")
        if not name or not href:
            continue

        path = urlparse(href).path          # e.g. /human-performance/human-behaviour
        slug = path.strip("/").split("/")[-1]  # → human-behaviour

        # Only keep URLs that are sub-pages of /human-performance
        if HP_INDEX not in path or slug == "human-performance":
            continue

        if slug not in seen:
            categories.append({"name": name, "slug": slug})
            seen.add(slug)

    return categories


async def discover_categories(client: httpx.AsyncClient) -> list[dict[str, str]]:
    """Fetches the HP index page and returns the list of category dicts."""
    print(f"Fetching HP index: {BASE_URL}{HP_INDEX}")
    html = await fetch(client, f"{BASE_URL}{HP_INDEX}")
    if not html:
        print("ERROR: Could not fetch the human-performance index page.")
        sys.exit(1)

    cats = extract_categories(html)
    print(f"  Discovered {len(cats)} categories.")
    return cats

# ---------------------------------------------------------------------------
# Step 2 - scrape description from a category page
# ---------------------------------------------------------------------------

def extract_description(html: str) -> str:
    """Extracts the description text from an HP category page.
    Looks for a 'Description' heading and collects the following paragraphs,
    the same heuristic used in populate_operational_issues.py.
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

    # Fallback: grab the first non-empty paragraph in the article body
    for p in article.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) > 40:
            return text

    return ""

# ---------------------------------------------------------------------------
# Step 3 - scrape keywords (articles) from a category page (with pagination)
# ---------------------------------------------------------------------------

def extract_keywords(html: str) -> list[dict[str, str]]:
    """Extracts article keyword entries from a category listing page.
    Mirrors extract_keywords() in populate_operational_issues_map.py.
    """
    soup = BeautifulSoup(html, "html.parser")
    keywords: list[dict[str, str]] = []

    view_contents = soup.find_all("div", class_="view-content")
    if not view_contents:
        return keywords

    for vc in view_contents:
        for item in vc.find_all("div", class_=re.compile(r"masonry-item|views-row")):
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


async def scrape_category(
    client: httpx.AsyncClient,
    category_slug: str,
) -> tuple[str, list[dict[str, str]]]:
    """Fetches all pages for a category.
    Returns (description, keywords_list).
    """
    base_url = f"{BASE_URL}{HP_INDEX}/{category_slug}"
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

        # Extract description only from the first page
        if page == 0:
            description = extract_description(html)

        kws = extract_keywords(html)
        all_keywords.extend(kws)

        total_found, _, range_end_found = parse_result_counts(html)
        if total_found == 0:
            # No pagination text — single page
            break
        total    = total_found
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
    resume = "--resume" in sys.argv

    # Load existing outputs if resuming
    existing_issues: dict = {}
    existing_map: dict = {}
    if resume:
        if HP_ISSUES_FILE.exists():
            with open(HP_ISSUES_FILE) as f:
                existing_issues = json.load(f)
            print(f"Resuming: {len(existing_issues)} categories already in {HP_ISSUES_FILE.name}")
        if HP_MAP_FILE.exists():
            with open(HP_MAP_FILE) as f:
                existing_map = json.load(f)

    issues_data: dict = dict(existing_issues)
    map_data: dict    = dict(existing_map)

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        # ---- Discover categories ----
        categories = await discover_categories(client)

        if not categories:
            print("No categories found on the HP index page.")
            print("The page structure may differ from expected — check the HTML manually.")
            sys.exit(1)

        # ---- Process each category ----
        for idx, cat in enumerate(categories, 1):
            name = cat["name"]
            slug = cat["slug"]

            # When resuming, skip categories whose keywords are already populated
            if resume and name in existing_map and existing_map[name].get("keywords"):
                print(f"[{idx}/{len(categories)}] Skipping (already done): {name}")
                continue

            print(f"[{idx}/{len(categories)}] Processing: {name}  (slug: {slug})")

            description, keywords = await scrape_category(client, slug)

            print(f"  → {len(keywords)} keywords, description: {len(description)} chars")

            # Update issues file entry
            issues_data[name] = {"description": description, "slug": slug}

            # Update map entry (code is always "HP" for all human-performance categories)
            map_data[name] = {"code": "HP", "keywords": keywords}

            # Incremental save after each category
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(HP_ISSUES_FILE, "w") as f:
                json.dump(issues_data, f, indent=2, ensure_ascii=False)
            with open(HP_MAP_FILE, "w") as f:
                json.dump(map_data, f, indent=2, ensure_ascii=False)

            if idx < len(categories):
                print(f"  Sleeping {SLEEP_BETWEEN} s...")
                await asyncio.sleep(SLEEP_BETWEEN)

    print(f"\nDone.")
    print(f"  {HP_ISSUES_FILE}: {len(issues_data)} categories")
    print(f"  {HP_MAP_FILE}:    {len(map_data)} categories")


if __name__ == "__main__":
    asyncio.run(main())
