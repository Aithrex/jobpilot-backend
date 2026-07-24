"""
AWS SSM Parameter Store Integration

This module provides secure secret retrieval from AWS Systems Manager
Parameter Store. Secrets are cached to minimize API calls and reduce latency.

Usage:
    from utils.ssm_config import get_secret, get_all_secrets
    
    # Get a single secret
    mongodb_uri = get_secret('mongodb-uri')
    
    # Get all secrets at once (more efficient)
    secrets = get_all_secrets()
"""

import os
import logging
from functools import lru_cache
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Cache for SSM client (created lazily)
_ssm_client = None


def _get_ssm_client():
    """Get or create SSM client (lazy initialization)."""
    global _ssm_client
    if _ssm_client is None:
        import boto3
        region = os.getenv('AWS_REGION_NAME', os.getenv('AWS_REGION', 'us-east-1'))
        _ssm_client = boto3.client('ssm', region_name=region)
        logger.info(f"SSM client initialized for region: {region}")
    return _ssm_client


@lru_cache(maxsize=32)
def get_secret(parameter_name: str, with_decryption: bool = True) -> Optional[str]:
    """
    Retrieve a secret from AWS SSM Parameter Store.
    
    Args:
        parameter_name: Full parameter path (e.g., '/portfolio/prod/mongodb-uri')
                       or just the name if SSM_* env var is used
        with_decryption: Whether to decrypt SecureString parameters
    
    Returns:
        The parameter value or None if not found
    
    Example:
        # Using environment variable reference
        mongodb_uri = get_secret(os.getenv('SSM_MONGODB_URI'))
        
        # Using direct path
        mongodb_uri = get_secret('/portfolio/prod/mongodb-uri')
    """
    if not parameter_name:
        return None
    
    try:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=with_decryption
        )
        value = response['Parameter']['Value']
        logger.debug(f"Successfully retrieved parameter: {parameter_name}")
        return value
    except Exception as e:
        logger.error(f"Failed to get SSM parameter '{parameter_name}': {str(e)}")
        return None


