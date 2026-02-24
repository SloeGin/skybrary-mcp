"""
Fetches and parses SKYbrary accident/incident articles, producing structured
JSON files ready for vector embedding.

Input : data/accidents_incidents.json  (produced by populate_accidents_incidents.py)
Output: data/rag/processed/{slug}.json  one file per article

Each output file has the shape:
{
  "slug":         "a109-vicinity-london-heliport-london-uk-2013",
  "title":        "A109, vicinity London Heliport London UK, 2013",
  "url":          "https://skybrary.aero/accidents-and-incidents/...",
  "summary":      "On 16 January 2013 ...",
  "date":         "16 January 2013",          # best-effort
  "location":     "London Heliport, UK",      # from title / body
  "event_types":  ["LOC", "FIRE"],            # taxonomy tags found on page
  "aircraft":     ["Augusta 109E (G-CRST)"],  # parsed from body / fields
  "sections": {
      "Description":           "...",
      "Investigation":         "...",
      "Causal Factors":        "...",    # or "Findings", "Probable Cause" …
      "Safety Recommendations":"...",
      ...                                # any other h2 sections present
  }
}

Credentials:
    SKYBRARY_USER - SKYbrary username / e-mail
    SKYBRARY_PASS - SKYbrary password

Run:
    export SKYBRARY_USER="you@example.com"
    read -s SKYBRARY_PASS
    export SKYBRARY_PASS
    python scripts/process_accidents.py

Use --resume to skip articles already present in data/rag/processed/.
Use --slug <slug> to process only a single article (useful for debugging).
Use --save-html to write the raw fetched HTML to /tmp/<slug>.html for inspection.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
DATA_DIR       = Path(__file__).parent.parent.parent / "data"
INCIDENTS_FILE = DATA_DIR / "accidents_incidents.json"
OUTPUT_DIR     = DATA_DIR / "rag" / "processed"

BASE_URL   = "https://skybrary.aero"
ARTICLE_PATH = "/accidents-and-incidents"
LOGIN_PATH   = "/user/login"

HEADERS = {
    "User-Agent": "MCP-Scraper/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}

SLEEP_BETWEEN = 5   # seconds between article fetches
SLEEP_429     = 30  # seconds after a 429

# ---------------------------------------------------------------------------
# Login  (identical to populate_accidents_incidents.py)
# ---------------------------------------------------------------------------

async def login(client: httpx.AsyncClient, username: str, password: str) -> bool:
    login_url = f"{BASE_URL}{LOGIN_PATH}"
    print(f"Fetching login page: {login_url}")
    try:
        resp = await client.get(login_url, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Error fetching login page: {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", id=re.compile(r"user.login", re.I)) or soup.find("form")
    if not form:
        print("  Could not find login form.")
        return False

    payload: dict[str, str] = {}
    for hidden in form.find_all("input", type="hidden"):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")

    submit = form.find("input", type="submit")
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Log in")

    payload["name"] = username
    payload["pass"] = password

    action = form.get("action") or login_url
    if action.startswith("/"):
        action = f"{BASE_URL}{action}"

    try:
        post_resp = await client.post(action, data=payload, follow_redirects=True)
    except Exception as e:
        print(f"  Error posting login form: {e}")
        return False

    final_url = str(post_resp.url)
    if LOGIN_PATH in final_url:
        err_soup = BeautifulSoup(post_resp.text, "html.parser")
        msg = err_soup.find(class_=re.compile(r"error|messages--error", re.I))
        hint = msg.get_text(strip=True) if msg else "(no error message found)"
        print(f"  Login failed. Hint: {hint}")
        return False

    print(f"  Logged in (redirected to {final_url})")
    return True

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_article(client: httpx.AsyncClient, slug: str) -> str | None:
    url = f"{BASE_URL}{ARTICLE_PATH}/{slug}"
    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code == 429:
            print(f"  Rate limited (429) — sleeping {SLEEP_429} s ...")
            await asyncio.sleep(SLEEP_429)
            resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _text(tag: Tag | None) -> str:
    """Return clean text from a BS4 tag, or empty string."""
    if not tag:
        return ""
    return " ".join(tag.get_text(" ", strip=True).split())


def _find_field(soup: BeautifulSoup, *field_name_fragments: str) -> Tag | None:
    """Find a Drupal field div by one of several field-name-* fragments."""
    for frag in field_name_fragments:
        tag = soup.find("div", class_=re.compile(rf"field-name-{re.escape(frag)}", re.I))
        if tag:
            return tag
    return None


# Aviation event-type codes are short ALL-CAPS strings (2-8 chars, optional hyphens).
# e.g. CFIT, FIRE, HF, WX, LOC-I, LOC-G, AGC, RE, RI, AW, GPWS, ACAS …
_EVENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{0,3}(?:-[A-Z0-9]{1,3})?$")


def _is_event_code(text: str) -> bool:
    return bool(_EVENT_CODE_RE.match(text.strip()))


def extract_event_types_and_tags(soup: BeautifulSoup) -> tuple[list[str], dict[str, list[str]]]:
    """
    Returns (event_codes, {code: [tag1, tag2, ...]}).

    SKYbrary Drupal structure (confirmed from live HTML):

    Event codes — all in a single field:
        <div class="field-name-field-event-type ...">
          <div class="field-items">
            <div class="field-item">
              <a href="/event-type/cfit">CFIT</a>,
              <a href="/event-type/fire">FIRE</a>, ...
            </div>
          </div>
        </div>

    Tags per code — one group per event code:
        <div class="group-cfit data-table">
          <div><span>CFIT</span></div>
          <div class="field-name-field-event-cfit ...">
            <div class="field-label">Tag(s)</div>
            <div class="field-items">
              <div class="field-item">Into obstruction, VFR flight plan</div>
            </div>
          </div>
        </div>
    """
    codes: list[str] = []
    tags_map: dict[str, list[str]] = {}

    # --- Step 1: extract ordered event codes from the event-type field ---
    event_type_field = soup.find(
        "div", class_=re.compile(r"\bfield-name-field-event-type\b")
    )
    if event_type_field:
        for a in event_type_field.find_all("a", href=re.compile(r"^/event-type/")):
            code = a.get_text(strip=True)
            if code and code not in codes:
                codes.append(code)

    # --- Step 2: extract tags from each group-{code} data-table block ---
    for group_div in soup.find_all("div", class_="data-table"):
        # The event code appears in a <span> in the first child div
        span = group_div.find("span")
        if not span:
            continue
        code = span.get_text(strip=True)
        if not _is_event_code(code):
            continue

        # Tags live in the field-item div — may be plain comma-separated text or <a> links
        field_item = group_div.find("div", class_="field-item")
        if not field_item:
            continue

        tag_links = [a.get_text(strip=True) for a in field_item.find_all("a")]
        if tag_links:
            tag_values = tag_links
        else:
            raw = field_item.get_text(",", strip=True)
            tag_values = [t.strip() for t in raw.split(",") if t.strip()]

        if tag_values:
            tags_map[code] = tag_values
            if code not in codes:
                codes.append(code)

    return codes, tags_map


def extract_sections(body_tag: Tag | None) -> dict[str, str]:
    """
    Split the body content by h2/h3 headings.
    Returns {heading_text: section_text, ...}.
    """
    if not body_tag:
        return {}

    sections: dict[str, str] = {}
    current_heading = "Description"
    current_parts: list[str] = []

    for child in body_tag.children:
        if not isinstance(child, Tag):
            continue

        if child.name in ("h2", "h3"):
            # Save previous section
            text = " ".join(current_parts).strip()
            if text:
                sections[current_heading] = text
            current_heading = child.get_text(strip=True)
            current_parts = []
        else:
            t = _text(child)
            if t:
                current_parts.append(t)

    # Save last section
    text = " ".join(current_parts).strip()
    if text:
        sections[current_heading] = text

    return sections


def extract_aircraft_strings(soup: BeautifulSoup, body_text: str) -> list[str]:
    """
    Try to extract aircraft information.
    1. Look for a dedicated Drupal field (field-name-field-aircraft*)
    2. Fall back to regex patterns in the body text.
    """
    aircraft: list[str] = []

    # Drupal structured field
    field = _find_field(soup, "field-aircraft", "field-aircraft-type", "aircraft")
    if field:
        items = field.find_all("div", class_="field-item")
        for item in items:
            t = _text(item)
            if t:
                aircraft.append(t)

    # Regex fallback: "a <Type> (registration)" patterns
    if not aircraft:
        # Match things like "Augusta 109E (G-CRST)", "Boeing 737-800 (N12345)"
        for m in re.finditer(
            r"([A-Z][a-zA-Z0-9 \-]{2,30})\s+\(([A-Z]{1,3}-[A-Z0-9]{3,6}|[A-Z]{5})\)",
            body_text,
        ):
            candidate = f"{m.group(1).strip()} ({m.group(2)})"
            if candidate not in aircraft:
                aircraft.append(candidate)

    return aircraft[:10]  # cap at 10


def extract_date(title: str, body_text: str) -> str:
    """Best-effort date extraction from the article title or body."""
    # Title often starts with something like "16 Jan 2013" or has a year
    date_pattern = re.compile(
        r"\b(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
        r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{4})\b",
        re.I,
    )
    for text in (title, body_text):
        m = date_pattern.search(text)
        if m:
            return m.group(1)

    # Year only from title
    m = re.search(r"\b(19|20)\d{2}\b", title)
    return m.group(0) if m else ""


def extract_location(title: str) -> str:
    """
    The title pattern is usually:
      "TYPE, vicinity LOCATION, COUNTRY, YEAR"
    Strip the aircraft type prefix and year suffix.
    """
    # Remove leading "ICAO/type, " and trailing ", YYYY"
    loc = re.sub(r"^[^,]+,\s*", "", title)       # strip first comma-segment (aircraft type)
    loc = re.sub(r",?\s*\d{4}\s*$", "", loc).strip()
    return loc


def parse_article(slug: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # --- Title ---
    h1 = soup.find("h1")
    title = _text(h1) if h1 else slug

    # --- Summary ---
    summary_field = _find_field(soup, "field-event-description", "event-description", "summary")
    summary = _text(summary_field.find("div", class_="field-item") if summary_field else None)

    # --- Body ---
    body_field = _find_field(soup, "body")
    body_item  = body_field.find("div", class_="field-item") if body_field else None

    # Some layouts put body directly inside group-inner → fall back to searching group-left-bottom
    if not body_item:
        group = soup.find("div", class_=re.compile(r"group-left-bottom|group-inner"))
        if group:
            body_item = group

    body_text = _text(body_item)

    # --- Sections ---
    sections = extract_sections(body_item)

    # --- Event types + per-type tags ---
    event_types, event_type_tags = extract_event_types_and_tags(soup)

    # --- Aircraft ---
    aircraft = extract_aircraft_strings(soup, body_text)

    # --- Date & Location ---
    date     = extract_date(title, body_text)
    location = extract_location(title)

    return {
        "slug":            slug,
        "title":           title,
        "url":             f"{BASE_URL}{ARTICLE_PATH}/{slug}",
        "summary":         summary,
        "date":        date,
        "location":    location,
        "event_types":     event_types,
        "event_type_tags": event_type_tags,
        "aircraft":        aircraft,
        "sections":        sections,
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    username = os.environ.get("SKYBRARY_USER", "").strip()
    password = os.environ.get("SKYBRARY_PASS", "").strip()

    if not username or not password:
        print(
            "ERROR: Set SKYBRARY_USER and SKYBRARY_PASS environment variables.\n"
            "  Example:\n"
            "    export SKYBRARY_USER='you@example.com'\n"
            "    read -s SKYBRARY_PASS && export SKYBRARY_PASS\n"
            "    python scripts/process_accidents.py"
        )
        sys.exit(1)

    if not INCIDENTS_FILE.exists():
        print(f"ERROR: {INCIDENTS_FILE} not found. Run populate_accidents_incidents.py first.")
        sys.exit(1)

    resume    = "--resume"    in sys.argv
    save_html = "--save-html" in sys.argv

    # --slug <slug>  → process only that one article
    single_slug: str | None = None
    if "--slug" in sys.argv:
        idx = sys.argv.index("--slug")
        if idx + 1 < len(sys.argv):
            single_slug = sys.argv[idx + 1]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(INCIDENTS_FILE) as f:
        incidents: list[dict] = json.load(f)

    if single_slug:
        incidents = [i for i in incidents if i["slug"] == single_slug]
        if not incidents:
            # Allow processing slugs not in the listing (e.g. for ad-hoc testing)
            incidents = [{"slug": single_slug, "title": single_slug}]

    already_done: set[str] = set()
    if resume and not single_slug:
        already_done = {p.stem for p in OUTPUT_DIR.glob("*.json")}
        print(f"Resuming: {len(already_done)} articles already processed")

    todo = [i for i in incidents if i["slug"] not in already_done]
    total = len(incidents)
    done_count = len(already_done)

    print(f"{len(todo)} articles to process ({done_count} already done, {total} total)\n")

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        ok = await login(client, username, password)
        if not ok:
            print("Aborting: could not log in.")
            sys.exit(1)

        for idx, incident in enumerate(todo, 1):
            slug  = incident["slug"]
            title = incident.get("title", slug)
            print(f"[{idx}/{len(todo)}] {slug}")

            html = await fetch_article(client, slug)
            if not html:
                print("  Skipping (fetch failed).")
                continue

            if save_html:
                html_path = Path("/tmp") / f"{slug}.html"
                html_path.write_text(html, encoding="utf-8")
                print(f"  Saved HTML → {html_path}")

            article = parse_article(slug, html)

            out_path = OUTPUT_DIR / f"{slug}.json"
            with open(out_path, "w") as f:
                json.dump(article, f, indent=2, ensure_ascii=False)

            section_names = list(article["sections"].keys())
            tags_summary = {k: v for k, v in article["event_type_tags"].items()}
            print(
                f"  title: {article['title'][:60]}\n"
                f"  date: {article['date']}  location: {article['location']}\n"
                f"  aircraft: {article['aircraft']}\n"
                f"  event_types: {article['event_types']}\n"
                f"  event_type_tags: {tags_summary}\n"
                f"  sections: {section_names}"
            )

            if idx < len(todo):
                await asyncio.sleep(SLEEP_BETWEEN)

    processed = len(list(OUTPUT_DIR.glob("*.json")))
    print(f"\nDone. {processed} articles in {OUTPUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
