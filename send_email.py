from __future__ import annotations

import argparse
import csv
import mimetypes
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


def build_body(matches: list[dict[str, str]], csv_path: Path) -> str:
    preview_count = min(5, len(matches))
    lines = [
        f"Found {len(matches)} matching job posting(s).",
        "",
        f"Top {preview_count} preview:",
        "",
    ]

    for index, match in enumerate(matches[:preview_count], start=1):
        lines.extend(
            [
                f"{index}. {match.get('title', 'Untitled')} - {match.get('site', '')}",
                f"   Score: {match.get('score', '')}",
                f"   Location: {match.get('location', '')}",
                f"   Posted: {match.get('posted_on', '')}",
                f"   URL: {match.get('url', '')}",
                "",
            ]
        )

    lines.extend(
        [
            "The full CSV is attached.",
            f"Attachment: {csv_path.name}",
        ]
    )

    return "\n".join(lines)


def attach_file(message: EmailMessage, file_path: Path) -> None:
    content_type, _ = mimetypes.guess_type(file_path.name)
    if content_type:
        maintype, subtype = content_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"

    message.add_attachment(
        file_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=file_path.name,
    )


def send_email(subject: str, body: str, attachment_path: Path) -> None:
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

    attach_file(message, attachment_path)

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

    top_title = matches[0].get("title", "new match")
    subject = f"Workday matcher: {len(matches)} match(es), top: {top_title}"
    body = build_body(matches, csv_path)

    send_email(subject=subject, body=body, attachment_path=csv_path)
    print(f"Sent email for {len(matches)} match(es) with CSV attachment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

