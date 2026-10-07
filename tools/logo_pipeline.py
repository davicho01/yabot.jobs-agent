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
    fetch  --domain D --render --out F            ...from the header logo of the page rendered in Chromium
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
    # The shorter side at SVG_RENDER_SIZE, so an icon cropped out of a wide
    # logo still has plenty of pixels (longer side capped).
    if width and height and width >= height:
        return cairosvg.svg2png(bytestring=data, output_height=SVG_RENDER_SIZE) if width / height <= 8 else (
            cairosvg.svg2png(bytestring=data, output_width=SVG_RENDER_SIZE * 8))
    if width and height:
        return cairosvg.svg2png(bytestring=data, output_width=SVG_RENDER_SIZE) if height / width <= 8 else (
            cairosvg.svg2png(bytestring=data, output_height=SVG_RENDER_SIZE * 8))
    return cairosvg.svg2png(bytestring=data, output_width=SVG_RENDER_SIZE)


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


CRISP_RENDER_SIZE = 512
CRISP_MAX_COLORS = 3  # flat icons only; detailed ones lose detail when snapped


def _dist_to_segment(p, a, b) -> float:
    ab = [y - x for x, y in zip(a, b)]
    ap = [y - x for x, y in zip(a, p)]
    t = max(0.0, min(1.0, sum(x * y for x, y in zip(ap, ab)) / (sum(v * v for v in ab) or 1)))
    return sum((pi - (ai + t * abi)) ** 2 for pi, ai, abi in zip(p, a, ab)) ** 0.5


def flat_colors(image, max_colors: int = 6) -> list[tuple[int, int, int]]:
    """An icon's real colors: its most frequent opaque colors, without
    near-duplicates or the anti-aliasing blends between two of them."""
    counts: dict[tuple[int, int, int], int] = {}
    for pixel in image.getdata():
        if pixel[3] > 200:
            counts[pixel[:3]] = counts.get(pixel[:3], 0) + 1
    total = sum(counts.values()) or 1
    kept: list[tuple[int, int, int]] = []
    for color, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n / total < 0.02 or len(kept) >= max_colors:
            break
        if any(sum((x - y) ** 2 for x, y in zip(color, c)) < 40**2 for c in kept):
            continue
        if any(_dist_to_segment(color, a, b) < 24 for i, a in enumerate(kept) for b in kept[i + 1 :]):
            continue
        kept.append(color)
    return kept


def crisp_upscale(image, colors):
    """Enlarge a small flat icon with sharp edges: smooth it at 512px, snap
    every pixel back to the icon's own colors (hard edges, no blur), then
    downsample to 128 so the edges are cleanly anti-aliased."""
    from PIL import Image, ImageFilter

    size = CRISP_RENDER_SIZE
    big = image.resize((size, size), Image.BICUBIC).filter(ImageFilter.GaussianBlur(size / max(image.size) * 0.3))
    palette = Image.new("P", (1, 1))
    flat = [v for c in colors for v in c]
    palette.putpalette(flat + flat[:3] * (256 - len(colors)))
    out = big.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE).convert("RGBA")
    out.putalpha(big.getchannel("A").point(lambda v: 255 if v > 127 else 0))
    return out.resize((128, 128), Image.LANCZOS)


