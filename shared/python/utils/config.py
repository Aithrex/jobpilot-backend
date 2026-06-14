import os
import secrets
import logging
from datetime import timedelta

# Load environment variables from .env file if available (local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

# Small observability hook: shows in Lambda logs on cold start when using SSM secrets.
logger = logging.getLogger(__name__)
if os.getenv('USE_SSM_SECRETS', 'false').lower() == 'true':
    logger.info('Shared layer: utils.config loaded')


def _get_config_value(key: str, default: str = None) -> str:
    """
    Get configuration value from SSM Parameter Store or environment variable.

    Priority:
    1. SSM Parameter Store (when USE_SSM_SECRETS=true, i.e., Lambda)
    2. Environment variable
    3. Default value
    """
    use_ssm = os.getenv('USE_SSM_SECRETS', 'false').lower() == 'true'

    if use_ssm:
        try:
            from utils.ssm_config import ssm_config
            value = getattr(ssm_config, key, None)
            if value:
                return value
        except Exception as e:
            import logging
            logging.warning(f"SSM config failed for {key}: {e}")

    # Fallback to environment variable
    return os.getenv(key, default)


def _get_jwt_secret_key() -> str:
    """
    Get JWT secret key. In production, a secret must be configured (SSM or env).
    In development, generates a per-process fallback so tokens are invalidated on restart.
    """
    value = _get_config_value('JWT_SECRET_KEY', None)
    if value:
        return value
    env = os.getenv('ENVIRONMENT', 'development').lower()
    if env != 'development':
        raise RuntimeError(
            "JWT_SECRET_KEY must be set in production (via SSM or environment variable). "
            "Refusing to run with an unconfigured secret."
        )
    return secrets.token_hex(32)


class DBConfigMeta(type):
    """Metaclass to make DBConfig work as both class and instance."""
    @property
    def DATABASE_CONFIG(cls):
        return {
            'mongo_uri': _get_config_value('MONGODB_URI', os.getenv('MONGO_URI', 'mongodb://localhost:27017/')),
            'db_name': os.getenv('DB_NAME', 'portfolio_db'),
        }


class DBConfig(object, metaclass=DBConfigMeta):
    """Database configuration - supports both local and AWS Lambda environments."""
    pass


class AppConfigMeta(type):
    """Metaclass so AppConfig.JWT_SECRET_KEY is a class-level property."""
    @property
    def JWT_SECRET_KEY(cls):
        return _get_jwt_secret_key()


class AppConfig(object, metaclass=AppConfigMeta):
    """Application configuration."""
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)


class IPInfoConfig(object):
    """Configuration for ipinfo.io API

    Get a free token at: https://ipinfo.io/signup
    Free tier: 50,000 requests/month
    """

    @classmethod
    @property
    def IPINFO_TOKEN(cls):
        return _get_config_value('IPINFO_TOKEN', '')


class GeminiConfig(object):
    """Configuration for Google Gemini AI API

    Free tier: 15 RPM, 1,500 RPD, 1M TPM
    """

    @classmethod
    @property
    def GEMINI_API_KEY(cls):
        return _get_config_value('GEMINI_API_KEY', '')


class JSearchConfig(object):
    """Configuration for JSearch (RapidAPI) job search API"""

    @classmethod
    @property
    def JSEARCH_API_KEY(cls):
        return _get_config_value('JSEARCH_API_KEY', '')


class JobSearchAuthConfigMeta(type):
    @property
    def JOB_SEARCH_PASSWORD_HASH(cls):
        return _get_config_value('JOB_SEARCH_PASSWORD_HASH', '')

class JobSearchAuthConfig(object, metaclass=JobSearchAuthConfigMeta):
    """Configuration for job search password authentication"""
    pass


class ResumeStorageConfig(object):
    @classmethod
    @property
    def S3_BUCKET(cls):
        return os.getenv('RESUME_S3_BUCKET', '')


class SandboxConfigMeta(type):
    @property
    def GITHUB_PAT(cls):
        return _get_config_value('GITHUB_PAT', '')

class SandboxConfig(object, metaclass=SandboxConfigMeta):
    """Configuration for GitHub Actions CI/CD Sandbox"""
    pass


class GmailOAuthConfigMeta(type):
    @property
    def GOOGLE_OAUTH_CLIENT_ID(cls):
        return _get_config_value('GOOGLE_OAUTH_CLIENT_ID', '')

    @property
    def GOOGLE_OAUTH_CLIENT_SECRET(cls):
        return _get_config_value('GOOGLE_OAUTH_CLIENT_SECRET', '')

    @property
    def GOOGLE_OAUTH_REDIRECT_URI(cls):
        return _get_config_value('GOOGLE_OAUTH_REDIRECT_URI', '')

    @property
    def GMAIL_TOKEN_ENCRYPTION_KEY(cls):
        return _get_config_value('GMAIL_TOKEN_ENCRYPTION_KEY', '')


class GmailOAuthConfig(object, metaclass=GmailOAuthConfigMeta):
    """Gmail OAuth + token-encryption configuration.

    Required for the Applications-tab Gmail auto-status feature.
    Redirect URI must exactly match the value registered in the Google Cloud
    OAuth client (e.g. http://localhost:5173/oauth/gmail/callback for dev).
    GMAIL_TOKEN_ENCRYPTION_KEY can be any string — it's hashed to a Fernet key.
    """
    pass
