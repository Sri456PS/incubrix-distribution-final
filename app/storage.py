from __future__ import annotations

from pathlib import Path, PurePosixPath

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.errors import DistributionError


class ObjectStorage:
    def __init__(self) -> None:
        self._s3 = None
        if settings.storage_backend == "s3":
            self._s3 = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
            )

    def ensure_ready(self) -> None:
        try:
            if self._s3:
                try:
                    self._s3.head_bucket(Bucket=settings.s3_bucket)
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code", ""))
                    if code not in {"404", "NoSuchBucket", "NotFound"}:
                        raise
                    self._s3.create_bucket(Bucket=settings.s3_bucket)
            else:
                settings.ensure_directories()
                Path(settings.storage_path).mkdir(parents=True, exist_ok=True)
        except (ClientError, BotoCoreError, OSError) as exc:
            raise DistributionError(
                "STORAGE_UNAVAILABLE",
                "Configured object storage is unavailable.",
                True,
                {"reason": str(exc)[:300]},
                503,
            ) from exc

    def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self._validate_key(key)
        try:
            if self._s3:
                self._s3.put_object(
                    Bucket=settings.s3_bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
            else:
                target = Path(settings.storage_path).joinpath(*PurePosixPath(key).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            return f"{settings.public_base_url.rstrip('/')}/api/v1/files/{key}"
        except (ClientError, BotoCoreError, OSError) as exc:
            raise DistributionError(
                "STORAGE_UNAVAILABLE",
                "Could not save object.",
                True,
                {"reason": str(exc)[:300]},
                503,
            ) from exc

    def get(self, key: str) -> tuple[bytes, str]:
        self._validate_key(key)
        try:
            if self._s3:
                obj = self._s3.get_object(Bucket=settings.s3_bucket, Key=key)
                return obj["Body"].read(), obj.get(
                    "ContentType", "application/octet-stream"
                )
            target = Path(settings.storage_path).joinpath(*PurePosixPath(key).parts)
            if not target.is_file():
                raise FileNotFoundError(key)
            return target.read_bytes(), "application/octet-stream"
        except FileNotFoundError as exc:
            raise DistributionError(
                "DESTINATION_NOT_FOUND", "Stored file was not found.", status_code=404
            ) from exc
        except (ClientError, BotoCoreError, OSError) as exc:
            raise DistributionError(
                "STORAGE_UNAVAILABLE",
                "Could not read object.",
                True,
                {"reason": str(exc)[:300]},
                503,
            ) from exc

    @staticmethod
    def _validate_key(key: str) -> None:
        path = PurePosixPath(key)
        if not key or path.is_absolute() or ".." in path.parts or "\\" in key:
            raise DistributionError(
                "INVALID_REQUEST", "Invalid storage key.", status_code=400
            )


storage = ObjectStorage()
