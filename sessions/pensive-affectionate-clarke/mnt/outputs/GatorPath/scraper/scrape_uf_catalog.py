"""
GatorPath — UF Catalog Scraper
================================
Scrapes degree requirements from catalog.ufl.edu and outputs data/majors.json.

Usage:
    pip install -r requirements.txt
    python scrape_uf_catalog.py

The scraper fetches every undergraduate degree program listed at
catalog.ufl.edu/UGRD/colleges-schools/, parses course requirements,
critical-tracking info, and model semester plans, then saves the
structured data to ../data/majors.json.

NOTE: UF's catalog uses JavaScript rendering for some pages.
This scraper handles static pages; if a page fails, it falls
back to the seed data already in majors.json.
"""

import json
import time
import re
import os
import sys
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:  pip install -r requirements.txt")
    sys.exit(1)

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL = "https://catalog.ufl.edu"
CATALOG_BASE = f"{BASE_URL}/UGRD/colleges-schools/"
OUTPUT_FILE = Path(__file__).parent.parent / "data" / "majors.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
DELAY = 1.5  # seconds between requests (be polite to UF's servers)

# ─── Helpers ──────────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, retries: int = 3) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            print(f"  [attempt {attempt+1}] Error fetching {url}: {e}")
            time.sleep(2 ** attempt)
    return None


def parse_credits(text: str) -> int:
    """Extract credit count from a string like '3 credits' or '(3)'."""
    match = re.search(r"\b(\d)\b", text)
    return int(match.group(1)) if match else 3


def parse_course_code(text: str) -> Optional[str]:
    """Extract a UF course code like 'MAC 2311' from text."""
    match = re.search(r"([A-Z]{2,4}\s*\d{4}[A-Z]?)", text)
    return match.group(1).strip() if match else None


# ─── Catalog Discovery ────────────────────────────────────────────────────────

def discover_majors() -> list[dict]:
    """Discover all undergraduate degree program URLs from the catalog index."""
    print("Discovering programs from catalog index...")
    soup = fetch(CATALOG_BASE)
    if not soup:
        print("  Could not reach catalog index. Using seed data only.")
        return []

    programs = []
    # The catalog lists programs as links inside .sitemap or similar containers
    links = soup.select("a[href*='/UGRD/colleges-schools/']")
    seen = set()
    for link in links:
        href = link.get("href", "")
        # Program pages end with _BS/, _BA/, _BSBA/, etc.
        if re.search(r"_[A-Z]+/$", href) and href not in seen:
            seen.add(href)
            full_url = BASE_URL + href if href.startswith("/") else href
            programs.append({
                "name": link.get_text(strip=True),
                "url": full_url
            })

    print(f"  Found {len(programs)} program links.")
    return programs


# ─── Program Page Parser ──────────────────────────────────────────────────────

