"""Build official accident slug -> Event Type code mappings from SKYbrary filters."""

import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from skybrary_browser import SkybraryBrowser

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "accident_event_types.json"
PROGRESS_FILE = DATA_DIR / "accident_event_types_progress.json"

load_dotenv(PROJECT_ROOT / ".env")
BASE_URL = os.environ.get("SKYBRARY_BASE_URL", "https://skybrary.aero").rstrip("/")
TIMEOUT = float(os.environ.get("SCRAPER_REQUEST_TIMEOUT_SECONDS", "30"))
DELAY = float(os.environ.get("SCRAPER_DELAY_SECONDS", "5"))
ITEMS_PER_PAGE = 500


def with_query(url: str, **updates: str | int) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))
    query = parse_qs(parsed.query)
    query.update({key: [str(value)] for key, value in updates.items()})
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def extract_slugs(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    slugs = set()
    for link in soup.select('a[href*="/accidents-and-incidents/"]'):
        path = urlparse(link.get("href", "")).path.strip("/").split("/")
        if len(path) == 2 and path[0] == "accidents-and-incidents":
            slugs.add(path[1])
    return slugs


async def main() -> None:
    requested_codes: set[str] | None = None
    if "--codes" in sys.argv:
        index = sys.argv.index("--codes")
        requested_codes = {code.upper() for code in sys.argv[index + 1:]}
        if not requested_codes:
            raise SystemExit("--codes requires at least one Event Type code")

    mapping = {
        slug: set(codes)
        for slug, codes in (
            json.loads(OUTPUT_FILE.read_text()).items() if OUTPUT_FILE.exists() else []
        )
    }
    completed = set(json.loads(PROGRESS_FILE.read_text())) if PROGRESS_FILE.exists() else set()

    async with SkybraryBrowser(TIMEOUT) as browser:
        index_html = await browser.fetch(f"{BASE_URL}/accidents-and-incidents")
        soup = BeautifulSoup(index_html, "html.parser")
        filters: dict[str, str] = {}
        for link in soup.select('#collapse-eventtype-content a[href*="event_type"]'):
            code = re.sub(r"\s*\(\d+\)\s*$", "", link.get_text(" ", strip=True))
            if code:
                filters[code] = link.get("href", "")

        selected = [
            (code, url)
            for code, url in sorted(filters.items())
            if (requested_codes is None or code in requested_codes) and code not in completed
        ]
        for index, (code, filter_url) in enumerate(selected):
            if index:
                await asyncio.sleep(DELAY)
            first_url = with_query(filter_url, items_per_page=ITEMS_PER_PAGE, page=0)
            html = await browser.fetch(first_url)
            count_match = re.search(r"Showing below (\d+) results", html)
            total = int(count_match.group(1)) if count_match else 0
            pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
            slugs = extract_slugs(html)
            for page in range(1, pages):
                await asyncio.sleep(DELAY)
                page_html = await browser.fetch(
                    with_query(filter_url, items_per_page=ITEMS_PER_PAGE, page=page)
                )
                slugs.update(extract_slugs(page_html))
            for slug in slugs:
                mapping.setdefault(slug, set()).add(code)
            print(f"{code}: {len(slugs)} reports")
            completed.add(code)
            serializable = {slug: sorted(codes) for slug, codes in sorted(mapping.items())}
            OUTPUT_FILE.write_text(json.dumps(serializable, indent=2) + "\n")
            PROGRESS_FILE.write_text(json.dumps(sorted(completed), indent=2) + "\n")

    serializable = {slug: sorted(codes) for slug, codes in sorted(mapping.items())}
    OUTPUT_FILE.write_text(json.dumps(serializable, indent=2) + "\n")
    print(f"Wrote {len(serializable)} labeled reports ({len(completed)} categories) to {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
