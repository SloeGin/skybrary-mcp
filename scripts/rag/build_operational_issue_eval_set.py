"""Create a deterministic Description-only evaluation set from authenticated reports."""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "rag" / "processed"
TAXONOMY_FILE = DATA_DIR / "operational_issues.json"
OUTPUT_FILE = DATA_DIR / "eval" / "operational_issues_eval.json"
CASE_LIMIT = 20


def main() -> None:
    taxonomy = json.loads(TAXONOMY_FILE.read_text())
    code_to_name = {
        item["code"]: name
        for name, item in taxonomy.items()
        if item.get("code") and item.get("description")
    }
    cases = []
    for path in sorted(PROCESSED_DIR.glob("*.json")):
        article = json.loads(path.read_text())
        if article.get("content_access") != "authenticated":
            continue
        description = article.get("sections", {}).get("Description", "").strip()
        expected_codes = [
            code for code in article.get("event_types", []) if code in code_to_name
        ]
        if not description or not expected_codes:
            continue
        cases.append({
            "accidentId": article["slug"],
            "queryText": description,
            "expectedIssueCodes": expected_codes,
            "expectedIssues": [code_to_name[code] for code in expected_codes],
        })
        if len(cases) == CASE_LIMIT:
            break

    if len(cases) < CASE_LIMIT:
        raise RuntimeError(f"Only {len(cases)} eligible cases found; need {CASE_LIMIT}")
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(cases)} Description-only cases to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
