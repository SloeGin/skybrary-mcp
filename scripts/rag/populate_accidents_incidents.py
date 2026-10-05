"""
Scrapes the full list of accident & incident reports from SKYbrary.

URL pattern : https://skybrary.aero/accidents-and-incidents?page=N
Uses an authenticated browser session because SKYbrary requires a JavaScript
browser check and complete data is account-protected.

Output: data/accidents_incidents.json
    [
      {"title": "A109 Vicinity London Heliport ...", "slug": "a109-vicinity-london-heliport-london-uk-2013"},
      ...
    ]

Run:
    python scripts/rag/populate_accidents_incidents.py

Use --resume to skip pages that have already been written to the output file.
Use --limit N to stop after N entries (useful for a small evaluation set).
"""

import asyncio
import json
import os
import re
import sys
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from skybrary_browser import SkybraryBrowser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "accidents_incidents.json"

load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = os.environ.get("SKYBRARY_BASE_URL", "https://skybrary.aero").rstrip("/")
LIST_PATH = "/accidents-and-incidents"
REQUEST_TIMEOUT = float(os.environ.get("SCRAPER_REQUEST_TIMEOUT_SECONDS", "30"))
SLEEP_BETWEEN = float(os.environ.get("SCRAPER_DELAY_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Pagination helper  (shared logic with populate_operational_issues_map.py)
# ---------------------------------------------------------------------------

def parse_result_counts(html: str) -> tuple[int, int, int]:
    """Parses 'Showing below X results in range #Y to #Z'.
    Returns (total, range_start, range_end). Returns (0, 0, 0) if not found.
    """
    match = re.search(r"Showing below (\d+) results in range #(\d+) to #(\d+)", html)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return 0, 0, 0


def total_pages(total: int, page_size: int) -> int:
    """Number of pages needed to cover all results (0-indexed)."""
    return ceil(total / page_size) if page_size else 1

# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def extract_incidents(html: str) -> list[dict[str, str]]:
    """Extracts incident report entries from a single listing page.

    Expected HTML structure:
        <div class="view-content">
            <div class="views-row">
                <a href="/accidents-and-incidents/some-slug">Report Title</a>
                ...
            </div>
            ...
        </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []

    view_content = soup.find("div", class_="view-content")
    if not view_content:
        return results

    for row in view_content.find_all("div", class_="views-row"):
        link = row.find("a")
        if not link:
            continue

        title = link.get_text(strip=True)
        href = link.get("href", "")

        if not title or not href:
            continue

        # Slug is the last path segment, e.g.
        # /accidents-and-incidents/a109-vicinity-london-heliport-london-uk-2013
        #  → a109-vicinity-london-heliport-london-uk-2013
        path = urlparse(href).path
        slug = path.strip("/").split("/")[-1]

        if slug:
            results.append({"title": title, "slug": slug})

    return results

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    resume = "--resume" in sys.argv
    limit: int | None = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        if idx + 1 >= len(sys.argv):
            raise SystemExit("--limit requires an integer")
        limit = int(sys.argv[idx + 1])
        if limit < 1:
            raise SystemExit("--limit must be at least 1")

    # Load existing data when resuming
    existing: list[dict[str, str]] = []
    existing_slugs: set[str] = set()
    if resume and OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        existing_slugs = {entry["slug"] for entry in existing}
        print(f"Resuming: {len(existing)} entries already in {OUTPUT_FILE.name}")

    all_results: list[dict[str, str]] = list(existing)

    async with SkybraryBrowser(REQUEST_TIMEOUT) as browser:
        try:
            await browser.login_from_env(required=True)
        except Exception as e:
            print(f"Aborting: could not establish an authenticated SKYbrary session: {e}")
            sys.exit(1)

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

        # --- Page 0: determine total count and page size ---
        # When resuming, skip pages already covered by existing entries.
        page_size = 100  # default; updated once we read the first fetched page
        start_page = (len(existing) // page_size) if resume and existing else 0

        if start_page > 0:
            print(f"\nResume: {len(existing)} existing entries → skipping to page {start_page}")
            # We still need total/range_end to drive the loop; fetch the start page
            # rather than page 0 so we don't waste a request on already-done data.
            total = 9999   # will be updated on first fetch
            range_end = (start_page * page_size) - 1  # pages before start_page are done
        else:
            # Fetch page 0 normally
            first_url = f"{BASE_URL}{LIST_PATH}?page=0"
            print(f"\nFetching first page: {first_url}")
            try:
                html = await browser.fetch_authenticated(first_url)
            except Exception as e:
                print(f"Error fetching first page: {e}")
                sys.exit(1)

            total, range_start, range_end = parse_result_counts(html)

            if total == 0:
                print("WARNING: Could not determine total result count from page text.")
                print("  Make sure the browser check succeeded and the page is accessible.")
                total = 9999
                range_end = 0
            else:
                page_size = range_end - range_start + 1
                num_pages = total_pages(total, page_size)
                print(f"Found {total} results across {num_pages} pages (page size: {page_size})")

            entries = extract_incidents(html)
            new_entries = [e for e in entries if e["slug"] not in existing_slugs]
            all_results.extend(new_entries)
            if limit:
                all_results = all_results[:limit]
            existing_slugs.update(e["slug"] for e in new_entries)
            print(f"  Page 0: {len(entries)} entries ({len(new_entries)} new)")

            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)

            if limit and len(all_results) >= limit:
                print(f"\nDone. {len(all_results)} entries written to {OUTPUT_FILE} (--limit reached)")
                return

        # --- Remaining pages ---
        page = start_page if start_page > 0 else 1
        while range_end < total:
            await asyncio.sleep(SLEEP_BETWEEN)

            page_url = f"{BASE_URL}{LIST_PATH}?page={page}"
            print(f"Fetching page {page}: {page_url}")
            try:
                html = await browser.fetch_authenticated(page_url)
            except Exception as e:
                print(f"  Error fetching {page_url}: {e}")
                break

            _, _, range_end = parse_result_counts(html)

            entries = extract_incidents(html)
            if not entries:
                print(f"  Page {page}: no entries found — stopping.")
                break

            new_entries = [e for e in entries if e["slug"] not in existing_slugs]
            all_results.extend(new_entries)
            if limit:
                all_results = all_results[:limit]
            existing_slugs.update(e["slug"] for e in new_entries)
            print(f"  Page {page}: {len(entries)} entries ({len(new_entries)} new), range end now #{range_end}/{total}")

            # Incremental save
            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)

            if limit and len(all_results) >= limit:
                break

            if range_end >= total:
                break

            page += 1

    print(f"\nDone. {len(all_results)} total entries written to {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
