from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
SEEN_PATH = DATA_DIR / "seen_jobs.json"


@dataclass(frozen=True)
class Job:
    site: str
    title: str
    location: str
    posted_on: str
    url: str
    external_path: str
    description: str
    external_id: str


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not config.get("sites"):
        raise ValueError("Config must include at least one site.")
    return config


def normalize_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slug_from_workday_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    host = parsed.netloc
    path_parts = [part for part in parsed.path.split("/") if part]

    if not host or not path_parts:
        raise ValueError(f"Not a recognized Workday career URL: {url}")

    tenant = host.split(".")[0]
    site_slug = path_parts[-1]

    return host, tenant, site_slug


def workday_api_url(public_url: str) -> str:
    host, tenant, site_slug = slug_from_workday_url(public_url)
    return f"https://{host}/wday/cxs/{tenant}/{site_slug}/jobs"


def job_detail_url(public_url: str, external_path: str) -> str:
    parsed = urlparse(public_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    career_path = parsed.path.rstrip("/")
    if external_path.startswith("http"):
        return external_path
    if external_path.startswith("/"):
        return f"{base}{external_path}"
    return f"{base}{career_path}/{external_path.lstrip('/')}"


def fetch_page(session: requests.Session, api_url: str, offset: int, page_size: int) -> dict[str, Any]:
    response = session.post(
        api_url,
        json={"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""},
        timeout=30,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def fetch_description(session: requests.Session, api_url: str, external_path: str) -> str:
    if not external_path:
        return ""

    detail_base_url = api_url.rsplit("/jobs", 1)[0]
    detail_url = f"{detail_base_url.rstrip('/')}/{external_path.strip('/')}"

    response = session.get(
        detail_url,
        timeout=30,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )

    if response.status_code in (404, 406):
        return ""

    response.raise_for_status()
    payload = response.json()
    job_posting = payload.get("jobPostingInfo") or payload.get("jobPosting") or {}
    pieces = [
        job_posting.get("jobDescription"),
        job_posting.get("qualifications"),
        job_posting.get("responsibilities"),
        job_posting.get("additionalJobDescription"),
    ]
    return normalize_text(" ".join(piece for piece in pieces if piece))



def extract_jobs(site_name: str, public_url: str, payload: dict[str, Any]) -> list[Job]:
    postings = payload.get("jobPostings") or payload.get("jobs") or []
    jobs: list[Job] = []

    for item in postings:
        title = normalize_text(item.get("title"))
        external_path = str(item.get("externalPath") or "")
        external_id = str(item.get("jobReqId") or item.get("id") or external_path or title)
        locations = item.get("locationsText") or item.get("locations") or item.get("location") or ""

        if isinstance(locations, list):
            locations = ", ".join(normalize_text(location) for location in locations)

        posted_on = item.get("postedOn") or item.get("startDate") or item.get("postedOnDate") or ""

        jobs.append(
            Job(
                site=site_name,
                title=title,
                location=normalize_text(locations),
                posted_on=normalize_text(posted_on),
                url=job_detail_url(public_url, external_path),
                external_path=external_path,
                description="",
                external_id=external_id,
            )
        )

    return jobs


def within_days(posted_on: str, days_back: int | None) -> bool:
    if not days_back or not posted_on:
        return True

    today = dt.date.today()
    lowered = posted_on.lower()

    if "today" in lowered or "yesterday" in lowered:
        return True

    match = re.search(r"(\d+)\s+day", lowered)
    if match:
        return int(match.group(1)) <= days_back

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            posted_date = dt.datetime.strptime(posted_on, fmt).date()
            return (today - posted_date).days <= days_back
        except ValueError:
            continue

    return True

def looks_expired(job: Job) -> bool:
    text = f"{job.title} {job.posted_on} {job.description}".lower()

    expired_phrases = [
        "no longer accepting applications",
        "job posting is no longer active",
        "this job is no longer available",
        "this position is no longer available",
        "applications are no longer being accepted",
    ]

    return any(phrase in text for phrase in expired_phrases)



def term_hits(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


def score_job(job: Job, background: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    searchable = f"{job.title} {job.location} {job.description}".lower()
    required = [str(term) for term in background.get("required_any", [])]
    nice_to_have = background.get("nice_to_have", {}) or {}
    negative = background.get("negative", {}) or {}

    matched = term_hits(searchable, required)
    score = len(matched) * 3

    for term, points in nice_to_have.items():
        if str(term).lower() in searchable:
            matched.append(str(term))
            score += int(points)

    negative_hits: list[str] = []
    for term, points in negative.items():
        if str(term).lower() in searchable:
            negative_hits.append(str(term))
            score -= int(points)

    return score, sorted(set(matched)), sorted(set(negative_hits))


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()

    with SEEN_PATH.open("r", encoding="utf-8") as handle:
        return set(json.load(handle))


def save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    with SEEN_PATH.open("w", encoding="utf-8") as handle:
        json.dump(sorted(seen), handle, indent=2)


def job_key(job: Job) -> str:
    return f"{job.site}|{job.external_id}|{job.url}"


def write_results(rows: list[dict[str, Any]]) -> tuple[Path, Path] | None:
    if not rows:
        return None

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"matches_{timestamp}.json"
    csv_path = OUTPUT_DIR / f"matches_{timestamp}.csv"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return csv_path, json_path


def run(config_path: Path) -> int:
    config = load_config(config_path)
    session = requests.Session()
    seen = load_seen()
    rows: list[dict[str, Any]] = []

    minimum_score = int(config.get("minimum_score", 8))
    page_size = int(config.get("page_size", 20))
    max_pages = int(config.get("max_pages_per_site", 10))
    days_back = config.get("days_back")

    for site in config["sites"]:
        site_name = site["name"]
        public_url = site["url"]
        api_url = workday_api_url(public_url)

        for page in range(max_pages):
            payload = fetch_page(session, api_url, page * page_size, page_size)
            jobs = extract_jobs(site_name, public_url, payload)

            if not jobs:
                break

            for job in jobs:
                key = job_key(job)

                if config.get("exclude_seen", True) and key in seen:
                    continue

                if not within_days(job.posted_on, days_back):
                    continue

                description = fetch_description(session, api_url, job.external_path)
full_job = Job(**{**job.__dict__, "description": description})

if looks_expired(full_job):
    seen.add(key)
    continue

score, matched_terms, negative_hits = score_job(full_job, config.get("background", {}))


                if config.get("exclude_on_negative", True) and negative_hits:
                    seen.add(key)
                    continue

                if score >= minimum_score:
                    rows.append(
                        {
                            "score": score,
                            "site": full_job.site,
                            "title": full_job.title,
                            "location": full_job.location,
                            "posted_on": full_job.posted_on,
                            "matched_terms": ", ".join(matched_terms),
                            "negative_terms": ", ".join(negative_hits),
                            "url": full_job.url,
                            "description": full_job.description[:1000],
                        }
                    )

                seen.add(key)

    save_seen(seen)
    paths = write_results(sorted(rows, key=lambda row: row["score"], reverse=True))

    if paths:
        print(f"Wrote {len(rows)} matches:")
        print(paths[0])
        print(paths[1])
    else:
        print("No new matches found.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Find matching jobs on Workday career sites.")
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    args = parser.parse_args()

    try:
        return run(Path(args.config))
    except Exception as exc:
        LOG_DIR.mkdir(exist_ok=True)

        with (LOG_DIR / "last_error.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{dt.datetime.now().isoformat()} {exc}\n")

        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