def upscale(data: bytes) -> tuple[bytes, str | None]:
    """A source smaller than MIN_SIDE, enlarged to fill 128px (the backend's
    normalizer only ever shrinks): crisp for flat icons, smooth otherwise. Returns the image and its
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
    colors = flat_colors(image)
    if 1 <= len(colors) <= CRISP_MAX_COLORS and image.width == image.height:
        image = crisp_upscale(image, colors)
        original += " (crisp)"
    else:
        scale = 128 / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        original += " (smooth)"
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), original


def _foreground(image):
    """Mask of the logo's own pixels: opaque ones, or for an image with no
    transparency, the ones that differ from its background (corner) color."""
    from PIL import Image, ImageChops

    alpha = image.getchannel("A").point(lambda v: 255 if v > 32 else 0)
    if alpha.getextrema()[0] == 255:  # fully opaque: key out the background color
        rgb = image.convert("RGB")
        background = rgb.getpixel((0, 0))
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background)).convert("L")
        return diff.point(lambda v: 255 if v > 40 else 0)
    return alpha


def crop_mark(image, max_aspect: float):
    """A wide logo's icon: the leftmost part, separated from the rest (the
    wordmark) by a clear vertical gap, if it's about square and at least half
    the logo's height — e.g. Nationwide's eagle "N" from its horizontal logo.
    None if the logo has no such part."""
    mask = _foreground(image)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    image, mask = image.crop(bbox), mask.crop(bbox)
    width, height = image.size
    filled = [mask.crop((x, 0, x + 1, height)).getbbox() is not None for x in range(width)]
    min_gap = max(2, height // 20)
    right, gap = None, 0
    for x, on in enumerate(filled):
        if on:
            gap = 0
            continue
        gap += 1
        if gap >= min_gap:
            right = x - gap + 1
            break
    if right is None or right >= width - min_gap:
        return None  # one solid block: nothing to split off
    part_mask = mask.crop((0, 0, right, height))
    pb = part_mask.getbbox()
    part = image.crop((0, 0, right, height)).crop(pb)
    pw, ph = part.size
    if max(pw, ph) / min(pw, ph) > max_aspect or ph < height * 0.5:
        return None
    if image.getchannel("A").getextrema()[0] == 255:  # opaque source: make the background transparent
        part.putalpha(part_mask.crop(pb))
    return part


def square(li, data: bytes, max_aspect: float, allow_small: bool = False) -> tuple[bytes, dict]:
    """Normalized 128px PNG, plus notes on what was done to get it: the icon
    cropped out of a wide logo, and/or a small favicon upscaled."""
    import io

    from PIL import Image

    notes: dict = {}
    try:
        image = Image.open(io.BytesIO(data))
        if image.format == "ICO":
            image.size = sorted(image.info.get("sizes") or [image.size], key=lambda wh: wh[0] * wh[1])[-1]
        image = image.convert("RGBA")
    except Exception as exc:
        raise li.LogoRejected(f"not a readable image ({exc})") from exc
    if max(image.size) / max(1, min(image.size)) > max_aspect:
        mark = crop_mark(image, max_aspect)
        if mark is not None:
            notes["cropped_mark_from"] = f"{image.width}x{image.height}"
            out = io.BytesIO()
            mark.save(out, format="PNG")
            data = out.getvalue()
    if not allow_small:
        return li.normalize_or_raise(data, max_aspect=max_aspect, min_side=MIN_SIDE), notes
    try:
        data, original = upscale(data)
    except Exception as exc:
        raise li.LogoRejected(f"not a readable image ({exc})") from exc
    if original:
        notes["upscaled_from"] = original
    return li.normalize_or_raise(data, max_aspect=max_aspect, min_side=SMALL_MIN_SIDE), notes


class _Response:
    def __init__(self, status_code: int, content: bytes, headers: dict, url: str):
        self.status_code, self.content, self.headers, self.url = status_code, content, headers, url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


BLOCKED = (401, 403, 429, 503)


def browser_get(url: str) -> _Response:
    """GET through headless Chromium, for sites/CDNs that refuse plain HTTP
    clients (e.g. Akamai in front of nationwide.com and honda.com)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            response = browser.new_context().request.get(url, timeout=20_000)
            return _Response(response.status, response.body(), dict(response.headers), response.url)
        finally:
            browser.close()


def http_get(li, url: str):
    import httpx

    try:
        response = httpx.get(url, headers={"User-Agent": li.USER_AGENT}, timeout=15, follow_redirects=True)
        if response.status_code not in BLOCKED:
            return response
    except httpx.HTTPError:
        pass
    return browser_get(url)


def load_image(li, url: str, max_aspect: float, allow_small: bool = False) -> tuple[bytes, str, dict]:
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
    png, notes = square(li, data, max_aspect, allow_small)
    return png, str(response.url), notes


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


