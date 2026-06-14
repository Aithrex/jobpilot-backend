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
        'GEMINI_API_KEY': os.getenv('SSM_GEMINI_API_KEY'),
        'JSEARCH_API_KEY': os.getenv('SSM_JSEARCH_API_KEY'),
        'APIFY_API_KEY': os.getenv('SSM_APIFY_API_KEY'),
        'APIFY_TOKEN': os.getenv('SSM_APIFY_TOKEN'),
        'JOB_SEARCH_PASSWORD_HASH': os.getenv('SSM_JOB_SEARCH_PASSWORD_HASH'),
        'GITHUB_PAT': os.getenv('SSM_GITHUB_PAT'),
        'GOOGLE_OAUTH_CLIENT_ID': os.getenv('SSM_GOOGLE_OAUTH_CLIENT_ID'),
        'GOOGLE_OAUTH_CLIENT_SECRET': os.getenv('SSM_GOOGLE_OAUTH_CLIENT_SECRET'),
        'GOOGLE_OAUTH_REDIRECT_URI': os.getenv('SSM_GOOGLE_OAUTH_REDIRECT_URI'),
        'GMAIL_TOKEN_ENCRYPTION_KEY': os.getenv('SSM_GMAIL_TOKEN_ENCRYPTION_KEY'),
    }
    
    # Filter out empty values
    param_names = [v for v in param_mappings.values() if v]
    
    if not param_names:
        logger.warning("No SSM parameter paths configured")
        return secrets
    
    try:
        ssm = _get_ssm_client()
        response = ssm.get_parameters(
            Names=param_names,
            WithDecryption=True
        )
        
        # Build reverse mapping
        path_to_key = {v: k for k, v in param_mappings.items() if v}
        
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
    def GEMINI_API_KEY(self) -> Optional[str]:
        return self._get('GEMINI_API_KEY', 'GEMINI_API_KEY')

    @property
    def JSEARCH_API_KEY(self) -> Optional[str]:
        return self._get('JSEARCH_API_KEY', 'JSEARCH_API_KEY')

    @property
    def APIFY_API_KEY(self) -> Optional[str]:
        return self._get('APIFY_API_KEY', 'APIFY_API_KEY')

    @property
    def APIFY_TOKEN(self) -> Optional[str]:
        return self._get('APIFY_TOKEN', 'APIFY_TOKEN')

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
    def GMAIL_TOKEN_ENCRYPTION_KEY(self) -> Optional[str]:
        return self._get('GMAIL_TOKEN_ENCRYPTION_KEY', 'GMAIL_TOKEN_ENCRYPTION_KEY')

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
