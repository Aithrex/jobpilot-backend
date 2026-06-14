"""
Visitor model - Data structures for visitor tracking

This module defines the data structures and validation for visitor-related data.
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict


@dataclass
class GeoLocation:
    """Geolocation data from IP lookup"""
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    country_name: Optional[str] = None
    timezone: Optional[str] = None
    org: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserInfo:
    """Browser and device information"""
    browser: Optional[str] = None
    browser_version: Optional[str] = None
    os: Optional[str] = None
    os_version: Optional[str] = None
    device: Optional[str] = None
    user_agent_raw: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionData:
    """Session tracking data"""
    session_id: str
    ip_address: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    page_views: int = 1
    pages_visited: List[str] = field(default_factory=list)
    is_tracked: bool = False
    visitor_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['last_activity'] = self.last_activity.isoformat()
        return data


@dataclass
class VisitorInfo:
    """Complete visitor information"""
    session_id: str
    ip_address: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    page: str = 'unknown'
    referrer: str = 'direct'
    geo: Optional[GeoLocation] = None
    browser: Optional[BrowserInfo] = None
    ip_info: Optional[Dict[str, Any]] = None
    raw_data: Optional[Dict[str, Any]] = None
    client_reported_ip: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        data = {
            'session_id': self.session_id,
            'ip_address': self.ip_address,
            'timestamp': self.timestamp.isoformat(),
            'page': self.page,
            'referrer': self.referrer,
            'client_reported_ip': self.client_reported_ip,
        }
        
        if self.geo:
            data['geo'] = self.geo.to_dict()
        if self.browser:
            data['browser'] = self.browser.browser
            data['os'] = self.browser.os
            data['device'] = self.browser.device
            data['user_agent_raw'] = self.browser.user_agent_raw
        if self.ip_info:
            data['ip_info'] = self.ip_info
        if self.raw_data:
            data['raw_data'] = self.raw_data
            
        return data


@dataclass 
class RegisteredVisitor:
    """Registered visitor with personal information"""
    first_name: str
    last_name: str
    email: str
    ip_address: str
    middle_name: Optional[str] = None
    organization: Optional[str] = None
    linkedin: Optional[Dict[str, Any]] = None
    geo: Optional[GeoLocation] = None
    browser: Optional[BrowserInfo] = None
    session_id: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None
    registered_at: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def full_name(self) -> str:
        parts = [self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        parts.append(self.last_name)
        return ' '.join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        data = {
            'first_name': self.first_name,
            'last_name': self.last_name,
            'middle_name': self.middle_name,
            'full_name': self.full_name,
            'email': self.email,
            'ip_address': self.ip_address,
            'organization': self.organization,
            'session_id': self.session_id,
            'registered_at': self.registered_at.isoformat(),
        }
        
        if self.linkedin:
            data['linkedin'] = self.linkedin
        if self.geo:
            data['geo'] = self.geo.to_dict()
        if self.browser:
            data['browser'] = self.browser.browser
            data['os'] = self.browser.os
            data['device'] = self.browser.device
        if self.fingerprint:
            data['fingerprint'] = self.fingerprint
            
        return data
