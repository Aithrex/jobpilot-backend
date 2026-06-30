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

            logger.info("Uploaded base resume for user %s: %s", user_id, s3_key)
            return s3_key
        except Exception as e:
            logger.error("Failed to upload base resume for user %s: %s", user_id, e)
            raise

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

            logger.info("Uploaded generated resume for user %s: %s", user_id, s3_key)
            return s3_key
        except Exception as e:
            logger.error("Failed to upload generated resume for user %s: %s", user_id, e)
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
            logger.info("Downloaded resume: %s (%d bytes)", s3_key, len(data))
            return data
        except self.client.exceptions.NoSuchKey:
            logger.error("Resume not found in S3: %s", s3_key)
            raise
        except Exception as e:
            logger.error("Failed to download resume %s: %s", s3_key, e)
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
            logger.info("Deleted resume: %s", s3_key)
            return True
        except Exception as e:
            logger.error("Failed to delete resume %s: %s", s3_key, e)
            raise

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
            logger.info("Generated presigned URL for %s (expiry=%ds)", s3_key, expiry)
            return url
        except Exception as e:
            logger.error("Failed to generate presigned URL for %s: %s", s3_key, e)
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
