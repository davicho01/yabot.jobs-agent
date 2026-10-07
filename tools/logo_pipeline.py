"""Deterministic half of the logo agent: everything that must be exact.

Claude decides *which* image is the company's real logo; this script fetches
candidates, normalizes them exactly the way the backend does (it imports
app.services.logo_images from the backend checkout), stores the chosen one
and records the result. Keys follow the backend's own convention
(app.services.company_logos.store_logo):

    logos/<slug>-<first 8 hex of sha256(png)>.png   (128x128, transparent padding)

Subcommands:
    (--csv defaults to the backend checkout's CSV)
    next   [--csv PATH] [--limit N]                 rows still to do (JSON)
    fetch  --domain D --out F                     a candidate from the company's own site icons
    fetch  --url URL --out F                      a candidate from an image URL (SVG ok)
           [--allow-small]                        ...accepting a small favicon, upscaled (last resort)
    store  --csv PATH --id ID --png F --source-url URL [--dry-run]
                                                  upload it, write logo_key etc. to the CSV
    skip   --csv PATH --id ID --reason TEXT       no real logo found: mark it so it isn't retried
    heads  --versions DIR                         current Alembic head revision(s)
    newrev                                        a fresh 12-hex Alembic revision id

fetch never uploads anything; it writes the normalized PNG to --out so it can
be looked at. Uploads go to $LOGO_BUCKET (default yabot.jobs-frontend) with
the ambient AWS credentials. logo.dev is deliberately not used (out of
credits).
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

LOGO_CACHE_CONTROL = "public, max-age=31536000, immutable"  # company_logos.LOGO_CACHE_CONTROL
SVG_NS = "{http://www.w3.org/2000/svg}"
SVG_RENDER_SIZE = 512  # rasterize SVGs this big, then normalize down like any image
COLUMNS = ("logo_key", "logo_source_url", "logo_status")
DEFAULT_CSV = "backend/migrations/data/companies_missing_logos_with_domain.csv"
STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"


def logo_images(backend: str):
    """The backend's own normalizer/crawler (depends only on httpx + Pillow)."""
    sys.path.insert(0, str(Path(backend).resolve()))
    from app.services import logo_images as module

    return module


def slug(name: str) -> str:
    # Same as company_logos._slug.
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "company"


# --- CSV ---------------------------------------------------------------


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for column in COLUMNS:
        if column not in fields:
            fields.append(column)
            for row in rows:
                row[column] = ""
    return fields, rows


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def pending_row(path: Path, company_id: str) -> tuple[list[str], list[dict[str, str]], dict[str, str]]:
    fields, rows = read_csv(path)
    row = next((r for r in rows if r["id"] == company_id), None)
    if row is None:
        sys.exit(f"id {company_id} not found in {path}")
    if row["logo_key"] or row["logo_status"]:
        sys.exit(f"id {company_id} is already done (logo_key={row['logo_key']!r}, status={row['logo_status']!r})")
    return fields, rows, row


# --- SVG sources -----------------------------------------------------------


def validate_svg(data: bytes) -> None:
    """Only rasterize plain, self-contained SVG drawings."""
    root = ET.fromstring(data)
    if root.tag != f"{SVG_NS}svg":
        raise ValueError("root element must be <svg> in the SVG namespace")
    for el in root.iter():
        tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
        if tag in {"script", "foreignObject", "image"}:
            raise ValueError(f"<{tag}> is not allowed")
        for attr, value in el.attrib.items():
            name = attr.split("}")[-1]
            if name.startswith("on"):
                raise ValueError(f"event handler attribute {name} is not allowed")
            if name == "href" and not value.startswith("#"):
                raise ValueError("external references are not allowed")


def rasterize_svg(data: bytes) -> bytes:
    import cairosvg

    validate_svg(data)
    root = ET.fromstring(data)
    width = float(re.sub(r"[^\d.]", "", root.get("width", "")) or 0)
    height = float(re.sub(r"[^\d.]", "", root.get("height", "")) or 0)
    if not (width and height) and root.get("viewBox"):
        _, _, width, height = (float(v) for v in re.split(r"[\s,]+", root.get("viewBox").strip()))
    if width >= height:
        return cairosvg.svg2png(bytestring=data, output_width=SVG_RENDER_SIZE)
    return cairosvg.svg2png(bytestring=data, output_height=SVG_RENDER_SIZE)