def parse_program_page(url: str, name: str) -> Optional[dict]:
    """
    Parse a single UF degree program page and extract structured data.
    Returns a dict matching the majors.json schema, or None if parsing fails.
    """
    print(f"  Parsing: {name} ({url})")
    soup = fetch(url)
    if not soup:
        return None
    time.sleep(DELAY)

    # ── Basic metadata ────────────────────────────────────────────────────────
    degree = "B.S."
    for tag in ["B.A.", "B.S.B.A.", "B.S.", "B.F.A.", "B.Mus.", "B.H.S."]:
        if tag in (soup.get_text() or ""):
            degree = tag
            break

    college_tag = soup.select_one(".college-name, h2.page-title + p, .program-college")
    college = college_tag.get_text(strip=True) if college_tag else "University of Florida"

    # Total credits — look for "minimum X credits" or similar
    total_credits = 120
    credit_match = re.search(r"minimum\s+of?\s*(\d{3})\s+credit", soup.get_text(), re.I)
    if credit_match:
        total_credits = int(credit_match.group(1))

    # ── Required courses ──────────────────────────────────────────────────────
    required_courses = []
    # UF catalog typically has a table or list of required courses
    course_rows = soup.select("table.sc_courselist tr, .courseblock")
    for row in course_rows:
        code_el = row.select_one(".codecol, .courseblock-code, td:first-child")
        name_el = row.select_one(".titlecol, .courseblock-title, td:nth-child(2)")
        credit_el = row.select_one(".hourscol, .courseblock-credits, td:last-child")
        if not code_el:
            continue
        code = parse_course_code(code_el.get_text(strip=True))
        if not code:
            continue
        course_name = name_el.get_text(strip=True) if name_el else ""
        credits = parse_credits(credit_el.get_text(strip=True)) if credit_el else 3
        # Infer category from heading context (look at preceding <tr class="listsum"> or <h3>)
        category = infer_category(row)
        required_courses.append({
            "code": code,
            "name": course_name,
            "credits": credits,
            "category": category
        })

    # ── Critical tracking ─────────────────────────────────────────────────────
    critical_tracking_courses = []
    ct_section = soup.find(string=re.compile(r"critical.?track", re.I))
    if ct_section:
        ct_parent = ct_section.find_parent(["section", "div", "table"])
        if ct_parent:
            for row in ct_parent.select("tr, li"):
                code = parse_course_code(row.get_text())
                if code:
                    critical_tracking_courses.append({
                        "code": code,
                        "name": row.get_text(strip=True)[:60],
                        "credits": 3,
                        "min_grade": "C"
                    })

    # ── Build result ──────────────────────────────────────────────────────────
    major_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    result = {
        "id": major_id,
        "name": name,
        "degree": degree,
        "college": college,
        "catalog_url": url,
        "total_credits": total_credits,
        "min_gpa": 2.0,
        "critical_tracking": {
            "min_gpa": 2.0,
            "courses": critical_tracking_courses
        },
        "required_courses": required_courses,
        "electives": {
            "upper_division": {"credits_required": 9, "level": "3000-4000"},
            "free": {"credits_required": 9}
        },
        "gen_ed_required": True,
        "model_plan": []
    }

    if not required_courses:
        print(f"    WARNING: No courses parsed for {name} — page may be JS-rendered.")
        return None

    return result


def infer_category(row) -> str:
    """Walk up the DOM to find the nearest section heading."""
    el = row
    for _ in range(8):
        prev = el.find_previous_sibling(["tr", "th"])
        if prev and "areaheader" in (prev.get("class") or []):
            text = prev.get_text(strip=True).lower()
            if "math" in text:
                return "math"
            if "science" in text:
                return "science"
            if "core" in text:
                return "core"
            if "elective" in text:
                return "elective"
            if "capstone" in text or "senior" in text:
                return "capstone"
        el = el.parent if el.parent else el
        if el is None:
            break
    return "required"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("GatorPath — UF Catalog Scraper")
    print("=" * 60)

    # Load existing seed data so we don't lose it
    seed_majors = []
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        seed_majors = existing.get("majors", [])
        seed_ids = {m["id"] for m in seed_majors}
        print(f"Loaded {len(seed_majors)} majors from existing seed data.")
    else:
        seed_ids = set()

    # Discover and scrape live programs
    programs = discover_majors()
    scraped_majors = []
    failed = []

    for prog in programs:
        try:
            major = parse_program_page(prog["url"], prog["name"])
            if major and major["id"] not in seed_ids:
                scraped_majors.append(major)
                print(f"    ✓ {major['name']} ({len(major['required_courses'])} courses)")
            elif major:
                print(f"    ~ {major['name']} already in seed data — skipping")
        except Exception as e:
            print(f"    ✗ Failed: {prog['name']}: {e}")
            failed.append(prog["name"])
        time.sleep(DELAY)

    # Merge: seed data first, then newly scraped
    all_majors = seed_majors + scraped_majors

    # Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "metadata": {
            "university": "University of Florida",
            "catalog_year": "2025-2026",
            "last_updated": "auto-scraped",
            "source": CATALOG_BASE,
            "total_programs": len(all_majors)
        },
        "majors": all_majors
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print()
    print(f"Done! Wrote {len(all_majors)} majors to {OUTPUT_FILE}")
    if failed:
        print(f"Failed to parse {len(failed)} programs: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
