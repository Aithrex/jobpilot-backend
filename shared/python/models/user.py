"""User model for authentication with secure password hashing"""
from datetime import datetime
import bcrypt
import re

class User:
    """User model with bcrypt password hashing"""
    
    # Password validation rules
    MIN_PASSWORD_LENGTH = 8
    REQUIRE_UPPERCASE = True
    REQUIRE_LOWERCASE = True
    REQUIRE_NUMBER = True
    REQUIRE_SPECIAL = True
    
    @staticmethod
    def validate_password(password: str) -> tuple[bool, str]:
        """
        Validate password strength.
        Returns (is_valid, error_message)
        """
        if len(password) < User.MIN_PASSWORD_LENGTH:
            return False, f"Password must be at least {User.MIN_PASSWORD_LENGTH} characters"
        
        if User.REQUIRE_UPPERCASE and not re.search(r'[A-Z]', password):
            return False, "Password must contain at least one uppercase letter"
        
        if User.REQUIRE_LOWERCASE and not re.search(r'[a-z]', password):
            return False, "Password must contain at least one lowercase letter"
        
        if User.REQUIRE_NUMBER and not re.search(r'\d', password):
            return False, "Password must contain at least one number"
        
        if User.REQUIRE_SPECIAL and not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            return False, "Password must contain at least one special character"
        
        return True, ""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt (industry standard, secure)"""
        # Generate salt and hash - bcrypt automatically handles salt
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt(rounds=12)  # 12 rounds is secure and performant
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')
    
    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify password against bcrypt hash"""
        try:
            password_bytes = password.encode('utf-8')
            hash_bytes = password_hash.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hash_bytes)
        except Exception:
            return False
    
    @staticmethod
    def sanitize_input(value: str, max_length: int = 255) -> str:
        """Sanitize user input to prevent injection attacks"""
        if not value:
            return ""
        # Strip whitespace and limit length
        value = str(value).strip()[:max_length]
        # Remove null bytes and control characters
        value = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', value)
        return value
    
    @staticmethod
    def sanitize_email(email: str) -> str:
        """Sanitize and validate email format"""
        if not email:
            return ""
        email = User.sanitize_input(email.lower(), 320)
        # Basic email format validation
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if re.match(email_pattern, email):
            return email
        return ""
