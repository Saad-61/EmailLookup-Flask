from pydantic import BaseModel
from typing import Optional, List


class GithubProfile(BaseModel):
    url: str
    username: str
    avatar: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    repos: Optional[int] = None
    followers: Optional[int] = None
    blog: Optional[str] = None


class PersonInfo(BaseModel):
    name: Optional[str] = None
    avatar: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None


class PlatformResult(BaseModel):
    name: str
    found: bool
    icon: str
    url: Optional[str] = None


class EvidenceItem(BaseModel):
    type: str  # 'handle_match', 'name_match', 'company_match', 'location_match'
    source: str  # 'github', 'searxng_yandex', 'instagram_og', etc.
    value: str
    signal_strength: Optional[str] = "medium"  # 'strong', 'medium', 'weak'


class SocialCandidate(BaseModel):
    platform: str
    platform_label: str
    handle: str
    name: Optional[str] = None
    url: str
    snippet: Optional[str] = None
    score: int
    confidence_badge: Optional[str] = ""
    confidence_level: str = "potential"  # 'strong' or 'potential'
    reasons: List[str] = []
    evidence: List[EvidenceItem] = []
    sub_scores: dict = {}
    avatar_url: Optional[str] = None
    discovery_method: str = "probing"  # 'probing' or 'querying'


class LookupResponse(BaseModel):
    email: str
    query_time_ms: int
    email_type: str  # 'personal' or 'corporate'
    domain: Optional[str] = None
    person: PersonInfo = PersonInfo()
    profiles: dict = {}
    platforms: List[PlatformResult] = []
    phone: Optional[str] = None
    address: Optional[str] = None
    deliverability: Optional[str] = None
    autocorrect: Optional[str] = None
    company: Optional[dict] = None
    social_candidates: List[dict] = []
    social_candidates_by_platform: dict = {}
    cached: bool = False
    error: Optional[str] = None


class CacheInvalidateRequest(BaseModel):
    email: str


class CacheInvalidateResponse(BaseModel):
    success: bool
    email: str
    message: Optional[str] = None


class VerifyRequest(BaseModel):
    email: str


class LookupRequest(BaseModel):
    email: str
    force_refresh: bool = False


class VerifyResponse(BaseModel):
    email: str
    valid: Optional[bool] = None
    catchall: Optional[bool] = None
    mx_provider: Optional[str] = None
    mx_record: Optional[str] = None
    confidence: Optional[int] = None
    response_time_ms: Optional[int] = None
    port25_available: bool = False
    method_used: str = "smtp"
    error: Optional[str] = None


class PortCheckResponse(BaseModel):
    port25_available: bool
    message: str
