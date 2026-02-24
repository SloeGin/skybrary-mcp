"""
Scrapes the full list of accident & incident reports from SKYbrary.

URL pattern : https://skybrary.aero/accidents-and-incidents?page=N
Requires login (Drupal form auth).

Credentials are read from environment variables:
    SKYBRARY_USER - your SKYbrary username / e-mail
    SKYBRARY_PASS - your SKYbrary password

Output: data/accidents_incidents.json
    [
      {"title": "A109 Vicinity London Heliport ...", "slug": "a109-vicinity-london-heliport-london-uk-2013"},
      ...
    ]

Run:
    export SKYBRARY_USER="you@example.com"
    read -s SKYBRARY_PASS
    export SKYBRARY_PASS
    python scripts/populate_accidents_incidents.py

Use --resume to skip pages that have already been written to the output file.
"""

import asyncio
import json
import os
import re
import sys
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "accidents_incidents.json"

BASE_URL = "https://skybrary.aero"
LIST_PATH = "/accidents-and-incidents"
LOGIN_PATH = "/user/login"

HEADERS = {
    "User-Agent": "MCP-Scraper/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}

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
# Login
# ---------------------------------------------------------------------------

async def login(client: httpx.AsyncClient, username: str, password: str) -> bool:
    """Performs Drupal form-based login. Returns True on success."""
    login_url = f"{BASE_URL}{LOGIN_PATH}"

    # Step 1 - fetch the login form to get the CSRF / form tokens.
    print(f"Fetching login page: {login_url}")
    try:
        resp = await client.get(login_url, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Error fetching login page: {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", id=re.compile(r"user.login", re.I))
    if not form:
        # Fall back to any form on the page that has a 'name' field
        form = soup.find("form")

    if not form:
        print("  Could not find login form on page.")
        return False

    # Collect all hidden inputs (Drupal uses form_build_id, form_id, op, etc.)
    payload: dict[str, str] = {}
    for hidden in form.find_all("input", type="hidden"):
        name = hidden.get("name")
        value = hidden.get("value", "")
        if name:
            payload[name] = value

    # Also grab the submit button value if present
    submit = form.find("input", type="submit")
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Log in")

    payload["name"] = username
    payload["pass"] = password

    # Step 2 - POST credentials.
    action = form.get("action") or login_url
    if action.startswith("/"):
        action = f"{BASE_URL}{action}"

    print(f"  Posting credentials to {action} ...")
    try:
        post_resp = await client.post(action, data=payload, follow_redirects=True)
    except Exception as e:
        print(f"  Error posting login form: {e}")
        return False

    # Drupal redirects to /user/<uid> or the destination on success.
    # A failure usually stays on /user/login.
    final_url = str(post_resp.url)
    if LOGIN_PATH in final_url:
        # Check for error messages in page
        error_soup = BeautifulSoup(post_resp.text, "html.parser")
        msg = error_soup.find(class_=re.compile(r"error|messages--error", re.I))
        hint = msg.get_text(strip=True) if msg else "(no error message found)"
        print(f"  Login appears to have failed. Hint: {hint}")
        return False

    print(f"  Logged in successfully (redirected to {final_url})")
    return True

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
    username = os.environ.get("SKYBRARY_USER", "").strip()
    password = os.environ.get("SKYBRARY_PASS", "").strip()

    if not username or not password:
        print(
            "ERROR: Set SKYBRARY_USER and SKYBRARY_PASS environment variables before running.\n"
            "  Example:\n"
            "    export SKYBRARY_USER='you@example.com'\n"
            "    read -s SKYBRARY_PASS && export SKYBRARY_PASS\n"
            "    python scripts/populate_accidents_incidents.py"
        )
        sys.exit(1)

    resume = "--resume" in sys.argv

    # Load existing data when resuming
    existing: list[dict[str, str]] = []
    existing_slugs: set[str] = set()
    if resume and OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        existing_slugs = {entry["slug"] for entry in existing}
        print(f"Resuming: {len(existing)} entries already in {OUTPUT_FILE.name}")

    all_results: list[dict[str, str]] = list(existing)

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        # --- Authenticate ---
        ok = await login(client, username, password)
        if not ok:
            print("Aborting: could not log in.")
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
                resp = await client.get(first_url, follow_redirects=True)
                resp.raise_for_status()
            except Exception as e:
                print(f"Error fetching first page: {e}")
                sys.exit(1)

            html = resp.text
            total, range_start, range_end = parse_result_counts(html)

            if total == 0:
                print("WARNING: Could not determine total result count from page text.")
                print("  Make sure login succeeded and the page is accessible.")
                total = 9999
                range_end = 0
            else:
                page_size = range_end - range_start + 1
                num_pages = total_pages(total, page_size)
                print(f"Found {total} results across {num_pages} pages (page size: {page_size})")

            entries = extract_incidents(html)
            new_entries = [e for e in entries if e["slug"] not in existing_slugs]
            all_results.extend(new_entries)
            existing_slugs.update(e["slug"] for e in new_entries)
            print(f"  Page 0: {len(entries)} entries ({len(new_entries)} new)")

            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)

        # --- Remaining pages ---
        page = start_page if start_page > 0 else 1
        while range_end < total:
            await asyncio.sleep(5)

            page_url = f"{BASE_URL}{LIST_PATH}?page={page}"
            print(f"Fetching page {page}: {page_url}")
            try:
                resp = await client.get(page_url, follow_redirects=True)
                if resp.status_code == 429:
                    print("  Rate limited (429) — sleeping 30 s then retrying...")
                    await asyncio.sleep(30)
                    resp = await client.get(page_url, follow_redirects=True)
                resp.raise_for_status()
            except Exception as e:
                print(f"  Error fetching {page_url}: {e}")
                break

            html = resp.text
            _, _, range_end = parse_result_counts(html)

            entries = extract_incidents(html)
            if not entries:
                print(f"  Page {page}: no entries found — stopping.")
                break

            new_entries = [e for e in entries if e["slug"] not in existing_slugs]
            all_results.extend(new_entries)
            existing_slugs.update(e["slug"] for e in new_entries)
            print(f"  Page {page}: {len(entries)} entries ({len(new_entries)} new), range end now #{range_end}/{total}")

            # Incremental save
            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)

            if range_end >= total:
                break

            page += 1

    print(f"\nDone. {len(all_results)} total entries written to {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
