import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "txt": "text/plain",
}


def _redact_key(s3_key: str) -> str:
    """Log-safe form of an S3 key. Legacy keys embed original filenames /
    user ids / timestamps — never log the full key (spec section 4)."""
    key = str(s3_key or "")
    head = key.split("/", 1)[0]
    return f"{head}/..." if "/" in key else "..."


class ResumeStorageService:
    def __init__(self):
        self._client = None
        self._bucket = os.getenv("RESUME_S3_BUCKET", "")

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    @property
    def bucket(self):
        return self._bucket

    def upload_base_resume(self, user_id: str, file_bytes: bytes, filename: str) -> str:
        """Upload a base resume to S3.

        Args:
            user_id: The user identifier.
            file_bytes: Raw bytes of the resume file.
            filename: Original filename including extension.

        Returns:
            The S3 key where the file was stored.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            sanitized = self._sanitize_filename(filename)
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            s3_key = f"{user_id}/base/{timestamp}_{sanitized}"

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
            content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

            self.client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=file_bytes,
                ContentType=content_type,
            )

            logger.info("Uploaded base resume for user %s (%d bytes)", user_id, len(file_bytes))
            return s3_key
        except Exception as e:
            logger.error("Failed to upload base resume for user %s: %s", user_id, e)
            raise

    def upload_resume_asset(self, resume_id: str, file_bytes: bytes, mime_type: str) -> str:
        """Upload an original resume file under the v3 asset key scheme.

        Key = ``resume-assets/{resume_id}/original`` — carries NO name, email,
        original filename, or timestamp (Resume Platform v3, spec section 2).
        The caller (route) creates the Mongo asset doc FIRST and $sets
        ``object_key`` after this returns.

        Args:
            resume_id: Immutable asset id (uuid4 hex).
            file_bytes: Raw bytes of the uploaded file.
            mime_type: Content type of the file.

        Returns:
            The S3 key where the file was stored.
        """
        if not self.bucket:
            raise ValueError("RESUME_S3_BUCKET is not configured")

        s3_key = f"resume-assets/{resume_id}/original"
        self.client.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=file_bytes,
            ContentType=str(mime_type or "application/octet-stream"),
        )
        logger.info("Uploaded resume asset %s (%d bytes)", resume_id, len(file_bytes))
        return s3_key

    def upload_tailoring_file(
        self,
        record_id: str,
        version_id: str,
        render_version: str,
        file_bytes: bytes,
        fmt: str = "pdf",
    ) -> str:
        """Upload a rendered tailored-resume file under the v3 key scheme.

        Key = ``tailoring/{record_id}/{version_id}/{render_version}.{fmt}``
        (Resume Platform v3, spec section 2): no names, emails, original
        filenames, job titles, or second-resolution timestamps in NEW keys.
        Old objects under old schemes remain readable via stored refs.
        """
        if not self.bucket:
            raise ValueError("RESUME_S3_BUCKET is not configured")

        rid = self._sanitize_filename(str(record_id))
        vid = self._sanitize_filename(str(version_id))
        rv = self._sanitize_filename(str(render_version)) or "0"
        ext = str(fmt or "pdf").lower()
        s3_key = f"tailoring/{rid}/{vid}/{rv}.{ext}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=file_bytes,
            ContentType=CONTENT_TYPES.get(ext, "application/octet-stream"),
        )
        logger.info(
            "Uploaded tailoring file for record %s (%d bytes)", rid, len(file_bytes)
        )
        return s3_key

    def upload_generated_resume(
        self, user_id: str, file_bytes: bytes, job_title: str, format: str = "pdf"
    ) -> str:
        """Upload a generated/tailored resume to S3.

        Args:
            user_id: The user identifier.
            file_bytes: Raw bytes of the generated resume.
            job_title: The job title the resume was tailored for.
            format: File format (default: pdf).

        Returns:
            The S3 key where the file was stored.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            sanitized_title = self._sanitize_filename(job_title)
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            s3_key = f"{user_id}/generated/{timestamp}_{sanitized_title}_tailored.{format}"
            content_type = CONTENT_TYPES.get(format, "application/octet-stream")

            self.client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=file_bytes,
                ContentType=content_type,
            )

            logger.info("Uploaded generated resume for user %s (%d bytes)", user_id, len(file_bytes))
            return s3_key
        except Exception as e:
            logger.error("Failed to upload generated resume for user %s: %s", user_id, e)
            raise

    def upload_guest_resume(self, guest_id: str, file_bytes: bytes, filename: str) -> str:
        """Upload an anonymous guest's resume under the dedicated guest/ prefix.

        Keeps the raw file OUT of Mongo job payloads (the async guest_parse
        worker fetches it back by key). Objects live under
        ``guest/{guest_id}/`` so the retention cleanup can delete them with
        the expiring session.

        Args:
            guest_id: The server-issued guest session id.
            file_bytes: Raw bytes of the resume file.
            filename: Original filename including extension.

        Returns:
            The S3 key where the file was stored.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            sanitized = self._sanitize_filename(filename)
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            s3_key = f"guest/{guest_id}/{timestamp}_{sanitized}"

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
            content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

            self.client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=file_bytes,
                ContentType=content_type,
            )

            logger.info("Uploaded guest resume for %s (%d bytes)", guest_id, len(file_bytes))
            return s3_key
        except Exception as e:
            logger.error("Failed to upload guest resume for %s: %s", guest_id, e)
            raise

    def list_base_resumes(self, user_id: str) -> list:
        """List all base resumes for a user.

        Args:
            user_id: The user identifier.

        Returns:
            List of dicts with keys: s3_key, filename, uploaded_at, size_bytes.
            Sorted by uploaded_at descending (newest first).
        """
        try:
            if not self.bucket:
                logger.warning("RESUME_S3_BUCKET is not configured, returning empty list")
                return []

            prefix = f"{user_id}/base/"
            response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)

            resumes = []
            for obj in response.get("Contents", []):
                key = obj["Key"]
                # Key format: {user_id}/base/{timestamp}_{sanitized_filename}
                basename = key.split("/")[-1]
                parts = basename.split("_", 1)
                timestamp_str = parts[0] if len(parts) > 1 else ""
                filename = parts[1] if len(parts) > 1 else basename

                uploaded_at = None
                if timestamp_str:
                    try:
                        uploaded_at = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S").isoformat()
                    except ValueError:
                        uploaded_at = obj["LastModified"].isoformat()

                resumes.append(
                    {
                        "s3_key": key,
                        "filename": filename,
                        "uploaded_at": uploaded_at,
                        "size_bytes": obj["Size"],
                    }
                )

            resumes.sort(key=lambda r: r["uploaded_at"] or "", reverse=True)
            logger.info("Listed %d base resumes for user %s", len(resumes), user_id)
            return resumes
        except Exception as e:
            logger.error("Failed to list base resumes for user %s: %s", user_id, e)
            raise

    def list_generated_resumes(self, user_id: str) -> list:
        """List all generated/tailored resumes for a user.

        Args:
            user_id: The user identifier.

        Returns:
            List of dicts with keys: s3_key, filename, generated_at, size_bytes, job_title.
            Sorted by generated_at descending (newest first).
        """
        try:
            if not self.bucket:
                logger.warning("RESUME_S3_BUCKET is not configured, returning empty list")
                return []

            prefix = f"{user_id}/generated/"
            response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)

            resumes = []
            for obj in response.get("Contents", []):
                key = obj["Key"]
                # Key format: {user_id}/generated/{timestamp}_{sanitized_job_title}_tailored.{format}
                basename = key.split("/")[-1]
                parts = basename.split("_", 1)
                timestamp_str = parts[0] if len(parts) > 1 else ""
                remainder = parts[1] if len(parts) > 1 else basename

                generated_at = None
                if timestamp_str:
                    try:
                        generated_at = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S").isoformat()
                    except ValueError:
                        generated_at = obj["LastModified"].isoformat()

                # Extract job title by removing the trailing '_tailored.{ext}'
                job_title = re.sub(r"_tailored\.\w+$", "", remainder)

                resumes.append(
                    {
                        "s3_key": key,
                        "filename": basename,
                        "generated_at": generated_at,
                        "size_bytes": obj["Size"],
                        "job_title": job_title,
                    }
                )

            resumes.sort(key=lambda r: r["generated_at"] or "", reverse=True)
            logger.info("Listed %d generated resumes for user %s", len(resumes), user_id)
            return resumes
        except Exception as e:
            logger.error("Failed to list generated resumes for user %s: %s", user_id, e)
            raise

    def get_resume(self, s3_key: str) -> bytes:
        """Download a resume from S3.

        Args:
            s3_key: The S3 object key.

        Returns:
            The file contents as bytes.

        Raises:
            ClientError: If the object is not found or inaccessible.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            response = self.client.get_object(Bucket=self.bucket, Key=s3_key)
            data = response["Body"].read()
            logger.info("Downloaded resume object %s (%d bytes)", _redact_key(s3_key), len(data))
            return data
        except self.client.exceptions.NoSuchKey:
            logger.error("Resume object not found in S3: %s", _redact_key(s3_key))
            raise
        except Exception as e:
            logger.error("Failed to download resume object %s: %s", _redact_key(s3_key), e)
            raise

    def delete_resume(self, s3_key: str) -> bool:
        """Delete a resume from S3.

        Args:
            s3_key: The S3 object key.

        Returns:
            True on success.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            self.client.delete_object(Bucket=self.bucket, Key=s3_key)
            logger.info("Deleted resume object %s", _redact_key(s3_key))
            return True
        except Exception as e:
            logger.error("Failed to delete resume object %s: %s", _redact_key(s3_key), e)
            raise

    def delete_prefix_all_versions(self, prefix: str) -> dict:
        """Versioned purge for account erasure (spec section 2).

        The bucket is VERSIONED: a plain delete_object only writes a delete
        marker and every noncurrent version survives. Erasure must therefore
        list_object_versions and delete EVERY version AND delete marker under
        the prefix. Per-object results are inspected; failures are returned
        (never silently dropped) so the caller can decide whether storage
        cleanup is verified before removing the identity record.

        Returns {"deleted": int, "errors": [redacted "key@version", ...]}.
        """
        if not self.bucket:
            raise ValueError("RESUME_S3_BUCKET is not configured")
        if not prefix:
            raise ValueError("delete_prefix_all_versions requires a non-empty prefix")

        deleted = 0
        errors: list = []
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            resp = self.client.list_object_versions(**kwargs)
            objects = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for group in ("Versions", "DeleteMarkers")
                for v in resp.get(group, [])
                if v.get("Key") and v.get("VersionId")
            ]
            if objects:
                del_resp = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                batch_errors = del_resp.get("Errors") or []
                for err in batch_errors:
                    errors.append(f"{_redact_key(err.get('Key', ''))}@{err.get('VersionId', '')}")
                deleted += len(objects) - len(batch_errors)
            if resp.get("IsTruncated"):
                kwargs["KeyMarker"] = resp.get("NextKeyMarker")
                kwargs["VersionIdMarker"] = resp.get("NextVersionIdMarker")
            else:
                break
        logger.info(
            "Versioned purge under %s: %d deleted, %d errors",
            _redact_key(prefix), deleted, len(errors),
        )
        return {"deleted": deleted, "errors": errors}

    def get_presigned_url(self, s3_key: str, expiry: int = 3600) -> str:
        """Generate a presigned download URL for a resume.

        Args:
            s3_key: The S3 object key.
            expiry: URL expiration time in seconds (default: 3600).

        Returns:
            The presigned URL string.
        """
        try:
            if not self.bucket:
                raise ValueError("RESUME_S3_BUCKET is not configured")

            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=expiry,
            )
            logger.info("Generated presigned URL for %s (expiry=%ds)", _redact_key(s3_key), expiry)
            return url
        except Exception as e:
            logger.error("Failed to generate presigned URL for %s: %s", _redact_key(s3_key), e)
            raise

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Sanitize a string for use in S3 keys.

        Removes special characters, keeping only alphanumeric, hyphens,
        underscores, and dots. Converts to lowercase and truncates to 100 chars.

        Args:
            name: The raw filename or title string.

        Returns:
            A sanitized, lowercase string safe for S3 keys.
        """
        # Replace spaces with underscores
        sanitized = name.strip().replace(" ", "_")
        # Remove anything that is not alphanumeric, hyphen, underscore, or dot
        sanitized = re.sub(r"[^a-zA-Z0-9\-_.]", "", sanitized)
        # Collapse consecutive underscores/hyphens
        sanitized = re.sub(r"[_\-]{2,}", "_", sanitized)
        # Lowercase and truncate
        sanitized = sanitized.lower()[:100]
        return sanitized


# Singleton instance
_instance = None


def get_storage_service() -> ResumeStorageService:
    """Return the singleton ResumeStorageService instance."""
    global _instance
    if _instance is None:
        _instance = ResumeStorageService()
    return _instance