def get_all_secrets() -> Dict[str, str]:
    """
    Retrieve all portfolio secrets from SSM Parameter Store.
    
    This is more efficient than individual get_secret() calls
    as it uses GetParameters API (single API call).
    
    Returns:
        Dictionary with secret names as keys
    """
    secrets = {}
    
    # Get parameter paths from environment
    param_mappings = {
        'MONGODB_URI': os.getenv('SSM_MONGODB_URI'),
        'JWT_SECRET_KEY': os.getenv('SSM_JWT_SECRET'),
        'IPINFO_TOKEN': os.getenv('SSM_IPINFO_TOKEN'),
        'JSEARCH_API_KEY': os.getenv('SSM_JSEARCH_API_KEY'),
        # --- APIFY DISABLED (temporary) ---
        # 'APIFY_API_KEY': os.getenv('SSM_APIFY_API_KEY'),
        # 'APIFY_TOKEN': os.getenv('SSM_APIFY_TOKEN'),
        'JOB_SEARCH_PASSWORD_HASH': os.getenv('SSM_JOB_SEARCH_PASSWORD_HASH'),
        'GITHUB_PAT': os.getenv('SSM_GITHUB_PAT'),
        'GOOGLE_SIGNIN_CLIENT_ID': os.getenv('SSM_GOOGLE_SIGNIN_CLIENT_ID'),
        'GOOGLE_SIGNIN_CLIENT_SECRET': os.getenv('SSM_GOOGLE_SIGNIN_CLIENT_SECRET'),
        'GITHUB_CONNECT_CLIENT_ID': os.getenv('SSM_GITHUB_CONNECT_CLIENT_ID'),
        'GITHUB_CONNECT_CLIENT_SECRET': os.getenv('SSM_GITHUB_CONNECT_CLIENT_SECRET'),
        'LINKEDIN_CONNECT_CLIENT_ID': os.getenv('SSM_LINKEDIN_CONNECT_CLIENT_ID'),
        'LINKEDIN_CONNECT_CLIENT_SECRET': os.getenv('SSM_LINKEDIN_CONNECT_CLIENT_SECRET'),
        'INTEGRATION_TOKEN_ENCRYPTION_KEY': os.getenv('SSM_INTEGRATION_TOKEN_ENCRYPTION_KEY'),
        'GOOGLE_OAUTH_CLIENT_ID': os.getenv('SSM_GOOGLE_OAUTH_CLIENT_ID'),
        'GOOGLE_OAUTH_CLIENT_SECRET': os.getenv('SSM_GOOGLE_OAUTH_CLIENT_SECRET'),
        'GOOGLE_OAUTH_REDIRECT_URI': os.getenv('SSM_GOOGLE_OAUTH_REDIRECT_URI'),
        'GMAIL_TOKEN_ENCRYPTION_KEY': os.getenv('SSM_GMAIL_TOKEN_ENCRYPTION_KEY'),
        # Auto-apply Tranche 1 §2 — dedicated HMAC secret for signed approval
        # magic-links (NEVER the JWT secret). Fetched in the same batched
        # GetParameters call below (the loop chunks to the 10-per-call limit).
        'APPROVAL_LINK_SECRET': os.getenv('SSM_APPROVAL_LINK_SECRET'),
        # Guest Onboarding — server-side salt for guest ip_hash. Without it,
        # guest_service.hash_ip fails CLOSED (stores null, logs once).
        'GUEST_IP_HASH_SALT': os.getenv('SSM_GUEST_IP_HASH_SALT'),
        # Guest Onboarding v2 — keyed-HMAC secret for guest identity signals
        # (emails/phones/resume signatures). Unset -> fail closed: signals are
        # not stored or matched, quota subjects stay standalone.
        'GUEST_SIGNAL_HMAC_KEY': os.getenv('SSM_GUEST_SIGNAL_HMAC_KEY'),
        # Guest Onboarding v2 — HMAC secret for the short-lived guest download
        # capability tokens. Unset -> issuing tokens fails closed (503-style
        # message, never 401). NEVER reuse JWT_SECRET_KEY.
        'GUEST_DOWNLOAD_TOKEN_SECRET': os.getenv('SSM_GUEST_DOWNLOAD_TOKEN_SECRET'),
        # ZeptoMail (Zoho transactional email) Send Mail token — the transport
        # secret for email_service when EMAIL_PROVIDER=zeptomail. Missing → the
        # service falls back to dev console logging (no mail sent).
        'ZEPTOMAIL_TOKEN': os.getenv('SSM_ZEPTOMAIL_TOKEN'),
        # Managed Application Mailbox (managed-mailbox-v1 §7). Both stay unset
        # until the neutral domain + Cloudflare Worker + SSE-KMS bucket exist and
        # APPLY_MAILBOX_ENABLED flips on. MAIL_INBOUND_HMAC_SECRET is shared with
        # the Cloudflare Email Worker's secret binding (HMAC-verifies the inbound
        # webhook — NEVER the JWT secret). APPLY_MAIL_KMS_KEY_ID is the CMK id for
        # the raw/attachment quarantine bucket.
        'MAIL_INBOUND_HMAC_SECRET': os.getenv('SSM_MAIL_INBOUND_HMAC_SECRET'),
        'APPLY_MAIL_KMS_KEY_ID': os.getenv('SSM_APPLY_MAIL_KMS_KEY_ID'),
    }
    
    # Filter out empty values
    param_names = [v for v in param_mappings.values() if v]
    
    if not param_names:
        logger.warning("No SSM parameter paths configured")
        return secrets
    
    try:
        ssm = _get_ssm_client()

        # Build reverse mapping
        path_to_key = {v: k for k, v in param_mappings.items() if v}

        # SSM GetParameters accepts at most 10 names per call — batch to stay
        # under the limit (exceeding it fails the WHOLE call, which would leave
        # every secret — including JWT_SECRET_KEY — unresolved and crash init).
        for start in range(0, len(param_names), 10):
            batch = param_names[start:start + 10]
            response = ssm.get_parameters(
                Names=batch,
                WithDecryption=True
            )

            for param in response.get('Parameters', []):
                key = path_to_key.get(param['Name'])
                if key:
                    secrets[key] = param['Value']
                    logger.debug(f"Loaded secret: {key}")

            # Log any invalid parameters
            for invalid in response.get('InvalidParameters', []):
                logger.warning(f"Invalid SSM parameter: {invalid}")

        return secrets

    except Exception as e:
        logger.error(f"Failed to get SSM parameters: {str(e)}")
        return secrets


