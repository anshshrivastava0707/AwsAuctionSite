"""Makes a small WebP thumbnail for every uploaded image.

Triggered by S3 "Object Created" events (via EventBridge) for uploads/<user>/<id>.<ext>;
writes thumbs/<user>/<id>.webp, which GET /images/<key>?size=thumb redirects to.
Kept in its own code directory because it is the only function that needs Pillow.
"""
from __future__ import annotations

import io
import logging
import os

import boto3
from PIL import Image, ImageOps

log = logging.getLogger()
log.setLevel(logging.INFO)
s3 = boto3.client("s3")

MAX_SIDE = 720  # cards show ~380 CSS px wide; this stays sharp on 2x screens
QUALITY = 78


def thumb_key(key: str) -> str:
    return "thumbs/" + key.removeprefix("uploads/").rsplit(".", 1)[0] + ".webp"


def make_thumb(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)  # phone photos carry their rotation in EXIF
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        out = io.BytesIO()
        im.save(out, "WEBP", quality=QUALITY, method=4)
        return out.getvalue()


def handler(event, _context):
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]
    if not key.startswith("uploads/") or bucket != os.environ.get("IMAGES_BUCKET", bucket):
        return {"skipped": key}
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        thumb = make_thumb(body)
    except Exception:
        log.exception("could not make a thumbnail for %s", key)  # pages fall back to the original
        return {"failed": key}
    s3.put_object(Bucket=bucket, Key=thumb_key(key), Body=thumb, ContentType="image/webp",
                  CacheControl="private, max-age=86400")
    log.info("thumb %s (%d -> %d bytes)", key, len(body), len(thumb))
    return {"thumb": thumb_key(key)}
