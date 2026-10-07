"""Deterministic half of the logo agent: everything that must be exact.

Claude designs each logo as an SVG; this script turns it into the stored
files the backend expects and records the result. Keys follow the backend's
own convention (app.services.company_logos.store_logo):

    logos/<slug>-<first 8 hex of sha256(png)>.png   (128x128, served by the app)
    logos/<slug>-<same hash>.svg                    (vector source)

Subcommands:
    next     --csv PATH [--limit N]            rows still without a logo_key (JSON)
    process  --csv PATH --id ID --svg FILE     render, upload, write logo_key to the CSV
             [--dry-run]                       ...or skip the upload
    heads    --versions DIR                    current Alembic head revision(s)
    newrev                                     a fresh 12-hex Alembic revision id

Uploads go to $LOGO_BUCKET (default yabot.jobs-frontend) using the ambient
AWS credentials (the workflow assumes an IAM role via OIDC).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

OUTPUT_SIZE = 128  # app.services.logo_images.OUTPUT_SIZE
LOGO_CACHE_CONTROL = "public, max-age=31536000, immutable"  # company_logos.LOGO_CACHE_CONTROL
KEY_COLUMN = "logo_key"
SVG_NS = "{http://www.w3.org/2000/svg}"


def slug(name: str) -> str:
    # Same as company_logos._slug.
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "company"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if KEY_COLUMN not in fields:
        fields.append(KEY_COLUMN)
        for row in rows:
            row[KEY_COLUMN] = ""
    return fields, rows


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def validate_svg(data: bytes) -> None:
    """Reject anything that isn't a plain, self-contained SVG drawing."""
    root = ET.fromstring(data)
    if root.tag != f"{SVG_NS}svg":
        raise ValueError("root element must be <svg> in the SVG namespace")
    if not root.get("viewBox"):
        raise ValueError("<svg> needs a viewBox")
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in {"script", "foreignObject", "image", "style"}:
            raise ValueError(f"<{tag}> is not allowed")
        for attr, value in el.attrib.items():
            name = attr.split("}")[-1]
            if name.startswith("on"):
                raise ValueError(f"event handler attribute {name} is not allowed")
            if name == "href" and not value.startswith("#"):
                raise ValueError("external references are not allowed")


def render_png(svg: bytes) -> bytes:
    import cairosvg

    return cairosvg.svg2png(bytestring=svg, output_width=OUTPUT_SIZE, output_height=OUTPUT_SIZE)


def upload(key: str, body: bytes, content_type: str) -> None:
    import boto3

    bucket = os.environ.get("LOGO_BUCKET", "yabot.jobs-frontend")
    region = os.environ.get("AWS_REGION", "us-east-1")
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type, CacheControl=LOGO_CACHE_CONTROL
    )


def cmd_next(args: argparse.Namespace) -> None:
    _, rows = read_csv(Path(args.csv))
    todo = [r for r in rows if not r[KEY_COLUMN]][: args.limit]
    json.dump(todo, sys.stdout, indent=2)
    print()


def cmd_process(args: argparse.Namespace) -> None:
    path = Path(args.csv)
    fields, rows = read_csv(path)
    row = next((r for r in rows if r["id"] == args.id), None)
    if row is None:
        sys.exit(f"id {args.id} not found in {path}")
    if row[KEY_COLUMN]:
        sys.exit(f"id {args.id} already has {KEY_COLUMN}={row[KEY_COLUMN]}")

    svg = Path(args.svg).read_bytes()
    validate_svg(svg)
    png = render_png(svg)
    base = f"logos/{slug(row['company_name'])}-{hashlib.sha256(png).hexdigest()[:8]}"
    png_key, svg_key = f"{base}.png", f"{base}.svg"

    if not args.dry_run:
        upload(png_key, png, "image/png")
        upload(svg_key, svg, "image/svg+xml")
    if args.png_out:
        Path(args.png_out).write_bytes(png)

    row[KEY_COLUMN] = png_key
    write_csv(path, fields, rows)
    json.dump({"id": args.id, "logo_key": png_key, "svg_key": svg_key, "uploaded": not args.dry_run}, sys.stdout)
    print()


def cmd_heads(args: argparse.Namespace) -> None:
    revisions: set[str] = set()
    parents: set[str] = set()
    for file in Path(args.versions).glob("*.py"):
        text = file.read_text(encoding="utf-8")
        rev = re.search(r"^revision\b[^=]*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision\b[^=]*=\s*(.+)$", text, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down:
            parents.update(re.findall(r"['\"]([^'\"]+)['\"]", down.group(1)))
    print("\n".join(sorted(revisions - parents)))


def cmd_newrev(args: argparse.Namespace) -> None:
    import secrets

    print(secrets.token_hex(6))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("next")
    p.add_argument("--csv", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("process")
    p.add_argument("--csv", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--svg", required=True)
    p.add_argument("--png-out", help="also write the rendered PNG here, to inspect it")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("heads")
    p.add_argument("--versions", required=True)
    p.set_defaults(func=cmd_heads)

    p = sub.add_parser("newrev")
    p.set_defaults(func=cmd_newrev)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