def is_svg(content: bytes, content_type: str, url: str) -> bool:
    head = content[:1000].lstrip().lower()
    return "svg" in content_type.lower() or url.lower().split("?")[0].endswith(".svg") or head.startswith(b"<svg") or (
        head.startswith(b"<?xml") and b"<svg" in head
    )


# --- fetch -----------------------------------------------------------------
#
# A 128px square only suits a square mark: a brand's icon/symbol (app icon,
# apple-touch-icon), not its wide wordmark, which shrinks to
# an unreadable strip. So every candidate must be at most --max-aspect wide
# (default 1.5:1), and a site is searched icon-first.

MAX_ASPECT = 1.5
MIN_SIDE = 64  # preferred: big enough to look crisp at 128
SMALL_MIN_SIDE = 16  # --allow-small: a favicon, upscaled, beats having no logo


def upscale(data: bytes) -> tuple[bytes, str | None]:
    """A source smaller than MIN_SIDE, smoothly enlarged to fill 128px (the
    backend's normalizer only ever shrinks). Returns the image and its
    original size, or the data unchanged if it's big enough already."""
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(data))
    if image.format == "ICO":  # its largest frame, as the backend does
        image.size = sorted(image.info.get("sizes") or [image.size], key=lambda wh: wh[0] * wh[1])[-1]
    image = image.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    if min(image.size) >= MIN_SIDE:
        return data, None
    original = f"{image.width}x{image.height}"
    scale = 128 / max(image.size)
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), original


def square(li, data: bytes, max_aspect: float, allow_small: bool = False) -> tuple[bytes, str | None]:
    """Normalized 128px PNG, and the original size if it had to be upscaled."""
    if not allow_small:
        return li.normalize_or_raise(data, max_aspect=max_aspect, min_side=MIN_SIDE), None
    try:
        data, original = upscale(data)
    except Exception as exc:
        raise li.LogoRejected(f"not a readable image ({exc})") from exc
    return li.normalize_or_raise(data, max_aspect=max_aspect, min_side=SMALL_MIN_SIDE), original


def http_get(li, url: str):
    import httpx

    return httpx.get(url, headers={"User-Agent": li.USER_AGENT}, timeout=15, follow_redirects=True)


def load_image(li, url: str, max_aspect: float, allow_small: bool = False) -> tuple[bytes, str, str | None]:
    """One image URL (raster or SVG) -> the normalized square PNG."""
    response = http_get(li, url)
    if response.status_code != 200:
        raise li.LogoRejected(f"HTTP {response.status_code} for {url}")
    if len(response.content) > li.MAX_UPLOAD_BYTES:
        raise li.LogoRejected(f"over the size limit: {url}")
    data = response.content
    if is_svg(data, response.headers.get("content-type", ""), url):
        try:
            data = rasterize_svg(data)
        except (ValueError, ET.ParseError) as exc:
            raise li.LogoRejected(f"unsafe or unreadable SVG: {exc}") from exc
    if "html" in response.headers.get("content-type", "").lower():
        raise li.LogoRejected(f"{url} is a page, not an image")
    png, original = square(li, data, max_aspect, allow_small)
    return png, str(response.url), original


def icon_candidates(li, html: str, page_url: str) -> list[str]:
    """A homepage's icons, square-mark sources first: web-app manifest icons,
    apple-touch-icons, large/SVG favicons, then the backend crawler's own
    candidates (organization logo etc., kept only if square enough)."""
    from urllib.parse import urljoin

    manifest: list[tuple[int, str]] = []
    touch: list[tuple[int, str]] = []
    icons: list[tuple[int, str]] = []
    for tag in li._LINK_TAG_RE.findall(html):  # the backend crawler's own tag parsing
        attrs = li._attrs(tag)
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href")
        if not href:
            continue
        url = urljoin(page_url, href)
        size = li._largest_size(attrs.get("sizes"))
        if "manifest" in rel:
            try:
                data = http_get(li, url).json()
                for icon in data.get("icons", []):
                    if icon.get("src"):
                        manifest.append((li._largest_size(icon.get("sizes")), urljoin(url, icon["src"])))
            except Exception:
                pass
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            touch.append((size or 180, url))
        elif "icon" in rel or "mask-icon" in rel:
            # An SVG favicon is usually the brand mark, at any size.
            icons.append((10_000 if url.lower().split("?")[0].endswith(".svg") else size, url))
    ordered = [u for _, u in sorted(manifest, reverse=True)]
    ordered += [u for _, u in sorted(touch, reverse=True)]
    ordered += [u for _, u in sorted(icons, reverse=True)]
    ordered += li.candidates(html, page_url)
    seen: set[str] = set()
    return [u for u in ordered if u.startswith(("http://", "https://")) and not (u in seen or seen.add(u))]


