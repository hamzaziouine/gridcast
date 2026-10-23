"""Provider-neutral object store over the S3 API (R2 / B2 / MinIO), BUILD_PLAN §4.1 and §13 #28.

Only four S3 calls are used (GetObject, PutObject, HeadObject, ListObjectsV2), path-style
addressing, region from OBJSTORE_REGION (`auto` on R2). Switching provider is a change of the
OBJSTORE_* secrets, never of code. Credentials are read from the environment / `.env` and are never
logged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObjstoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBJSTORE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    endpoint_url: str | None = None
    region: str = "auto"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    ro_access_key_id: str | None = None
    ro_secret_access_key: str | None = None
    bucket_private: str = "gridcast-artifacts"
    bucket_public: str = "gridcast-public"


class ObjstoreNotConfiguredError(RuntimeError):
    """Raised when the OBJSTORE_* credentials are missing."""


@dataclass
class ObjectStore:
    client: Any
    bucket: str

    def put_bytes(
        self, key: str, body: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        return bytes(self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def list_keys(self, prefix: str = "", page_size: int = 1000) -> Iterator[str]:
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": page_size}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                yield str(obj["Key"])
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")

    def put_json(self, key: str, doc: Any) -> None:
        body = json.dumps(doc, indent=1, sort_keys=True, default=str).encode("utf-8")
        self.put_bytes(key, body, "application/json")

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key))


def make_client(settings: ObjstoreSettings, read_only: bool = False) -> Any:
    key = settings.ro_access_key_id if read_only else settings.access_key_id
    secret = settings.ro_secret_access_key if read_only else settings.secret_access_key
    if not key or not secret:
        raise ObjstoreNotConfiguredError(
            "OBJSTORE_" + ("RO_" if read_only else "") + "ACCESS_KEY_ID / SECRET are not set"
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url or None,
        region_name=settings.region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(
            s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}
        ),
    )


def private_store(read_only: bool = False, settings: ObjstoreSettings | None = None) -> ObjectStore:
    s = settings or ObjstoreSettings()
    return ObjectStore(make_client(s, read_only), s.bucket_private)


def configured(settings: ObjstoreSettings | None = None) -> bool:
    s = settings or ObjstoreSettings()
    return bool(s.access_key_id and s.secret_access_key)
