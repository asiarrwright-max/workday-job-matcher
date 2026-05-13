from __future__ import annotations

import argparse
import csv
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def newest_csv(output_dir: Path) -> Path | None:
    files = sorted(output_dir.glob("matches_*.csv"), reverse=True)
    return files[0] if files else None


def load_matches(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_body(matches: list[dict[str, str]]) -> str:
    lines = [
        f"Found {len(matches)} matching job posting(s).",
        "",
    ]

    for match in matches[:20]:
        lines.extend(
            [
                f"{match.get('title', 'Untitled')} - {match.get('site', '')}",
                f"Score: {match.get('score', '')}",
                f"Location: {match.get('location', '')}",
                f"Posted: {match.get('posted_on', '')}",
                f"Matched: {match.get('matched_terms', '')}",
                f"URL: {match.get('url', '')}",
                "",
            ]
        )

    if len(matches) > 20:
        lines.append(f"And {len(matches) - 20} more in the GitHub artifact.")

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to_email = os.environ["ALERT_TO_EMAIL"]

    message = EmailMessage()
    message["From"] = user
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    csv_path = newest_csv(Path(args.output_dir))
    if not csv_path:
        print("No match CSV found; skipping email.")
        return 0

    matches = load_matches(csv_path)
    if not matches:
        print("Match CSV is empty; skipping email.")
        return 0

    send_email(
        subject=f"Workday job matcher: {len(matches)} new match(es)",
        body=build_body(matches),
    )
    print(f"Sent email for {len(matches)} match(es).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