def clear_cache():
    """Clear the secrets cache. Useful for testing or forced refresh."""
    get_secret.cache_clear()
    logger.info("SSM secrets cache cleared")


class SSMConfig:
    """
    Configuration class that loads secrets from SSM Parameter Store.
    
    Usage:
        config = SSMConfig()
        print(config.MONGODB_URI)
    """
    
    def __init__(self):
        self._secrets = None
        self._use_ssm = os.getenv('USE_SSM_SECRETS', 'false').lower() == 'true'
    
    def _load_secrets(self):
        """Load secrets from SSM (lazy loading)."""
        if self._secrets is None:
            if self._use_ssm:
                self._secrets = get_all_secrets()
            else:
                self._secrets = {}
        return self._secrets
    
    def _get(self, key: str, env_fallback: str = None) -> Optional[str]:
        """
        Get a configuration value.
        
        Priority:
        1. SSM Parameter Store (if USE_SSM_SECRETS=true)
        2. Environment variable fallback
        """
        if self._use_ssm:
            secrets = self._load_secrets()
            if key in secrets:
                return secrets[key]
        
        # Fallback to environment variable
        return os.getenv(env_fallback or key)
    
    @property
    def MONGODB_URI(self) -> Optional[str]:
        return self._get('MONGODB_URI', 'MONGODB_URI')
    
    @property
    def JWT_SECRET_KEY(self) -> Optional[str]:
        return self._get('JWT_SECRET_KEY', 'JWT_SECRET_KEY')
    
    @property
    def IPINFO_TOKEN(self) -> Optional[str]:
        return self._get('IPINFO_TOKEN', 'IPINFO_TOKEN')

    @property
    def JSEARCH_API_KEY(self) -> Optional[str]:
        return self._get('JSEARCH_API_KEY', 'JSEARCH_API_KEY')

    @property
    def APIFY_API_KEY(self) -> Optional[str]:
        # --- APIFY DISABLED (temporary): never resolve an Apify key from
        # SSM/env. Restore the original return to re-enable. ---
        return None
        # return self._get('APIFY_API_KEY', 'APIFY_API_KEY')

    @property
    def APIFY_TOKEN(self) -> Optional[str]:
        # --- APIFY DISABLED (temporary) ---
        return None
        # return self._get('APIFY_TOKEN', 'APIFY_TOKEN')

    @property
    def JOB_SEARCH_PASSWORD_HASH(self) -> Optional[str]:
        return self._get('JOB_SEARCH_PASSWORD_HASH', 'JOB_SEARCH_PASSWORD_HASH')

    @property
    def GITHUB_PAT(self) -> Optional[str]:
        return self._get('GITHUB_PAT', 'GITHUB_PAT')

    @property
    def GOOGLE_OAUTH_CLIENT_ID(self) -> Optional[str]:
        return self._get('GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_ID')

    @property
    def GOOGLE_OAUTH_CLIENT_SECRET(self) -> Optional[str]:
        return self._get('GOOGLE_OAUTH_CLIENT_SECRET', 'GOOGLE_OAUTH_CLIENT_SECRET')

    @property
    def GOOGLE_OAUTH_REDIRECT_URI(self) -> Optional[str]:
        return self._get('GOOGLE_OAUTH_REDIRECT_URI', 'GOOGLE_OAUTH_REDIRECT_URI')

    @property
    def GOOGLE_SIGNIN_CLIENT_ID(self) -> Optional[str]:
        return self._get('GOOGLE_SIGNIN_CLIENT_ID', 'GOOGLE_SIGNIN_CLIENT_ID')

    @property
    def GOOGLE_SIGNIN_CLIENT_SECRET(self) -> Optional[str]:
        return self._get('GOOGLE_SIGNIN_CLIENT_SECRET', 'GOOGLE_SIGNIN_CLIENT_SECRET')

    @property
    def GITHUB_CONNECT_CLIENT_ID(self) -> Optional[str]:
        return self._get('GITHUB_CONNECT_CLIENT_ID', 'GITHUB_CONNECT_CLIENT_ID')

    @property
    def GITHUB_CONNECT_CLIENT_SECRET(self) -> Optional[str]:
        return self._get('GITHUB_CONNECT_CLIENT_SECRET', 'GITHUB_CONNECT_CLIENT_SECRET')

    @property
    def LINKEDIN_CONNECT_CLIENT_ID(self) -> Optional[str]:
        return self._get('LINKEDIN_CONNECT_CLIENT_ID', 'LINKEDIN_CONNECT_CLIENT_ID')

    @property
    def LINKEDIN_CONNECT_CLIENT_SECRET(self) -> Optional[str]:
        return self._get('LINKEDIN_CONNECT_CLIENT_SECRET', 'LINKEDIN_CONNECT_CLIENT_SECRET')

    @property
    def INTEGRATION_TOKEN_ENCRYPTION_KEY(self) -> Optional[str]:
        return self._get('INTEGRATION_TOKEN_ENCRYPTION_KEY', 'INTEGRATION_TOKEN_ENCRYPTION_KEY')

    @property
    def GMAIL_TOKEN_ENCRYPTION_KEY(self) -> Optional[str]:
        return self._get('GMAIL_TOKEN_ENCRYPTION_KEY', 'GMAIL_TOKEN_ENCRYPTION_KEY')

    @property
    def APPROVAL_LINK_SECRET(self) -> Optional[str]:
        return self._get('APPROVAL_LINK_SECRET', 'APPROVAL_LINK_SECRET')

    @property
    def GUEST_IP_HASH_SALT(self) -> Optional[str]:
        return self._get('GUEST_IP_HASH_SALT', 'GUEST_IP_HASH_SALT')

    @property
    def GUEST_SIGNAL_HMAC_KEY(self) -> Optional[str]:
        return self._get('GUEST_SIGNAL_HMAC_KEY', 'GUEST_SIGNAL_HMAC_KEY')

    @property
    def GUEST_DOWNLOAD_TOKEN_SECRET(self) -> Optional[str]:
        return self._get('GUEST_DOWNLOAD_TOKEN_SECRET', 'GUEST_DOWNLOAD_TOKEN_SECRET')

    @property
    def ZEPTOMAIL_TOKEN(self) -> Optional[str]:
        return self._get('ZEPTOMAIL_TOKEN', 'ZEPTOMAIL_TOKEN')

    @property
    def MAIL_INBOUND_HMAC_SECRET(self) -> Optional[str]:
        return self._get('MAIL_INBOUND_HMAC_SECRET', 'MAIL_INBOUND_HMAC_SECRET')

    @property
    def APPLY_MAIL_KMS_KEY_ID(self) -> Optional[str]:
        return self._get('APPLY_MAIL_KMS_KEY_ID', 'APPLY_MAIL_KMS_KEY_ID')

    @property
    def ALLOWED_ORIGINS(self) -> str:
        return os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173,http://localhost:3000')
    
    @property
    def ENVIRONMENT(self) -> str:
        return os.getenv('ENVIRONMENT', 'development')
    
    @property
    def LOG_LEVEL(self) -> str:
        return os.getenv('LOG_LEVEL', 'INFO')


# Singleton instance
ssm_config = SSMConfig()