def fetch_site(li, domain: str, max_aspect: float, allow_small: bool = False) -> tuple[bytes, str, dict]:
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


RENDER_JS = """() => {
  const out = [];
  const hint = (el) => [el.tagName, el.id, el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className,
    el.getAttribute && (el.getAttribute('alt') || ''), el.getAttribute && (el.getAttribute('aria-label') || ''),
    el.getAttribute && (el.getAttribute('src') || '')].join(' ').toLowerCase();
  const logoish = (el) => { for (let e = el, i = 0; e && i < 4; e = e.parentElement || (e.getRootNode && e.getRootNode().host), i++) {
      if (/logo/.test(hint(e))) return true; } return false; };
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      const r = el.getBoundingClientRect();
      if (!r.width || r.top > 400) continue;
      if (el.tagName === 'IMG' && logoish(el)) out.push({kind: 'url', value: el.currentSrc || el.src, top: r.top});
      else if (el.tagName.toLowerCase() === 'svg' && logoish(el) && r.width > 20)
        out.push({kind: 'svg', value: el.outerHTML, top: r.top});
    }
  };
  walk(document);
  return out.sort((a, b) => a.top - b.top);
}"""


def render_candidates(domain: str, width: int) -> tuple[list[str], list[str]]:
    """Load the homepage in headless Chromium (desktop or mobile width) and
    return (image URLs, inline SVGs) that look like the header logo,
    including ones drawn by web components and any logo images it loaded."""
    from playwright.sync_api import sync_playwright

    loaded: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": width, "height": 900}).new_page()
            page.on("response", lambda r: loaded.append(r.url) if (
                "image" in (r.headers.get("content-type") or "") and re.search(r"logo", r.url, re.I)) else None)
            try:
                page.goto(f"https://{domain}", wait_until="load", timeout=45_000)
            except Exception:
                pass  # slow third-party assets: whatever has rendered is enough
            page.wait_for_timeout(3_000)  # web components draw their logo after load
            found = page.evaluate(RENDER_JS)
        finally:
            browser.close()
    urls = [f["value"] for f in found if f["kind"] == "url"] + loaded
    svgs = [f["value"] for f in found if f["kind"] == "svg"]
    return list(dict.fromkeys(urls)), list(dict.fromkeys(svgs))


def fetch_render(li, domain: str, max_aspect: float, allow_small: bool) -> tuple[bytes, str, dict]:
    reasons = []
    for width in (1280, 390):  # mobile headers often show just the icon
        urls, svgs = render_candidates(domain, width)
        for url in urls[:10]:
            try:
                return load_image(li, url, max_aspect, allow_small)
            except Exception as exc:
                reasons.append(f"{url}: {exc}")
        for svg in svgs[:5]:
            try:
                data = svg.encode()
                if b"xmlns=" not in data[:300]:
                    data = data.replace(b"<svg", b'<svg xmlns="http://www.w3.org/2000/svg"', 1)
                png, notes = square(li, rasterize_svg(data), max_aspect, allow_small)
                return png, f"rendered inline SVG on https://{domain}", notes
            except Exception as exc:
                reasons.append(f"inline svg: {exc}")
    raise li.LogoRejected("no usable logo in the rendered page. Tried: " + " | ".join(reasons[-6:]))


def cmd_fetch(args: argparse.Namespace) -> None:
    li = logo_images(args.backend)
    try:
        if args.url:
            png, source, notes = load_image(li, args.url, args.max_aspect, args.allow_small)
        elif args.render:
            png, source, notes = fetch_render(li, args.domain, args.max_aspect, args.allow_small)
        else:
            png, source, notes = fetch_site(li, args.domain, args.max_aspect, args.allow_small)
    except Exception as exc:  # LogoRejected or network trouble: just a failed candidate
        json.dump({"status": "rejected", "reason": f"{exc}"[:1500]}, sys.stdout)
        print()
        return
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(png)
    result = {"status": "ok", "png": args.out, "source_url": source, **notes}
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
    p.add_argument("--render", action="store_true", help="with --domain: load the page in headless Chromium")
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
