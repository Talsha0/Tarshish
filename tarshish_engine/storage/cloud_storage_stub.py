"""Cloud storage stub (S3 / Cloudinary) — implements the contract, not the upload.

Select it with ``STORAGE_MODE=cloud``. The class is discovered and wired like
any other backend; implement the two methods below and the rest of the engine
(media hand-off, ``media_public_url``, publishers) works unchanged.

Suggested settings, read through ``Settings.get_env`` so core needs no change:
    CLOUD_STORAGE_BUCKET           bucket / cloud name
    CLOUD_STORAGE_PUBLIC_BASE_URL  e.g. https://cdn.example.com
plus the provider's own credentials (AWS_ACCESS_KEY_ID, CLOUDINARY_URL, …).
"""

from __future__ import annotations

from core.config import Settings
from core.interfaces import BaseStorage

_NOT_IMPLEMENTED = (
    "Cloud storage is a stub. Implement CloudStorageStub.{method} in storage/cloud_storage_stub.py "
    "(or add your own backend module to storage/) or set STORAGE_MODE=local."
)


class CloudStorageStub(BaseStorage):
    plugin_name = "cloud"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.bucket = settings.get_env("CLOUD_STORAGE_BUCKET")
        self.public_base_url = (settings.get_env("CLOUD_STORAGE_PUBLIC_BASE_URL") or "").rstrip("/")

    def save(self, local_path: str, remote_key: str) -> str:
        """Upload ``local_path`` and return its public URL.

        S3 sketch::

            boto3.client("s3").upload_file(local_path, self.bucket, remote_key,
                                           ExtraArgs={"ContentType": mimetypes.guess_type(local_path)[0]})
            return f"{self.public_base_url}/{remote_key}"

        Cloudinary sketch::

            result = cloudinary.uploader.upload(local_path, public_id=remote_key, resource_type="auto")
            return result["secure_url"]
        """
        raise NotImplementedError(_NOT_IMPLEMENTED.format(method="save"))

    def delete(self, remote_key: str) -> bool:
        """Delete the object; return True when it existed.

        S3: ``delete_object(Bucket=self.bucket, Key=remote_key)``.
        Cloudinary: ``cloudinary.uploader.destroy(remote_key)``.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED.format(method="delete"))

    # ``cleanup`` keeps the base-class no-op: prefer a bucket lifecycle rule for retention.
