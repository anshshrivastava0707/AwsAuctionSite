"""Image uploads (auction photos and profile pictures).

The browser uploads straight to storage with a short-lived signed request, so
image bytes never pass through Lambda. Keys are `uploads/<userId>/<random>.<ext>`;
models.image_key only lets a user attach keys under their own prefix.

Images are served at GET /images/<key>, which redirects to a short-lived signed
S3 URL, so the bucket itself stays private.

`backend` is swappable: scripts/local_server.py installs a local-disk backend.
"""
from __future__ import annotations

import uuid

from botocore.exceptions import ClientError

from . import config
from .models import ValidationError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
UPLOAD_TTL_SECONDS = 600
DOWNLOAD_TTL_SECONDS = 3600


class S3Backend:
    def presign_upload(self, key: str, content_type: str) -> dict:
        post = config.s3_client().generate_presigned_post(
            Bucket=config.images_bucket(),
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_IMAGE_BYTES]],
            ExpiresIn=UPLOAD_TTL_SECONDS,
        )
        return {"method": "POST", "url": post["url"], "fields": post["fields"]}

    def exists(self, key: str) -> bool:
        try:
            config.s3_client().head_object(Bucket=config.images_bucket(), Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def download_url(self, key: str) -> str:
        return config.s3_client().generate_presigned_url(
            "get_object", Params={"Bucket": config.images_bucket(), "Key": key},
            ExpiresIn=DOWNLOAD_TTL_SECONDS,
        )


backend = S3Backend()


def create_upload(user_id: str, content_type: object, size: object) -> dict:
    if content_type not in CONTENT_TYPES:
        raise ValidationError("images must be JPEG, PNG, WebP or GIF")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_IMAGE_BYTES:
        raise ValidationError(f"images must be at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    key = f"uploads/{user_id}/{uuid.uuid4().hex}.{CONTENT_TYPES[content_type]}"
    return {"key": key, **backend.presign_upload(key, content_type)}


def require_uploaded(keys: list[str]) -> None:
    for key in keys:
        if not backend.exists(key):
            raise ValidationError("an image has not finished uploading")