def fetch_site(li, domain: str, max_aspect: float, allow_small: bool = False) -> tuple[bytes, str, str | None]:
    response = http_get(li, f"https://{domain}")
    if response.status_code != 200 or "html" not in response.headers.get("content-type", "").lower():
        raise li.LogoRejected(f"homepage of {domain} answered HTTP {response.status_code}")
    reasons = []
    for url in icon_candidates(li, response.text[: li.MAX_HTML_BYTES], str(response.url))[:12]:
        try:
            return load_image(li, url, max_aspect, allow_small)
        except Exception as exc:
            reasons.append(f"{url}: {exc}")
    raise li.LogoRejected("no square icon on the site. Tried: " + " | ".join(reasons[-6:]))


def cmd_fetch(args: argparse.Namespace) -> None:
    li = logo_images(args.backend)
    try:
        if args.url:
            png, source, original = load_image(li, args.url, args.max_aspect, args.allow_small)
        else:
            png, source, original = fetch_site(li, args.domain, args.max_aspect, args.allow_small)
    except Exception as exc:  # LogoRejected or network trouble: just a failed candidate
        json.dump({"status": "rejected", "reason": f"{exc}"[:1500]}, sys.stdout)
        print()
        return
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(png)
    result = {"status": "ok", "png": args.out, "source_url": source}
    if original:
        result["upscaled_from"] = original
    json.dump(result, sys.stdout)
    print()


# --- store / skip / next ---------------------------------------------------


def upload(key: str, body: bytes, content_type: str) -> None:
    import boto3

    bucket = os.environ.get("LOGO_BUCKET", "yabot.jobs-frontend")
    region = os.environ.get("AWS_REGION", "us-east-1")
    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type, CacheControl=LOGO_CACHE_CONTROL
    )


def cmd_store(args: argparse.Namespace) -> None:
    path = Path(args.csv)
    fields, rows, row = pending_row(path, args.id)
    png = Path(args.png).read_bytes()
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("--png must be a PNG produced by `fetch`")
    key = f"logos/{slug(row['company_name'])}-{hashlib.sha256(png).hexdigest()[:8]}.png"
    if not args.dry_run:
        upload(key, png, "image/png")
    row.update(logo_key=key, logo_source_url=args.source_url, logo_status=STATUS_FOUND)
    write_csv(path, fields, rows)
    json.dump({"id": args.id, "logo_key": key, "uploaded": not args.dry_run}, sys.stdout)
    print()


def cmd_skip(args: argparse.Namespace) -> None:
    path = Path(args.csv)
    fields, rows, row = pending_row(path, args.id)
    row.update(logo_status=STATUS_NOT_FOUND, logo_source_url=args.reason[:200])
    write_csv(path, fields, rows)
    json.dump({"id": args.id, "logo_status": STATUS_NOT_FOUND}, sys.stdout)
    print()


def cmd_next(args: argparse.Namespace) -> None:
    _, rows = read_csv(Path(args.csv))
    todo = [r for r in rows if not r["logo_key"] and not r["logo_status"]][: args.limit]
    json.dump([{k: r[k] for k in ("id", "company_name", "company_domain")} for r in todo], sys.stdout, indent=2)
    print()


# --- Alembic ---------------------------------------------------------------


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
    p.add_argument("--csv", default=DEFAULT_CSV)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("fetch")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--domain")
    p.add_argument("--out", required=True)
    p.add_argument("--max-aspect", type=float, default=MAX_ASPECT)
    p.add_argument("--allow-small", action="store_true", help="accept icons down to 16px, upscaled (last resort)")
    p.add_argument("--backend", default="backend")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("store")
    p.add_argument("--csv", default=DEFAULT_CSV)
    p.add_argument("--id", required=True)
    p.add_argument("--png", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_store)

    p = sub.add_parser("skip")
    p.add_argument("--csv", default=DEFAULT_CSV)
    p.add_argument("--id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_skip)

    p = sub.add_parser("heads")
    p.add_argument("--versions", required=True)
    p.set_defaults(func=cmd_heads)

    p = sub.add_parser("newrev")
    p.set_defaults(func=cmd_newrev)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
