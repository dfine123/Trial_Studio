"""Cloudflare R2 storage (S3-compatible) via boto3.

R2 needs the account endpoint, region_name='auto', and SigV4 (spec gotcha #2).
The bucket stays private; browsers get media via presigned GET URLs.
"""
from __future__ import annotations

from typing import BinaryIO

import boto3
from botocore.config import Config

from app.config import settings

_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _client


def upload_fileobj(key: str, fileobj: BinaryIO, content_type: str | None = None) -> str:
    extra = {"ContentType": content_type} if content_type else None
    get_client().upload_fileobj(fileobj, settings.r2_bucket_name, key, ExtraArgs=extra)
    return key


def download_to_path(key: str, dest_path: str) -> str:
    get_client().download_file(settings.r2_bucket_name, key, dest_path)
    return dest_path


def presigned_get(key: str, expires: int = 3600) -> str:
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket_name, "Key": key},
        ExpiresIn=expires,
    )


def presigned_put(key: str, expires: int = 3600, content_type: str | None = None) -> str:
    params = {"Bucket": settings.r2_bucket_name, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return get_client().generate_presigned_url("put_object", Params=params, ExpiresIn=expires)


def head_bucket() -> dict:
    """Auth + connectivity probe. Raises botocore ClientError on bad creds/permissions."""
    return get_client().head_bucket(Bucket=settings.r2_bucket_name)
