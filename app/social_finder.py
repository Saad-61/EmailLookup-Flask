"""
social_finder.py
----------------
Discovers and scores candidate accounts on LinkedIn, Instagram, TikTok, Pinterest,
Twitter/X, Facebook, and GitHub using:
1. High-concurrency direct OpenGraph / crawler probing across 5 platforms.
2. Clean single-site search queries via SearXNG (Yandex + Startpage).
3. Smart compound name parsing (e.g. nomanghaffar074 -> Noman Ghaffar).
4. Multi-anchor scoring with Jaro-Winkler similarity, surname disambiguation,
   and company/location corroboration.
"""

import asyncio
import os
import random
import re
import html
import urllib.parse
import sys
import time
import unicodedata
from typing import List, Optional, Dict, Any, Tuple
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*duckduckgo_search.*")

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)


# ==========================================
# 1. STRING SIMILARITY & JARO-WINKLER
# ==========================================
def jaro_similarity(s1: str, s2: str) -> float:
    """Compute standard Jaro similarity between two strings."""
    if not s1 or not s2:
        return 1.0 if s1 == s2 else 0.0
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    match_distance = max(len1, len2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2

    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j]:
                continue
            if s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    transpositions //= 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def jaro_winkler_similarity(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """
    Compute Jaro-Winkler similarity with prefix bonus.
    Accounts for common transliterations and minor spelling variations.
    """
    s1_clean = (s1 or "").strip().lower()
    s2_clean = (s2 or "").strip().lower()
    if not s1_clean or not s2_clean:
        return 1.0 if s1_clean == s2_clean else 0.0
    if s1_clean == s2_clean:
        return 1.0

    j_sim = jaro_similarity(s1_clean, s2_clean)

    prefix_len = 0
    for c1, c2 in zip(s1_clean[:4], s2_clean[:4]):
        if c1 == c2:
            prefix_len += 1
        else:
            break

    return min(1.0, j_sim + (prefix_len * prefix_weight * (1.0 - j_sim)))


# ==========================================
# 2. DYNAMIC RESIDENTIAL PROXY POOL
# ==========================================
PROXY_USER = os.getenv("PROXY_USERNAME", "").strip()
PROXY_PASS = os.getenv("PROXY_PASSWORD", "").strip()
RAW_IPS = os.getenv("PROXY_IPS", "").strip()
ALL_PROXY_IPS = [ip.strip() for ip in RAW_IPS.split(",") if ip.strip()]
PROXY_IPS = ALL_PROXY_IPS


class DynamicProxyPool:
    """
    Manages residential proxy IPs with automatic rate-limit cooldown and latency-based ranking.
    - Tracks temporary 202 blocks with an expiration timestamp (e.g. 10 minutes).
    - Automatically measures roundtrip response time on every query to route requests to the fastest nodes.
    - When cooldown expires, the proxy automatically re-joins the active pool.
    """
    def __init__(self, ip_list: List[str], cooldown_seconds: int = 600):
        self.all_ips = list(ip_list)
        self.cooldown_seconds = cooldown_seconds
        self.cooldowns: Dict[str, float] = {}
        # ip -> estimated latency in ms (pre-seeded with default 1500ms)
        self.latencies: Dict[str, float] = {ip: 1500.0 for ip in self.all_ips}

    def get_clean_ips(self) -> List[str]:
        """Returns all IPs whose cooldown has expired, ranked by lowest latency first."""
        now = time.time()
        clean = [ip for ip in self.all_ips if self.cooldowns.get(ip, 0) <= now]
        if not clean and self.all_ips:
            return sorted(self.all_ips, key=lambda ip: self.cooldowns.get(ip, 0))[:5]
        # Rank by latency with slight jitter for balanced load distribution
        return sorted(clean, key=lambda ip: self.latencies.get(ip, 2000.0) + random.uniform(0, 150))

    def mark_challenged(self, ip: str, duration: Optional[int] = None):
        """Temporarily flag an IP that encountered a 202 challenge or block."""
        cd = duration or self.cooldown_seconds
        self.cooldowns[ip] = time.time() + cd
        self.latencies[ip] = 9999.0
        remaining = len(self.get_clean_ips())
        print(f"  [ProxyPool] [COOLDOWN] IP {ip} flagged with {cd}s cooldown ({remaining} clean IPs remaining in pool)", flush=True)

    def mark_healthy(self, ip: str, elapsed_ms: Optional[int] = None):
        """Confirm an IP is clean and update its moving-average latency score."""
        if ip in self.cooldowns:
            del self.cooldowns[ip]
        if elapsed_ms is not None:
            prev = self.latencies.get(ip, float(elapsed_ms))
            self.latencies[ip] = prev * 0.35 + float(elapsed_ms) * 0.65

    def sample_distinct(self, n: int) -> List[str]:
        """Sample n distinct clean IPs, prioritizing the fastest responsive nodes."""
        clean = self.get_clean_ips()
        if not clean:
            return ["" for _ in range(n)]
        if len(clean) >= n:
            return clean[:n]
        return (clean * (n // len(clean) + 1))[:n]


proxy_pool = DynamicProxyPool(ALL_PROXY_IPS, cooldown_seconds=600)


def get_random_proxy_url() -> Optional[str]:
    """Construct randomized proxy URL from fastest clean residential proxies in pool."""
    clean_ips = proxy_pool.get_clean_ips()
    if not clean_ips:
        return None
    ip = random.choice(clean_ips[:5]) if len(clean_ips) >= 5 else random.choice(clean_ips)
    if PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{ip}"
    return f"http://{ip}"


# ==========================================
# 3. COMPOUND NAME PARSER & HANDLE GENERATION
# ==========================================
TITLE_PREFIXES = {
    "ch", "chaudhry", "chaudhary", "dr", "engr", "eng", "mr", "ms", "mrs", 
    "prof", "syed", "sh", "sk", "sheikh", "md", "muhd", "malik", "adv", "al", "el", "haj", "haji"
}

COMMON_FIRST_NAMES = {
    "fahad", "ahmad", "ahmed", "saad", "noman", "nouman", "nauman", "ali", "hamza", "usman", "osman",
    "bilal", "hassan", "hasan", "hussain", "zain", "omer", "umar", "faisal", "farhan", 
    "kashif", "tariq", "asif", "dameesha", "ahtisham", "atisam", "dilawar", "hameed", "mohid", "faraz",
    "ghaffar", "rashid", "tahir", "nasir", "amir", "aamir", "sami", "haris", "junaid", "shayan", "daniyal",
    "waseem", "wasim", "naveed", "navid", "arshad", "akram", "aslam", "iqbal", "anwar", "mustafa", "faizan",
    "akhtar", "latif", "mahmood", "mehmood", "butt", "dar", "bhatti", "rana", "khan", "arslan", "ahsan",
    "chaudhry", "malik", "sheikh", "syed", "shah", "javed", "javaid", "siddiqui", "sohaib", "suhaib",
    "qureshi", "ansari", "farooqi", "abbasi", "mirza", "baig", "mughal", "rehman", "danish", "talha",
    "rahman", "aziz", "khalid", "sultan", "alam", "raza", "ashraf", "munir", "zafar", "hamad", "hammad",
    "nawaz", "sarwar", "liaquat", "abid", "sajid", "majid", "zahid", "shahzad", "shahzaib", "shehryar",
    "khurram", "shahbaz", "tanveer", "tanvir", "waheed", "wahid", "yousaf", "yusuf", "burhan", "basit",
    "yaqoob", "ayub", "arouba", "ayesha", "fatima", "zainab", "maryam", "mariam", "sameer", "samir",
    "hira", "sana", "iqra", "amna", "sadia", "mahnoor", "anmol", "noor", "rabia", "affan", "huzaifa",
    "sidra", "kinza", "alishba", "hafsa", "laiba", "bisma", "aiman", "nimra", "shahmeer", "rayyan",
    "bushra", "sumaira", "shazia", "rubina", "farzana", "tahira", "samina", "yasmeen", "ayaan", "zayan",
    "shabnam", "nasreen", "parveen", "uzma", "fauzia", "fozia", "saima", "asifa",
    "nida", "fariha", "hina", "madiha", "kiran", "mehwish", "komal", "natasha", "sonia",
    "erik", "john", "david", "michael", "james", "robert", "william", "richard",
    "thomas", "charles", "daniel", "matthew", "anthony", "mark", "donald", "steven",
    "paul", "andrew", "joshua", "kenneth", "kevin", "brian", "george", "timothy",
    "ronald", "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric",
    "jonathan", "stephen", "larry", "justin", "scott", "brandon", "benjamin", "samuel",
    "gregory", "alexander", "frank", "patrick", "raymond", "jack", "dennis", "jerry",
    "tyler", "aaron", "jose", "adam", "nathan", "henry", "douglas", "zachary", "peter",
    "kyle", "walter", "ethan", "jeremy", "harold", "keith", "christian", "roger", "noah",
    "gerald", "carl", "terry", "sean", "austin", "arthur", "lawrence", "jesse", "dylan",
    "bryan", "joe", "jordan", "billy", "albert", "bruce", "willie", "gabriel", "logan",
    "alan", "juan", "wayne", "roy", "ralph", "randy", "eugene", "vincent", "russell",
    "louis", "philip", "bobby", "johnny", "bradley", "haseeb", "rauf", "collison"
}


ROLE_SUFFIXES = {
    "hr", "dev", "qa", "ceo", "cto", "cfo", "coo", "cmo", "admin", "recruiter", 
    "sales", "support", "help", "jobs", "hiring", "team", "legal", "ops", "design", "tech", "official"
}


def split_compound_name(local_part: str) -> Tuple[str, str]:
    """
    Splits compound local-part (e.g. ch.fahadahmad11 -> Ch Fahad Ahmad, sarah.jenkins.hr -> Sarah Jenkins, nomanghaffar074 -> Noman Ghaffar).
    Returns (first_name, last_name) or (full_inferred_name, "").
    """
    if not local_part:
        return "", ""

    # Check for delimited local parts e.g. sarah.jenkins.hr, john_doe
    if any(sep in local_part for sep in (".", "_", "-")):
        chunks = [re.sub(r'[\d._+-]+', '', c).strip().lower() for c in re.split(r'[._+-]', local_part)]
        chunks = [c for c in chunks if len(c) >= 2]
        if chunks and chunks[-1] in ROLE_SUFFIXES and len(chunks) >= 3:
            chunks = chunks[:-1]
        if len(chunks) >= 2:
            return chunks[0].capitalize(), chunks[1].capitalize()

    clean = re.sub(r'[\d._+-]+', '', local_part).lower().strip()
    if not clean or len(clean) < 3:
        return "", ""

    title = ""
    s = clean
    for t in sorted(TITLE_PREFIXES, key=len, reverse=True):
        if s.startswith(t) and len(s) >= len(t) + 4:
            title = t.capitalize()
            s = s[len(t):]
            break

    if len(s) >= 5 and s[0] == 'r' and s[1:] in COMMON_FIRST_NAMES:
        return s[1:].capitalize(), ""

    for fn in sorted(COMMON_FIRST_NAMES, key=len, reverse=True):
        if s.startswith(fn) and len(s) > len(fn):
            rem = s[len(fn):]
            if len(rem) >= 2:
                fn_cap = f"{title} {fn.capitalize()}".strip() if title else fn.capitalize()
                return fn_cap, rem.capitalize()

    return clean.capitalize(), ""


def generate_handle_variations(
    email: str,
    name: Optional[str] = None,
    gh_username: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Generate prioritized handle variations from email local-part and name."""
    specific = []
    stems = []
    local = email.split("@")[0].lower().strip() if "@" in email else ""

    if local:
        specific.append(local)
        clean_no_sep = re.sub(r"[._+-]", "", local)
        if clean_no_sep != local:
            specific.append(clean_no_sep)
        clean_no_num = re.sub(r"\d+", "", clean_no_sep)
        if len(clean_no_num) >= 3:
            stems.append(clean_no_num)

        chunks = [re.sub(r"\d+", "", c).strip("._-") for c in re.split(r"[._+-]", local)]
        chunks = [c for c in chunks if len(c) >= 2]

        for c in chunks:
            if len(c) >= 3 and c not in stems:
                stems.append(c)

        if len(chunks) >= 2:
            stems.append("".join(chunks))
            stems.append(f"{chunks[1]}{chunks[0]}")
            stems.append(f"{chunks[0]}.{chunks[1]}")
            stems.append(f"{chunks[0]}_{chunks[1]}")

    if gh_username:
        gh_clean = gh_username.lower().strip()
        if gh_clean not in specific:
            specific.append(gh_clean)
        gh_no_sep = re.sub(r"[._+-]", "", gh_clean)
        if gh_no_sep != gh_clean and gh_no_sep not in specific:
            specific.append(gh_no_sep)

    if not name and local:
        fn, ln = split_compound_name(local)
        if fn and ln:
            name = f"{fn} {ln}"
        elif fn:
            name = fn

    if name:
        parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", name)]
        title = ""
        core_parts = parts
        if parts and parts[0] in TITLE_PREFIXES and len(parts) > 1:
            title = parts[0]
            core_parts = parts[1:]

        if len(core_parts) >= 2:
            first, last = core_parts[0], core_parts[-1]
            concat = "".join(core_parts)
            rev_concat = f"{last}{first}"
            # Core name permutations
            for term in (
                f"{first}.{last}", f"{last}.{first}",
                f"{first}_{last}", f"{last}_{first}",
                f"{first}-{last}", f"{last}-{first}",
                concat, rev_concat,
                f"{first[0]}{last}", f"{last[0]}{first}"
            ):
                if term not in specific and term not in stems:
                    stems.append(term)

            # Permutations with Title prefix
            if title:
                for term in (
                    f"{title}{concat}", f"{title}.{first}.{last}",
                    f"{title}_{concat}", f"{title}.{concat}",
                    f"{title}_{first}_{last}", f"{title}{first}"
                ):
                    if term not in specific and term not in stems:
                        stems.append(term)

            if first in stems:
                stems.remove(first)
            stems.insert(0, first)
        elif len(core_parts) == 1:
            first = core_parts[0]
            if first in stems:
                stems.remove(first)
            stems.insert(0, first)

    generic = {
        "admin", "info", "support", "sales", "contact", "help",
        "billing", "team", "hello", "official", "mail", "user", "test",
        "gmail", "yahoo", "hotmail", "outlook", "profile", "account", "dev",
    }
    filtered_specific = [v for v in dict.fromkeys(specific) if len(v) >= 3 and v not in generic]
    filtered_stems = [v for v in dict.fromkeys(stems) if len(v) >= 3 and v not in generic]
    return filtered_specific, filtered_stems


def expand_social_probe_handles(
    specific_handles: List[str],
    stem_handles: List[str],
    name: Optional[str] = None,
) -> List[str]:
    """Generate targeted handle permutations for direct social probing across platforms."""
    probes = []
    
    # Identify bare surname to strictly prevent probing it alone (AGENTS.md Rule 3.3)
    bare_surname = ""
    if name and len(name.split()) >= 2:
        bare_surname = name.split()[-1].lower().strip()

    for h in (specific_handles + stem_handles):
        clean = h.strip().lower()
        if clean and clean not in probes and len(clean) >= 3 and clean != bare_surname:
            probes.append(clean)

    seeds = []
    if name:
        parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", name)]
        if parts:
            first = parts[0]
            if len(first) >= 3 and first not in seeds and first != bare_surname:
                seeds.append(first)

    for h in stem_handles:
        clean = re.sub(r"\d+", "", h).strip("._-").lower()
        if clean and len(clean) >= 3 and clean not in seeds and len(clean) <= 12 and clean != bare_surname:
            seeds.append(clean)

    for h in specific_handles:
        clean = re.sub(r"\d+", "", h).strip("._-").lower()
        if clean and len(clean) >= 3 and clean not in seeds and clean != bare_surname:
            seeds.append(clean)

    # High-signal handle templates ({clean}_, _{clean}, momina0_, _momina0, ahtisham.v2, ahtisham_v2, dameesha_09)
    variation_templates = [
        "{clean}_",
        "_{clean}",
        "{clean}0_",
        "_{clean}0",
        "{clean}_0",
        "{clean}.v2",
        "{clean}_v2",
        "{clean}_09",
        "{clean}09",
        "{clean}_01",
    ]

    for s in seeds[:3]:
        for tmpl in variation_templates:
            v = tmpl.format(clean=s)
            if v not in probes:
                probes.append(v)

    return probes


# ==========================================
# 4. PARSER & TITLE / NAME CLEANERS
# ==========================================
RESERVED_SYSTEM_SLUGS = {
    "https", "http", "www", "com", "net", "org", "null", "undefined"
}

def parse_social_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a social media URL into platform, canonical profile URL, and handle."""
    if not url:
        return None

    clean = url.split("?")[0].rstrip("/").strip(")>]\'\",.")
    m_nested = list(re.finditer(r"https?:/+", clean, re.IGNORECASE))
    if len(m_nested) > 1:
        clean = clean[m_nested[-1].start():]
    clean = re.sub(r"^(https?):/+([^\s/])", r"\1://\2", clean, flags=re.IGNORECASE)

    # Twitter / X
    tw_match = re.search(r"https?://(?:[a-z0-9-]+\.)?(?:x\.com|twitter\.com)/([a-zA-Z0-9_]{1,25})$", clean, re.IGNORECASE)
    if tw_match:
        handle = tw_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("home", "explore", "search", "notifications", "messages", "settings", "i", "privacy", "tos", "intent", "share"):
            return {
                "platform": "twitter",
                "platform_label": "X / Twitter",
                "handle": handle,
                "url": f"https://x.com/{handle}",
            }

    # Instagram
    ig_match = re.search(r"https?://(?:[a-z0-9-]+\.)?instagram\.com/([a-zA-Z0-9_.]{1,30})/?$", clean, re.IGNORECASE)
    if ig_match:
        handle = ig_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("p", "reel", "reels", "stories", "explore", "direct", "accounts", "about", "developer", "legal"):
            return {
                "platform": "instagram",
                "platform_label": "Instagram",
                "handle": handle,
                "url": f"https://www.instagram.com/{handle}",
            }

    # Facebook
    fb_people_match = re.search(r"https?://(?:[a-z0-9-]+\.)?facebook\.com/people/([^/?#]+)/(\d+)", clean, re.IGNORECASE)
    if fb_people_match:
        p_name = fb_people_match.group(1).replace("-", " ")
        p_id = fb_people_match.group(2)
        return {
            "platform": "facebook",
            "platform_label": "Facebook",
            "handle": p_name,
            "url": f"https://www.facebook.com/people/{fb_people_match.group(1)}/{p_id}/",
        }

    fb_match = re.search(r"https?://(?:[a-z0-9-]+\.)?facebook\.com/([a-zA-Z0-9_.]{3,50})/?$", clean, re.IGNORECASE)
    if fb_match:
        handle = fb_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("sharer", "share", "login", "recover", "help", "policies", "privacy", "pages", "groups", "events", "watch", "photo", "photos", "video", "videos", "reel", "reels", "posts"):
            return {
                "platform": "facebook",
                "platform_label": "Facebook",
                "handle": handle,
                "url": f"https://www.facebook.com/{handle}",
            }

    # LinkedIn
    if "linkedin.com/in/" in clean:
        li_match = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([a-zA-Z0-9_/%-]+)", clean, re.IGNORECASE)
        if li_match:
            slug = li_match.group(1).split("?")[0].rstrip("/")
            if slug.lower() not in RESERVED_SYSTEM_SLUGS and slug.lower() not in ("dir", "pub", "feed", "jobs", "company", "school", "pulse", "posts", "learning"):
                return {
                    "platform": "linkedin",
                    "platform_label": "LinkedIn",
                    "handle": slug,
                    "url": f"https://www.linkedin.com/in/{slug}",
                }

    # TikTok
    tt_match = re.search(r"https?://(?:[a-z0-9-]+\.)?tiktok\.com/@([a-zA-Z0-9_.]{2,30})/?$", clean, re.IGNORECASE)
    if tt_match:
        handle = tt_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("explore", "direct", "trending", "about", "discover", "login", "live", "tag"):
            return {
                "platform": "tiktok",
                "platform_label": "TikTok",
                "handle": handle,
                "url": f"https://www.tiktok.com/@{handle}",
            }

    # Pinterest
    pin_match = re.search(r"https?://(?:[a-z0-9-]+\.)?pinterest\.com/([a-zA-Z0-9_.]{2,30})/?$", clean, re.IGNORECASE)
    if pin_match:
        handle = pin_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("explore", "pin", "ideas", "business", "help", "about", "login", "today", "shop", "news"):
            return {
                "platform": "pinterest",
                "platform_label": "Pinterest",
                "handle": handle,
                "url": f"https://www.pinterest.com/{handle}/",
            }

    # GitHub
    gh_match = re.search(r"https?://(?:[a-z0-9-]+\.)?github\.com/([a-zA-Z0-9_-]{1,39})/?$", clean, re.IGNORECASE)
    if gh_match:
        handle = gh_match.group(1)
        if handle.lower() not in ("features", "business", "explore", "marketplace", "pricing", "topics", "collections", "events"):
            return {
                "platform": "github",
                "platform_label": "GitHub",
                "handle": handle,
                "url": f"https://github.com/{handle}",
            }

    # Spotify (Strictly User Profiles)
    sp_match = re.search(r"https?://(?:open\.)?spotify\.com/(?:intl-[a-z]{2,5}/)?user/([a-zA-Z0-9_.-]{2,60})(?:/.*)?$", clean, re.IGNORECASE)
    if sp_match:
        handle = sp_match.group(1)
        if handle.lower() not in RESERVED_SYSTEM_SLUGS and handle.lower() not in ("download", "search", "genre", "playlist", "track", "album", "artist", "user", "explore", "collection"):
            return {
                "platform": "spotify",
                "platform_label": "Spotify",
                "handle": handle,
                "url": f"https://open.spotify.com/user/{handle}",
            }

    return None


def format_handle_to_name(handle: str, resolved_name: Optional[str] = None) -> str:
    """Format a social handle into a clean human display name."""
    if not handle:
        return ""
    clean = handle.lstrip("@").strip()
    name_clean = re.sub(r"\d+$", "", clean).strip("._-")
    parts = [p.capitalize() for p in re.split(r"[._-]+", name_clean) if len(p) >= 2]
    if len(parts) >= 2:
        return " ".join(parts)
    elif len(parts) == 1:
        if resolved_name and parts[0].lower() in resolved_name.lower():
            return resolved_name
        return parts[0]
    return clean


def clean_display_name(raw_title: str, handle: str, platform: str, resolved_name: Optional[str] = None) -> str:
    """Extract and unescape authentic display name from title tag, stripping entity garbage and generic boilerplate."""
    if not raw_title:
        return format_handle_to_name(handle, resolved_name)

    t = unicodedata.normalize('NFKD', html.unescape(raw_title)).strip()

    if platform == "linkedin" or "linkedin" in t.lower():
        t = re.sub(r"\s*\|\s*LinkedIn.*$", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*-\s*LinkedIn.*$", "", t, flags=re.IGNORECASE)
        segments = re.split(r"\s*[-–|•]\s*", t)
        if segments and len(segments[0].strip()) >= 2:
            name_part = segments[0].strip()
            name_part = re.sub(r",\s*(?:MBA|PHD|PMP|MD|CPA|ESQ|SHRM-[A-Z]+|BSc|MSc).*$", "", name_part, flags=re.IGNORECASE)
            return name_part.strip()

    # Spotify Profile Titles:
    # "Spotify – Mohid Faraz", "Spotify - ahtisham", "Mohid Faraz on Spotify", "Mohid Faraz | Spotify"
    if platform == "spotify" or "spotify" in t.lower():
        t = re.sub(r"^Spotify\s*[-–—:|•·]\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*[-–—:|•·]\s*Spotify.*$", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s+on\s+Spotify.*$", "", t, flags=re.IGNORECASE)
        t = re.sub(r"^Listen\s+to\s+", "", t, flags=re.IGNORECASE)

    # Extract playlist creator if present e.g. "backseat - playlist by Mohid Faraz | Spotify"
    if "playlist by" in t.lower():
        m_pl = re.search(r"playlist by\s+([^|•–-]+)", t, re.IGNORECASE)
        if m_pl and len(m_pl.group(1).strip()) >= 2:
            return m_pl.group(1).strip()

    # Strip platform trailers & generic TikTok/Instagram/Pinterest/X titles
    t = re.sub(r"\s*[-–—|•·]\s*(?:Instagram|X|Twitter|Facebook|TikTok|Pinterest|Spotify|Photos and videos|Profile).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+on\s+(?:Instagram|Twitter|X|Facebook|TikTok|Pinterest|Spotify)\s*:?.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Photos?|Reels?|Videos?|Posts?)\s+by\s+", "", t, flags=re.IGNORECASE)

    # Strip handle in parentheses e.g. "Babar Dilawar (@dilawar)" -> "Babar Dilawar"
    t = re.sub(r"\s*\(@?[a-zA-Z0-9._-]+\)", "", t)
    if t.startswith("@") or t.lower() == handle.lower() or t.lower() == f"@{handle.lower()}":
        t = ""

    if "|" in t:
        t = t.split("|")[0].strip()
    if " - " in t or " – " in t or " — " in t:
        t = re.split(r"\s*[-–—]\s*", t)[0].strip()

    t = re.sub(r'[\"\'“”#]', '', t).strip(' -–—|•·:/')
    if len(t) > 40:
        t = t[:40].strip()

    tl = t.lower()
    reject_patterns = (
        "link to", "page not found", "welcome back", "log in", "sign up",
        "visit tiktok to discover profiles", "discover profiles", "watch trending",
        "web player", "see what", "profile", "music for everyone", "unsupported browser"
    )
    if not t or tl in ("spotify", "instagram", "facebook", "tiktok", "pinterest", "linkedin", "twitter", "x") or any(rej in tl for rej in reject_patterns) or len(t) < 2:
        return format_handle_to_name(handle, resolved_name)

    return t


def clean_bio_snippet(raw_snippet: str, platform: str, handle: str) -> str:
    """Clean and unescape bio snippets, preserving follower counts while removing boilerplate and directory spillovers."""
    if not raw_snippet:
        return f"{platform.title()} profile for @{handle.lstrip('@')}"
    s = unicodedata.normalize('NFKD', html.unescape(raw_snippet)).strip()
    s = re.sub(r"\s+", " ", s)
    # Only strip true list numbering like '1. ', '2) ', but preserve follower numbers like '12 Followers'
    s = re.sub(r"^\d+[\.\)\]]\s+", "", s).strip()

    if platform == "instagram":
        # Cut off bundled directory search spillover from other accounts
        for agg_marker in ["and discover other accounts you", "See photos and videos from friends on Instagram"]:
            idx = s.find(agg_marker)
            if idx != -1:
                s = s[:idx].strip()

        s = re.sub(r"\s*[-–—]?\s*See Instagram photos and videos from\s+[^@]+(?:\(@[a-zA-Z0-9._-]+\))?.*$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"\s*[-–—]?\s*See photos and videos from friends on Instagram.*$", "", s, flags=re.IGNORECASE).strip()
        s = s.strip(" .,-–—|•·:/")

    elif platform in ("twitter", "x"):
        s = re.sub(r"\s*See the latest conversations with\s+@?[a-zA-Z0-9._-]+.*$", "", s, flags=re.IGNORECASE).strip()
        s = s.strip(" .,-–—|•·:/")

    elif platform == "linkedin" or "linkedin" in s.lower():
        patterns = [
            r"^View\s+[^,]+(?:’s|'s)?\s*profile\s*on\s*LinkedIn[,\.\s]*(?:a\s+professional\s+community\s+of\s+[\d\w\s]+members\.?|the\s+world’s\s+largest\s+professional\s+community\.?|the\s+world's\s+largest\s+professional\s+community\.?)?\s*",
            r"a\s+professional\s+community\s+of\s+[\d\w\s]+members\.?",
            r"the\s+world(?:’|')s\s+largest\s+professional\s+community\.?",
            r"View\s+[^,\'’]+(?:’s|'s)?\s*profile\s*on\s*LinkedIn\.?",
            r"\b[A-Za-z0-9\s]+has\s+\d+\s+jobs?\s+listed\s+on\s+their\s+profile\.?",
            r"\bSee\s+the\s+complete\s+profile\s+on\s+LinkedIn\b\.?",
            r"\bJoin\s+LinkedIn\s+to\s+see\s+the\s+complete\s+profile\b\.?",
        ]
        for pat in patterns:
            s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
        s = s.strip(" .,-–|•·:/")

        # If LinkedIn bundled multiple directory profiles into one meta description, isolate the target person's block
        m_first_block = re.search(r"^(.*?connections\s+on\s+LinkedIn[\.,]*)", s, re.IGNORECASE)
        if m_first_block and len(m_first_block.group(1)) > 30:
            s = m_first_block.group(1).strip()
        elif len(s) > 260:
            s = s[:250].rsplit(" ", 1)[0] + "..."

    if any(bad in s.lower() for bad in ("the site owner hides", "link to facebook", "link to instagram", "welcome back", "log in", "unsupported browser", "join linkedin")):
        return f"{platform.title()} profile for @{handle.lstrip('@')}"

    if not s or len(s) < 3:
        return f"{platform.title()} profile for @{handle.lstrip('@')}"

    return s


# ==========================================
# 5. MULTI-ANCHOR SCORING & DISAMBIGUATION
# ==========================================
def score_candidate(
    platform_info: dict,
    title: str,
    snippet: str,
    all_variations: List[str],
    resolved_name: Optional[str],
    resolved_location: Optional[str],
    gh_username: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Tuple[int, List[str], dict, List[dict]]:
    """Score a candidate from 0 to 95 with Jaro-Winkler string similarity, sub-scores, evidence provenance, and disambiguation rules."""
    handle_score = 0
    name_score = 0
    company_score = 0
    location_score = 0
    reasons = []
    evidence = []

    handle = platform_info["handle"].lower()
    title_l = html.unescape(title or "").lower()
    snippet_l = html.unescape(snippet or "").lower()
    combined_text = f"{title_l} {snippet_l}"
    handle_norm = re.sub(r"[._-]", "", handle)
    plat = platform_info.get("platform", "")

    # 1. Verified GitHub handle match
    if gh_username:
        gh_clean = gh_username.lower().strip()
        gh_norm = re.sub(r"[._-]", "", gh_clean)
        if handle == gh_clean or handle_norm == gh_norm:
            handle_score = max(handle_score, 85)
            reasons.append(f"Direct match with verified GitHub handle (@{gh_username})")
            evidence.append({"type": "handle_match", "source": "github_verified", "value": f"@{gh_username}", "signal_strength": "strong"})
        elif len(gh_norm) >= 4 and (gh_norm in handle_norm or handle_norm in gh_norm):
            handle_score = max(handle_score, 70)
            reasons.append(f"Stem match with verified GitHub handle (@{gh_username})")
            evidence.append({"type": "handle_match", "source": "github_stem", "value": f"@{gh_username}", "signal_strength": "medium"})

    # 2. Handle matching
    for v in all_variations:
        vl = v.lower()
        vl_norm = re.sub(r"[._-]", "", vl)
        if handle == vl or handle_norm == vl_norm:
            if any(c.isdigit() for c in vl) or len(vl) >= 7:
                handle_score = max(handle_score, 75)
                reasons.append(f"Distinctive exact handle match (@{handle})")
                evidence.append({"type": "handle_match", "source": "email_pattern", "value": f"@{handle}", "signal_strength": "strong"})
            else:
                handle_score = max(handle_score, 55)
                reasons.append(f"Exact handle match (@{handle})")
                evidence.append({"type": "handle_match", "source": "email_pattern", "value": f"@{handle}", "signal_strength": "medium"})
            break
        elif len(vl_norm) >= 4 and (vl_norm in handle_norm or handle_norm in vl_norm):
            handle_score = max(handle_score, 45)
            reasons.append(f"Root stem handle match (@{handle})")
            evidence.append({"type": "handle_match", "source": "stem_pattern", "value": f"@{handle}", "signal_strength": "weak"})
            break

    # 3. LinkedIn vanity URL match
    if plat == "linkedin":
        slug_norm = re.sub(r"[-_.]", "", handle)
        if resolved_name:
            name_parts = [p.lower() for p in resolved_name.split() if len(p) >= 2]
            if len(name_parts) >= 2:
                first, last = name_parts[0], name_parts[-1]
                if slug_norm.startswith(f"{first}{last}") or slug_norm.startswith(f"{last}{first}"):
                    handle_score = max(handle_score, 80)
                    reasons.append(f"Direct LinkedIn vanity URL match (in/{handle})")
                    evidence.append({"type": "handle_match", "source": "linkedin_vanity", "value": f"in/{handle}", "signal_strength": "strong"})

    # 4. Name Matching (Exact, Inverted, and Jaro-Winkler >= 88%)
    target_name = resolved_name
    cand_extracted_name = clean_display_name(title, handle, plat, resolved_name)
    
    if target_name:
        name_parts = [p.lower() for p in target_name.split() if len(p) >= 2]
        if len(name_parts) >= 2:
            first, last = name_parts[0], name_parts[-1]
            rev_name = f"{last} {first}".lower()

            if target_name.lower() in title_l or rev_name in title_l:
                name_score = max(name_score, 40)
                reasons.append(f"Full name match in title ({target_name})")
                evidence.append({"type": "name_match", "source": "profile_title", "value": target_name, "signal_strength": "strong"})
            elif first in title_l and last in title_l:
                name_score = max(name_score, 35)
                reasons.append(f"First and last name in title ({first.title()} {last.title()})")
                evidence.append({"type": "name_match", "source": "profile_title", "value": f"{first.title()} {last.title()}", "signal_strength": "medium"})
            elif rev_name in combined_text or target_name.lower() in combined_text:
                name_score = max(name_score, 30)
                reasons.append(f"Full name match in bio ({target_name})")
                evidence.append({"type": "name_match", "source": "profile_bio", "value": target_name, "signal_strength": "weak"})
            elif cand_extracted_name:
                cand_parts = [p.lower() for p in cand_extracted_name.split() if len(p) >= 2]
                if len(cand_parts) >= 2:
                    c_first, c_last = cand_parts[0], cand_parts[-1]
                    jw_f = jaro_winkler_similarity(c_first, first)
                    jw_l = jaro_winkler_similarity(c_last, last)
                    jw_f_inv = jaro_winkler_similarity(c_first, last)
                    jw_l_inv = jaro_winkler_similarity(c_last, first)

                    if (jw_f >= 0.88 and jw_l >= 0.88) or (jw_f_inv >= 0.88 and jw_l_inv >= 0.88):
                        name_score = max(name_score, 25)
                        best_sim = max((jw_f + jw_l) / 2.0, (jw_f_inv + jw_l_inv) / 2.0)
                        reasons.append(f"Fuzzy name match (Jaro-Winkler {best_sim:.0%}: '{cand_extracted_name}' ~ '{target_name}')")
                        evidence.append({"type": "name_match", "source": "fuzzy_title", "value": cand_extracted_name, "signal_strength": "weak"})
                elif len(cand_parts) == 1:
                    c_single = cand_parts[0]
                    if c_single == first or jaro_winkler_similarity(c_single, first) >= 0.90:
                        name_score = max(name_score, 35)
                        reasons.append(f"First name match in profile ('{c_single.title()}')")
                        evidence.append({"type": "name_match", "source": "profile_name", "value": c_single.title(), "signal_strength": "medium"})
                    elif c_single == last or jaro_winkler_similarity(c_single, last) >= 0.90:
                        name_score = max(name_score, 30)
                        reasons.append(f"Surname match in profile ('{c_single.title()}')")
                        evidence.append({"type": "name_match", "source": "profile_name", "value": c_single.title(), "signal_strength": "weak"})

    # 5. Workplace / Company Corroboration
    if company_name and company_name.lower() in combined_text:
        company_score = 25
        reasons.append(f"Company corroboration ({company_name})")
        evidence.append({"type": "company_match", "source": "corporate_record", "value": company_name, "signal_strength": "strong"})

    # 6. Location Corroboration
    if resolved_location:
        loc_tokens = [tok.strip().lower() for tok in re.split(r"[,/]", resolved_location) if len(tok.strip()) >= 3]
        matched_locs = [l for l in loc_tokens if l in combined_text]
        if matched_locs:
            location_score = 15
            reasons.append(f"Location match ({', '.join([l.title() for l in matched_locs])})")
            evidence.append({"type": "location_match", "source": "location_record", "value": ', '.join([l.title() for l in matched_locs]), "signal_strength": "medium"})

    raw_score = handle_score + name_score + company_score + location_score
    final_score = raw_score

    # 7. Disambiguation Rules: Conflicting Given Name, Contradictory Surname, & Complete Mismatch Penalties
    if target_name and cand_extracted_name:
        name_parts = [p.lower() for p in target_name.split() if len(p) >= 2]
        cand_parts = [p.lower() for p in cand_extracted_name.split() if len(p) >= 2]
        if len(name_parts) >= 2 and len(cand_parts) >= 2:
            first, last = name_parts[0], name_parts[-1]
            c_first, c_last = cand_parts[0], cand_parts[-1]

            surname_match = (c_last == last or jaro_winkler_similarity(c_last, last) >= 0.90)
            first_match = (c_first == first or jaro_winkler_similarity(c_first, first) >= 0.90)
            given_conflict = (c_first != first and jaro_winkler_similarity(c_first, first) < 0.65 and len(c_first) >= 3 and len(first) >= 3)
            surname_conflict = (c_last != last and jaro_winkler_similarity(c_last, last) < 0.65 and len(c_last) >= 3 and len(last) >= 3)

            # Rule A: Conflicting given name (Haseeb Hameed vs Atisam Hameed) -> cap at 15
            if surname_match and given_conflict:
                final_score = min(final_score, 15)
                reasons.append(f"Conflicting given name penalty ({c_first.title()} vs {first.title()})")

            # Rule B: Contradictory Surname penalty (Ahtisham Khan vs Ahtisham Dilawar) -> cap at 35
            elif first_match and surname_conflict:
                final_score = min(final_score, 35)
                reasons.append(f"Contradictory surname penalty ({c_last.title()} vs {last.title()})")

            # Rule C: Complete Name Mismatch (Veronica Torralba Lozano vs Danielle Monaghan)
            # If neither first nor last name matches and handle doesn't match, this is a completely unrelated third-party profile!
            else:
                is_handle_exact = (handle in [v.lower() for v in all_variations])
                inv_first_match = (c_first == last or jaro_winkler_similarity(c_first, last) >= 0.90)
                inv_last_match = (c_last == first or jaro_winkler_similarity(c_last, first) >= 0.90)
                if not (surname_match or first_match or inv_first_match or inv_last_match) and not is_handle_exact:
                    return 0, [f"Unrelated profile: display name ('{cand_extracted_name}') does not match target name ('{target_name}')"], {}, []
        elif len(name_parts) >= 2 and len(cand_parts) == 1:
            first, last = name_parts[0], name_parts[-1]
            c_single = cand_parts[0]
            is_single_match = (c_single == first or c_single == last or jaro_winkler_similarity(c_single, first) >= 0.90 or jaro_winkler_similarity(c_single, last) >= 0.90)
            is_handle_exact = (handle in [v.lower() for v in all_variations])
            if not is_single_match and not is_handle_exact:
                return 0, [f"Unrelated profile: display name ('{cand_extracted_name}') does not match target name ('{target_name}')"], {}, []

    final_score = min(final_score, 95)
    sub_scores = {
        "handle_score": handle_score,
        "name_score": name_score,
        "company_score": company_score,
        "location_score": location_score,
        "final_score": max(0, final_score),
    }
    return max(0, final_score), reasons, sub_scores, evidence


# ==========================================
# 6. DIRECT 5-PLATFORM PROBERS
# ==========================================
CRAWLER_HEADERS = {
    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TWITTER_HEADERS = {
    "User-Agent": "Twitterbot/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def probe_instagram_profile(handle: str, client: httpx.AsyncClient, proxy_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("p", "reel", "reels", "explore", "direct", "accounts", "about", "developer"):
        return None
    url = f"https://www.instagram.com/{clean}/"
    
    resp = None
    # 1. High-speed direct probe with Twitterbot headers (most reliable for Instagram OpenGraph & avatar CDN)
    try:
        resp = await client.get(url, headers=TWITTER_HEADERS, timeout=4.0, follow_redirects=True)
    except Exception:
        resp = None

    # 2. Fallback to proxy if direct request failed
    if not resp or resp.status_code != 200:
        p_url = proxy_url or get_random_proxy_url()
        if p_url:
            try:
                async with httpx.AsyncClient(proxy=p_url, timeout=5.0, follow_redirects=True, verify=False) as px_client:
                    resp = await px_client.get(url, headers=TWITTER_HEADERS)
            except Exception:
                pass

    try:
        if resp and resp.status_code == 200:
            text = resp.text
            if any(bad in text for bad in ("Sorry, this page isn't available", "The link you followed may be broken", "Page Not Found")):
                return None
            
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else (soup.title.string.strip() if soup.title and soup.title.string else "")
            
            # Reject non-existent Instagram profiles (generic "Instagram" title, missing og:title)
            if not raw_title or raw_title.lower() in ("instagram", "login • instagram", "sign up • instagram", "page not found"):
                return None
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "static.xx.fbcdn" not in raw_img and "fb_icon" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            
            # If no real description and no authentic avatar, account is not valid
            if not raw_desc and not avatar_url:
                return None

            bio = clean_bio_snippet(raw_desc, "instagram", clean)
            display_name = clean_display_name(raw_title, clean, "instagram")

            print(f"[Prober] [INSTAGRAM] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "instagram",
                "platform_label": "Instagram",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_tiktok_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("explore", "direct", "trending", "about", "discover", "login", "live"):
        return None
    url = f"https://www.tiktok.com/@{clean}"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if any(bad in text for bad in ("Couldn't find this account", "UserNotExist", "page_not_found")):
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else (soup.title.string.strip() if soup.title and soup.title.string else "")
            
            # Reject generic placeholder pages for nonexistent accounts
            if not raw_title or raw_title.lower() in ("tiktok", "visit tiktok to discover profiles!", "discover profiles on tiktok"):
                return None

            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "static" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            
            # Non-existent accounts have the generic slogan without an authentic avatar
            if not avatar_url and (not raw_desc or "Watch, follow, and discover more trending content." in raw_desc):
                return None

            bio = clean_bio_snippet(raw_desc, "tiktok", clean)
            display_name = clean_display_name(raw_title, clean, "tiktok")

            print(f"[Prober] [TIKTOK] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "tiktok",
                "platform_label": "TikTok",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_pinterest_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("explore", "pin", "ideas", "business", "help", "about", "login", "today"):
        return None
    url = f"https://www.pinterest.com/{clean}/"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Profile not found" in text or "resource not found" in text.lower():
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else (soup.title.string.strip() if soup.title and soup.title.string else "")
            
            # Reject non-existent Pinterest placeholder profiles (empty title or generic "Pinterest")
            if not raw_title or raw_title.lower() in ("pinterest", "login • pinterest", "sign up • pinterest", "profile not found", "pinterest profile"):
                return None
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "default_280" not in raw_img and "default_open_graph" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "pinterest", clean)

            display_name = clean_display_name(raw_title, clean, "pinterest")

            print(f"[Prober] [PINTEREST] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "pinterest",
                "platform_label": "Pinterest",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_spotify_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._-]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 2 or clean.lower() in RESERVED_SYSTEM_SLUGS or clean.lower() in ("download", "search", "genre", "playlist", "track", "album", "artist", "user", "explore", "collection"):
        return None
    url = f"https://open.spotify.com/user/{clean}"
    try:
        resp = await client.get(url, headers=LI_CRAWLER_HEADERS, timeout=3.5, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Page not found" in text or "Something went wrong" in text:
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else (soup.title.string.strip() if soup.title and soup.title.string else "")
            
            # Reject non-existent placeholder / generic titles
            if not raw_title or raw_title.lower() in ("spotify", "spotify - web player", "spotify – web player", "page not found", "sign up", "log in"):
                return None
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = None
            if raw_img and raw_img.startswith("http") and not any(x in raw_img.lower() for x in ("default", "icon", "placeholder", "spotify-logo", "logo.png", "generic")):
                avatar_url = html.unescape(raw_img)
            
            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "spotify", clean)

            display_name = clean_display_name(raw_title, clean, "spotify")
            if not display_name or display_name.lower() in ("spotify", "web player"):
                display_name = format_handle_to_name(clean)

            print(f"[Prober] [SPOTIFY] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "spotify",
                "platform_label": "Spotify",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def search_spotify_users_pathfinder(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """
    Search Spotify for user profiles by display name using Spotify's internal Pathfinder GraphQL API.
    Uses SPOTIFY_CLIENT_TOKEN and SPOTIFY_AUTH_TOKEN from environment.
    Directly returns user profiles with exact 20-character IDs, display names, and CDN avatars.
    """
    load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)
    client_token = os.getenv("SPOTIFY_CLIENT_TOKEN", "").strip()
    auth_token = os.getenv("SPOTIFY_AUTH_TOKEN", "").strip()

    if not client_token or not auth_token:
        return []

    clean_query = query.strip()
    if not clean_query:
        return []

    url = "https://api-partner.spotify.com/pathfinder/v2/query"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en",
        "Referer": "https://open.spotify.com/",
        "Origin": "https://open.spotify.com",
        "Authorization": f"Bearer {auth_token}" if not auth_token.startswith("Bearer ") else auth_token,
        "client-token": client_token,
        "app-platform": "WebPlayer",
        "spotify-app-version": "1.3.4.71.gc1b8a0bfbc9b",
        "Content-Type": "application/json;charset=UTF-8",
    }
    payload = {
        "operationName": "searchUsers",
        "variables": {
            "searchTerm": clean_query,
            "offset": 0,
            "limit": limit,
            "numberOfTopResults": 20,
            "includeAudiobooks": True,
            "includeAuthors": False,
            "includeEpisodeContentRatingsV2": True,
            "includePreReleases": False,
            "includeAlbumPreReleases": False,
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "8f358dd82e62f61dd4ceaa9f8cd0889e644c9b707f1b724fbfb356a757cb7e5a",
            }
        },
    }

    try:
        r = await client.post(url, json=payload, headers=headers, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            users_block = (data.get("data", {}) or {}).get("searchV2", {}).get("users", {}) or {}
            items = users_block.get("items", []) or []
            results = []
            for it in items:
                u_data = it.get("data", {}) or {}
                uri = u_data.get("uri", "")
                uid = uri.replace("spotify:user:", "").strip()
                if not uid:
                    continue
                display_name = u_data.get("displayName", "").strip() or uid
                avatar_list = (u_data.get("avatar", {}) or {}).get("sources", [])
                avatar_url = avatar_list[0].get("url") if avatar_list else None

                results.append({
                    "platform": "spotify",
                    "platform_label": "Spotify",
                    "handle": uid,
                    "name": display_name,
                    "url": f"https://open.spotify.com/user/{uid}",
                    "avatar_url": avatar_url,
                    "snippet": f"Spotify profile for {display_name}",
                    "title": f"{display_name} on Spotify",
                    "discovery_method": "spotify_api",
                })
            print(f"[Spotify Pathfinder] Query '{clean_query}' → Found {len(results)} user profiles", flush=True)
            return results
        elif r.status_code == 401:
            print(f"[Spotify Pathfinder] ⚠️ HTTP 401: SPOTIFY_AUTH_TOKEN in .env has expired (Spotify Web tokens have a 1-hour TTL). Refresh the Bearer token in .env to resume direct user search.", flush=True)
        else:
            print(f"[Spotify Pathfinder] HTTP {r.status_code} for '{clean_query}': {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[Spotify Pathfinder] Error for '{clean_query}': {e}", flush=True)
    return []

async def probe_twitter_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9_]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("home", "explore", "search", "notifications", "settings", "i", "tos"):
        return None
    url = f"https://x.com/{clean}"
    try:
        resp = await client.get(url, headers=TWITTER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "This account doesn’t exist" in text or "account has been suspended" in text or "page doesn’t exist" in text:
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "pbs.twimg.com" in raw_img) else f"https://unavatar.io/x/{clean}"

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "twitter", clean)

            display_name = clean_display_name(raw_title, clean, "twitter")

            print(f"[Prober] [TWITTER] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "twitter",
                "platform_label": "X / Twitter",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_facebook_profile(handle: str, client: httpx.AsyncClient, proxy_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("sharer", "share", "login", "recover", "help", "policies", "privacy"):
        return None
    url = f"https://www.facebook.com/{clean}"
    
    resp = None
    p_url = proxy_url or get_random_proxy_url()
    if p_url:
        try:
            async with httpx.AsyncClient(proxy=p_url, timeout=5.0, follow_redirects=True, verify=False) as px_client:
                resp = await px_client.get(url, headers=TWITTER_HEADERS)
        except Exception:
            resp = None

    if not resp or resp.status_code != 200:
        try:
            resp = await client.get(url, headers=TWITTER_HEADERS, timeout=3.5, follow_redirects=True)
        except Exception:
            return None

    try:
        if resp and resp.status_code == 200:
            text = resp.text
            if any(bad in text for bad in ("This content isn't available right now", "Page Not Found", "You must log in")):
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else ""
            
            # If no og:title or generic "Facebook" title, account is not publicly confirmed
            if not raw_title or raw_title.lower() in ("facebook", "log in to facebook", "log into facebook", "welcome to facebook", "error"):
                return None

            # Extract authentic canonical URL & handle from og:url or final redirected resp.url
            og_url = soup.find("meta", property="og:url")
            canonical_url = og_url.get("content").strip() if (og_url and og_url.get("content")) else str(resp.url)
            m_handle = re.search(r"facebook\.com/([a-zA-Z0-9._-]+)/?$", canonical_url, re.IGNORECASE)
            if m_handle:
                c_cand = m_handle.group(1).rstrip("/")
                if c_cand.lower() not in ("profile.php", "pages", "people", "sharer", "share", "login"):
                    clean = c_cand
                    url = f"https://www.facebook.com/{clean}"
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "fb_icon" not in raw_img and "static.xx" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "facebook", clean)
            display_name = clean_display_name(raw_title, clean, "facebook")

            print(f"[Prober] [FACEBOOK] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "facebook",
                "platform_label": "Facebook",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None
    url = f"https://www.facebook.com/{clean}"
    try:
        resp = await client.get(url, headers=LI_CRAWLER_HEADERS, timeout=3.5, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if any(bad in text for bad in ("This content isn't available right now", "Page Not Found", "You must log in")):
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else ""
            
            # If no og:title or generic "Facebook" title, account is not publicly confirmed
            if not raw_title or raw_title.lower() in ("facebook", "log in to facebook", "log into facebook", "welcome to facebook"):
                return None

            # Extract authentic canonical URL & handle from og:url or final redirected resp.url
            og_url = soup.find("meta", property="og:url")
            canonical_url = og_url.get("content").strip() if (og_url and og_url.get("content")) else str(resp.url)
            m_handle = re.search(r"facebook\.com/([a-zA-Z0-9._-]+)/?$", canonical_url, re.IGNORECASE)
            if m_handle:
                c_cand = m_handle.group(1).rstrip("/")
                if c_cand.lower() not in ("profile.php", "pages", "people", "sharer", "share", "login"):
                    clean = c_cand
                    url = f"https://www.facebook.com/{clean}"
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "fb_icon" not in raw_img and "static.xx" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "facebook", clean)
            display_name = clean_display_name(raw_title, clean, "facebook")

            print(f"[Prober] [FACEBOOK] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "facebook",
                "platform_label": "Facebook",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
async def probe_github_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9_-]', '', handle).lstrip("@").strip("_-")
    if not clean or len(clean) < 1 or clean.lower() in ("features", "business", "explore", "marketplace", "pricing", "topics", "collections", "events"):
        return None
    url = f"https://github.com/{clean}"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Page not found" in text or "404 Not Found" in text:
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content").strip() if (og_title and og_title.get("content")) else (soup.title.string.strip() if soup.title and soup.title.string else "")
            
            if not raw_title or raw_title.lower() in ("github", "page not found", "built for developers"):
                return None

            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "avatars.githubusercontent.com" in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "github", clean)
            display_name = clean_display_name(raw_title, clean, "github")

            print(f"[Prober] [GITHUB] @{clean} -> [OK] Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "github",
                "platform_label": "GitHub",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


LI_CRAWLER_HEADERS = {
    "User-Agent": "Twitterbot/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

async def fetch_linkedin_candidate_avatar(url: str, client: httpx.AsyncClient) -> Tuple[Optional[str], Optional[str]]:
    """Extract authentic LinkedIn profile photo and canonical URL from public OpenGraph tags and HTML via Twitterbot crawler headers."""
    if not url or "linkedin.com/in/" not in url:
        return None, None
    try:
        resp = await client.get(url, headers=LI_CRAWLER_HEADERS, timeout=5.0, follow_redirects=True)
        canonical_url = str(resp.url) if resp else None
        avatar_url = None

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            og_url = soup.find("meta", property="og:url")
            if og_url and og_url.get("content"):
                canonical_url = og_url.get("content").strip()

            og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
            if og_img and og_img.get("content"):
                img_src = og_img.get("content").strip()
                if "licdn.com" in img_src and "ghost" not in img_src and "default_guest_profile" not in img_src:
                    avatar_url = img_src

            if not avatar_url:
                m = re.search(r'<meta\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)["\']', resp.text)
                if not m:
                    m = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']', resp.text)
                if m:
                    img_src = html.unescape(m.group(1)).strip()
                    if "licdn.com" in img_src and "ghost" not in img_src and "default_guest_profile" not in img_src:
                        avatar_url = img_src

        if not avatar_url:
            try:
                p_url = get_random_proxy_url()
                if p_url:
                    async with httpx.AsyncClient(proxy=p_url, timeout=5.0, verify=False) as p_client:
                        p_resp = await p_client.get(url, headers=LI_CRAWLER_HEADERS, follow_redirects=True)
                        if p_resp.status_code == 200:
                            p_soup = BeautifulSoup(p_resp.text, "html.parser")
                            p_img = p_soup.find("meta", property="og:image") or p_soup.find("meta", attrs={"name": "twitter:image"})
                            if p_img and p_img.get("content"):
                                img_src = p_img.get("content").strip()
                                if "licdn.com" in img_src and "ghost" not in img_src and "default_guest_profile" not in img_src:
                                    avatar_url = img_src
            except Exception:
                pass

        return avatar_url, canonical_url
    except Exception:
        pass
    return None, None


async def fetch_facebook_candidate_avatar(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Extract authentic Facebook profile photo from public OpenGraph tags via residential proxy routing."""
    if not url or "facebook.com/" not in url:
        return None
    resp = None
    p_url = get_random_proxy_url()
    if p_url:
        try:
            async with httpx.AsyncClient(proxy=p_url, timeout=5.0, follow_redirects=True, verify=False) as px_client:
                resp = await px_client.get(url, headers=TWITTER_HEADERS)
        except Exception:
            resp = None

    if not resp or resp.status_code != 200:
        try:
            resp = await client.get(url, headers=TWITTER_HEADERS, timeout=3.5, follow_redirects=True)
        except Exception:
            return None

    try:
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                img_src = og_img.get("content").strip()
                if "static.xx" not in img_src and "fb_icon" not in img_src:
                    return html.unescape(img_src)
    except Exception:
        pass
    return None



# ==========================================
# 7. DUCKDUCKGO DISTRIBUTED SEARCH ENGINE
# ==========================================
def _query_ddgs_sync(query: str, proxy_url: str, timeout: float = 4.5) -> List[Dict[str, str]]:
    """Execute DuckDuckGo search via residential proxy using official tokenized API session."""
    if DDGS is None:
        return []
    ddgs = DDGS(proxy=proxy_url, timeout=timeout)
    results = list(ddgs.text(query, max_results=10))
    items = []
    seen = set()
    for r in results:
        link = r.get("href", "")
        if link and link not in seen:
            if any(dom in link.lower() for dom in ("linkedin.com", "instagram.com", "facebook.com", "tiktok.com", "pinterest.com", "github.com", "x.com", "twitter.com", "spotify.com")):
                seen.add(link)
                items.append({
                    "link": link,
                    "title": html.unescape(r.get("title", "")),
                    "snippet": html.unescape(r.get("body", "")),
                })
    return items


def run_ddgs_auto_sync(q_str: str) -> List[Dict[str, str]]:
    """Fast failover using ddgs multi-engine browser impersonation."""
    if DDGS is None:
        return []
    try:
        ddgs = DDGS(timeout=4)
        results = None
        for b in ["google", "auto"]:
            try:
                res = list(ddgs.text(q_str, max_results=8, backend=b))
                if res:
                    results = res
                    break
            except Exception:
                continue
        if not results:
            return []
        items = []
        for r in results:
            if r.get("href"):
                items.append({
                    "link": r.get("href", ""),
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                })
        return items
    except Exception:
        return []


async def execute_ddg_html_query(
    query: str,
    primary_proxy_ip: str,
    max_retries: int = 1,
) -> Tuple[List[Dict[str, str]], str, int]:
    """
    Execute DuckDuckGo search query through a residential proxy IP with automatic failover.
    Utilizes DDGS tokenized session to prevent 202 JavaScript challenges.
    """
    available_ips = [primary_proxy_ip]
    fallback_pool = [ip for ip in proxy_pool.get_clean_ips() if ip != primary_proxy_ip]
    if fallback_pool:
        available_ips.extend(random.sample(fallback_pool, min(max_retries, len(fallback_pool))))

    items: List[Dict[str, str]] = []
    seen_links = set()
    used_ip = primary_proxy_ip
    attempts = 0

    for ip in available_ips[:2]:
        attempts += 1
        used_ip = ip
        proxy_url = (f"http://{PROXY_USER}:{PROXY_PASS}@{ip}" if (PROXY_USER and PROXY_PASS) else f"http://{ip}") if ip else None
        t0 = time.time()
        try:
            items = await asyncio.to_thread(_query_ddgs_sync, query, proxy_url, 4.5)
            elapsed_ms = int((time.time() - t0) * 1000)

            if items:
                proxy_pool.mark_healthy(ip, elapsed_ms)
                print(f"  [DDG] [OK] '{query[:35]}...' -> {len(items)} hits (via {ip} in {elapsed_ms}ms)", flush=True)
                return items, used_ip, attempts
            else:
                proxy_pool.mark_healthy(ip, elapsed_ms)
                print(f"  [DDG] [0 HITS] '{query[:35]}...' -> 0 hits (via {ip} in {elapsed_ms}ms)", flush=True)
                break
        except Exception as e:
            elapsed_ms = int((time.time() - t0) * 1000)
            err_msg = str(e).strip() or type(e).__name__
            if "202" in err_msg or "Ratelimit" in type(e).__name__:
                proxy_pool.mark_challenged(ip)
                print(f"  [DDG] [RATE-LIMIT] IP {ip} rate-limited. Retrying with fallback proxy...", flush=True)
            else:
                proxy_pool.latencies[ip] = max(proxy_pool.latencies.get(ip, 2000.0), float(elapsed_ms) * 1.5)
                print(f"  [DDG] [WARN] IP {ip} ({type(e).__name__}: {err_msg} in {elapsed_ms}ms). Retrying with fallback proxy...", flush=True)

    # Backup failover
    print(f"  [Failover] [BACKUP] Querying multi-engine backup for '{query[:35]}...'...", flush=True)
    fb_items = await asyncio.to_thread(run_ddgs_auto_sync, query)
    if fb_items:
        print(f"  [Failover] [OK] Retrieved {len(fb_items)} hits via backup failover", flush=True)
        for it in fb_items:
            if it["link"] and it["link"] not in seen_links:
                seen_links.add(it["link"])
                items.append(it)
    return items, used_ip, attempts


# ==========================================
# 8. MASTER HYBRID DISCOVERY ENGINE
# ==========================================
async def search_social_candidates(
    email: str,
    resolved_name: Optional[str] = None,
    resolved_location: Optional[str] = None,
    gh_username: Optional[str] = None,
    company_name: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    has_verified_linkedin: bool = False,
    **kwargs,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """
    Master candidate discovery engine combining:
    1. 5-platform high-speed direct probing with OpenGraph extraction.
    2. Focused DuckDuckGo search queries distributed across 5 distinct residential proxy IPs.
    3. Multi-anchor scoring with Jaro-Winkler string similarity and surname disambiguation.
    """
    local_part = email.split("@")[0].lower().strip() if "@" in email else ""
    inferred_first, inferred_last = split_compound_name(local_part)
    inferred_name = f"{inferred_first} {inferred_last}".strip() if (inferred_first and inferred_last) else (inferred_first or "")

    effective_name = resolved_name or (inferred_name if (inferred_name and len(inferred_name.split()) >= 2) else None)

    # Parse clean name tokens
    tokens = [p for p in re.findall(r"[a-zA-Z]+", effective_name or resolved_name or local_part)]
    if tokens and tokens[0].lower() in TITLE_PREFIXES and len(tokens) > 1:
        core_human_name = " ".join(p.capitalize() for p in tokens[1:])
    else:
        core_human_name = effective_name

    query_target = core_human_name if (core_human_name and len(core_human_name.split()) >= 2) else (effective_name or resolved_name or local_part)

    specific_handles, stem_handles = generate_handle_variations(email, effective_name or resolved_name, gh_username)
    probe_seeds = expand_social_probe_handles(specific_handles, stem_handles, effective_name or resolved_name)[:25]
    all_variations = specific_handles + stem_handles + probe_seeds

    print(f"\n[DDG Engine] ---------------------------------------------------", flush=True)
    print(f"[DDG Engine] Target: {email} | Inferred Name: '{effective_name or resolved_name}' | Query: '{query_target}'", flush=True)

    # 1. Build Direct Probe Tasks
    ig_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".") for s in probe_seeds if 3 <= len(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".")) <= 30))
    tt_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".") for s in probe_seeds if 2 <= len(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".")) <= 24))
    pin_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".") for s in probe_seeds if 3 <= len(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".")) <= 30))
    sp_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".") for s in probe_seeds if 3 <= len(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip(".")) <= 30))
    tw_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9_]', '_', s).lstrip("@").strip("_") for s in probe_seeds if 4 <= len(re.sub(r'[^a-zA-Z0-9_]', '_', s).lstrip("@").strip("_")) <= 15))
    fb_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9.]', '', s).lstrip("@").strip(".") for s in probe_seeds if 5 <= len(re.sub(r'[^a-zA-Z0-9.]', '', s).lstrip("@").strip(".")) <= 50))
    gh_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9_-]', '', s).lstrip("@").strip("_-") for s in probe_seeds if 1 <= len(re.sub(r'[^a-zA-Z0-9_-]', '', s).lstrip("@").strip("_-")) <= 39))

    limits = httpx.Limits(max_connections=60, max_keepalive_connections=25)
    async with httpx.AsyncClient(timeout=4.0, limits=limits, verify=False) as probe_client:
        probe_tasks = []
        for s in ig_seeds:
            probe_tasks.append(probe_instagram_profile(s, probe_client))
        for s in tt_seeds:
            probe_tasks.append(probe_tiktok_profile(s, probe_client))
        for s in pin_seeds:
            probe_tasks.append(probe_pinterest_profile(s, probe_client))
        for s in sp_seeds:
            probe_tasks.append(probe_spotify_profile(s, probe_client))
        for s in tw_seeds:
            probe_tasks.append(probe_twitter_profile(s, probe_client))
        for s in fb_seeds:
            probe_tasks.append(probe_facebook_profile(s, probe_client))
        if not gh_username:
            for s in gh_seeds:
                probe_tasks.append(probe_github_profile(s, probe_client))

        # 2. Build Focused DDG Search Queries & Spotify Pathfinder Search Queries
        clean_target = query_target.replace('"', '').strip()
        first_tok = tokens[0] if tokens else ""
        ddg_search_queries = [
            ("instagram", f'{clean_target} instagram'),
            ("facebook", f'site:facebook.com {clean_target}'),
            ("tiktok", f'{clean_target} tiktok'),
            ("pinterest", f'{clean_target} pinterest'),
            ("spotify", f'site:open.spotify.com/user/ {clean_target}'),
        ]
        if first_tok and len(first_tok) >= 3 and first_tok.lower() not in TITLE_PREFIXES and first_tok.lower() != clean_target.lower():
            ddg_search_queries.append(("instagram", f'{first_tok} instagram'))
            ddg_search_queries.append(("spotify", f'site:open.spotify.com/user/ {first_tok}'))

        if not has_verified_linkedin:
            ddg_search_queries.append(("linkedin", f'{clean_target} linkedin'))

        # Build direct Spotify Pathfinder GraphQL searches
        spotify_search_tasks = []
        if clean_target:
            spotify_search_tasks.append(search_spotify_users_pathfinder(clean_target, probe_client))
        if first_tok and len(first_tok) >= 3 and first_tok.lower() not in TITLE_PREFIXES and first_tok.lower() != clean_target.lower():
            spotify_search_tasks.append(search_spotify_users_pathfinder(first_tok, probe_client))

        # Assign each query its own distinct clean residential IP
        sampled_ips = proxy_pool.sample_distinct(len(ddg_search_queries))
        query_configs = [(plat, q, sampled_ips[i]) for i, (plat, q) in enumerate(ddg_search_queries)]

        print(f"[DDG Engine] Launching direct probes + Spotify GraphQL + {len(query_configs)} DDG queries...", flush=True)

        async def run_single_ddg(plat_tag: str, q_str: str, assigned_ip: str):
            hits, used_ip, attempts = await execute_ddg_html_query(q_str, assigned_ip)
            return plat_tag, hits

        query_tasks = [run_single_ddg(p, q, ip) for p, q, ip in query_configs]

        # 3. Concurrently execute all Probes, Spotify API searches, and DDG Queries
        t_start = time.time()
        probe_results_raw, spotify_results_raw, *query_results_raw = await asyncio.gather(
            asyncio.gather(*probe_tasks, return_exceptions=True),
            asyncio.gather(*spotify_search_tasks, return_exceptions=True),
            *query_tasks
        )
        discovery_elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[DDG Engine] All Probes & DDG searches completed in {discovery_elapsed_ms}ms", flush=True)

    candidates_map: Dict[str, Dict[str, Any]] = {}

    def make_candidate_dedup_key(p_plat: str, p_handle: str) -> str:
        h = p_handle.lstrip("@").strip().lower()
        if p_plat == "facebook":
            return f"facebook:{h.replace('.', '')}"
        return f"{p_plat}:{h}"

    # Ingest Direct Spotify User Search Hits (with CDN avatars & authentic display names)
    for sp_batch in spotify_results_raw:
        if not isinstance(sp_batch, list):
            continue
        for sp_cand in sp_batch:
            if not isinstance(sp_cand, dict) or not sp_cand.get("url"):
                continue
            h_clean = sp_cand["handle"].lstrip("@")
            dedup_key = make_candidate_dedup_key("spotify", h_clean)
            cand_name = sp_cand.get("name") or h_clean

            score, reasons, sub_scores, evidence = score_candidate(
                {"platform": "spotify", "platform_label": "Spotify", "handle": h_clean, "url": sp_cand["url"]},
                sp_cand.get("title") or f"{cand_name} on Spotify",
                sp_cand.get("snippet", ""),
                all_variations,
                effective_name or resolved_name,
                resolved_location,
                gh_username,
                company_name,
            )

            is_direct_exact = (cand_name.lower() == clean_target.lower())
            if not is_direct_exact and effective_name:
                is_direct_exact = (cand_name.lower() == effective_name.lower())

            if is_direct_exact:
                score = max(score, 80)
                reasons.append(f"Direct Spotify search exact display name match ('{cand_name}')")
                sub_scores["name_score"] = max(sub_scores.get("name_score", 0), 45)
                sub_scores["final_score"] = score
            elif score >= 35:
                score = max(score, 50)
                sub_scores["final_score"] = score
            elif score < 15:
                if first_tok and first_tok.lower() in cand_name.lower():
                    score = 45
                    reasons.append(f"Direct Spotify search user match ('{cand_name}')")
                    sub_scores = {"handle_score": 0, "name_score": 40, "company_score": 0, "location_score": 0, "final_score": 45}
                else:
                    continue

            cand_obj = {
                "platform": "spotify",
                "platform_label": "Spotify",
                "handle": f"@{h_clean}",
                "name": cand_name,
                "url": sp_cand["url"],
                "snippet": sp_cand.get("snippet", f"Spotify profile for {cand_name}"),
                "score": score,
                "confidence_badge": "",
                "confidence_level": "strong" if score >= 70 else "potential",
                "reasons": reasons,
                "sub_scores": sub_scores,
                "evidence": evidence,
                "avatar_url": sp_cand.get("avatar_url"),
                "discovery_method": "spotify_api"
            }
            if dedup_key not in candidates_map:
                candidates_map[dedup_key] = cand_obj
            else:
                existing = candidates_map[dedup_key]
                has_better_avatar = not existing.get("avatar_url") and cand_obj.get("avatar_url")
                if score > existing.get("score", 0) or has_better_avatar:
                    if not cand_obj.get("avatar_url") and existing.get("avatar_url"):
                        cand_obj["avatar_url"] = existing["avatar_url"]
                    candidates_map[dedup_key] = cand_obj

    # Ingest Probe Hits
    for p_cand in probe_results_raw:
        if not isinstance(p_cand, dict) or not p_cand.get("url"):
            continue
        plat = p_cand["platform"]
        h_clean = p_cand["handle"].lstrip("@")
        parsed = {
            "platform": plat,
            "platform_label": p_cand["platform_label"],
            "handle": h_clean,
            "url": p_cand["url"],
        }
        score, reasons, sub_scores, evidence = score_candidate(
            parsed,
            p_cand.get("title", ""),
            p_cand.get("snippet", ""),
            all_variations,
            effective_name or resolved_name,
            resolved_location,
            gh_username,
            company_name,
        )
        if score >= 15:
            dedup_key = make_candidate_dedup_key(plat, h_clean)
            cand_obj = {
                "platform": plat,
                "platform_label": p_cand["platform_label"],
                "handle": f"@{h_clean}",
                "name": p_cand.get("name") or h_clean,
                "url": p_cand["url"],
                "snippet": p_cand.get("snippet", ""),
                "score": score,
                "confidence_badge": "",
                "confidence_level": "strong" if score >= 70 else "potential",
                "reasons": reasons,
                "sub_scores": sub_scores,
                "evidence": evidence,
                "avatar_url": p_cand.get("avatar_url"),
                "discovery_method": "probing"
            }
            if dedup_key not in candidates_map:
                candidates_map[dedup_key] = cand_obj
            else:
                existing = candidates_map[dedup_key]
                has_better_avatar = not existing.get("avatar_url") and p_cand.get("avatar_url")
                has_better_dots = ("." in h_clean and "." not in existing.get("handle", ""))
                if score > existing.get("score", 0) or has_better_avatar or (score == existing.get("score", 0) and has_better_dots):
                    if not cand_obj.get("avatar_url") and existing.get("avatar_url"):
                        cand_obj["avatar_url"] = existing["avatar_url"]
                    candidates_map[dedup_key] = cand_obj

    # Ingest DDG Search Hits
    for q_res in query_results_raw:
        if not isinstance(q_res, tuple) or len(q_res) != 2:
            continue
        plat_tag, items = q_res
        for it in items:
            link = it.get("link", "")
            title = it.get("title", "")
            snippet = it.get("snippet", "")
            parsed = parse_social_url(link)
            if not parsed:
                continue

            plat = parsed["platform"]
            h_clean = parsed["handle"].lstrip("@")
            dedup_key = make_candidate_dedup_key(plat, h_clean)

            score, reasons, sub_scores, evidence = score_candidate(
                parsed,
                title,
                snippet,
                all_variations,
                effective_name or resolved_name,
                resolved_location,
                gh_username,
                company_name,
            )
            if score < 15:
                if plat == "spotify":
                    score = 30
                    reasons = [f"Discovered via Spotify user profile search ({clean_target})"]
                    sub_scores = {"handle_score": 0, "name_score": 30, "company_score": 0, "location_score": 0, "final_score": 30}
                else:
                    continue

            display_name = clean_display_name(title, h_clean, plat, effective_name or resolved_name)
            bio_clean = clean_bio_snippet(snippet, plat, h_clean)

            if dedup_key not in candidates_map:
                candidates_map[dedup_key] = {
                    "platform": plat,
                    "platform_label": parsed["platform_label"],
                    "handle": f"@{h_clean}",
                    "name": display_name or h_clean,
                    "url": parsed["url"],
                    "snippet": bio_clean,
                    "score": score,
                    "confidence_badge": "",
                    "confidence_level": "strong" if score >= 70 else "potential",
                    "reasons": reasons,
                    "sub_scores": sub_scores,
                    "evidence": evidence,
                    "avatar_url": None,
                    # DDG-discovered Spotify candidates need post-probe validation.
                    # Enrichment must confirm the display name matches the target;
                    # if enrichment fails or is skipped this flag keeps them removable.
                    "_sp_ddg": plat == "spotify" and score <= 30,
                }
            else:
                existing = candidates_map[dedup_key]
                existing_avatar = existing.get("avatar_url")
                has_better_dots = ("." in h_clean and "." not in existing.get("handle", ""))
                if score > existing.get("score", 0) or (score == existing.get("score", 0) and has_better_dots):
                    candidates_map[dedup_key] = {
                        "platform": plat,
                        "platform_label": parsed["platform_label"],
                        "handle": f"@{h_clean}",
                        "name": display_name or h_clean,
                        "url": parsed["url"],
                        "snippet": bio_clean,
                        "score": max(score, existing.get("score", 0)),
                        "confidence_badge": "",
                        "confidence_level": "strong" if max(score, existing.get("score", 0)) >= 70 else "potential",
                        "reasons": reasons,
                        "sub_scores": sub_scores,
                        "evidence": evidence,
                        "avatar_url": existing_avatar,
                    }

    # Concurrent Avatar Enrichment (LinkedIn + Instagram + Facebook + Spotify candidates via crawler headers)
    enrich_tasks = []
    li_count = 0
    ig_count = 0
    fb_count = 0
    sp_count = 0
    for c in candidates_map.values():
        if not c.get("avatar_url"):
            if c["platform"] == "linkedin" and li_count < 15:
                enrich_tasks.append(("linkedin", c))
                li_count += 1
            elif c["platform"] == "instagram" and ig_count < 15:
                enrich_tasks.append(("instagram", c))
                ig_count += 1
            elif c["platform"] == "facebook" and fb_count < 8:
                enrich_tasks.append(("facebook", c))
                fb_count += 1
            elif c["platform"] == "spotify" and sp_count < 15:
                enrich_tasks.append(("spotify", c))
                sp_count += 1

    if enrich_tasks:
        async def enrich_candidate(plat, c, http_client):
            if plat == "linkedin":
                av, canon_url = await fetch_linkedin_candidate_avatar(c["url"], http_client)
                if av:
                    c["avatar_url"] = av
                if canon_url and "linkedin.com/in/" in canon_url:
                    m_slug = re.search(r"linkedin\.com/in/([a-zA-Z0-9_/%-]+)", canon_url, re.IGNORECASE)
                    if m_slug:
                        canon_slug = m_slug.group(1).split("?")[0].rstrip("/")
                        if canon_slug and canon_slug.lower() not in ("dir", "pub", "feed"):
                            c["handle"] = f"@{canon_slug}"
                            c["url"] = f"https://www.linkedin.com/in/{canon_slug}"
            elif plat == "instagram":
                h_slug = c["handle"].lstrip("@").strip()
                ig_data = await probe_instagram_profile(h_slug, http_client)
                if ig_data:
                    if ig_data.get("avatar_url"):
                        c["avatar_url"] = ig_data["avatar_url"]
                    if ig_data.get("name") and (not c.get("name") or c["name"] == c["handle"].lstrip("@")):
                        c["name"] = ig_data["name"]
                    if ig_data.get("snippet") and not c.get("snippet"):
                        c["snippet"] = ig_data["snippet"]
            elif plat == "facebook":
                av = await fetch_facebook_candidate_avatar(c["url"], http_client)
                if av:
                    c["avatar_url"] = av
            elif plat == "spotify":
                h_slug = c["handle"].lstrip("@").strip()
                sp_data = await probe_spotify_profile(h_slug, http_client)
                if sp_data:
                    if sp_data.get("avatar_url"):
                        c["avatar_url"] = sp_data["avatar_url"]
                    if sp_data.get("name") and (not c.get("name") or c["name"] == c["handle"].lstrip("@") or c["name"] == "on Spotify"):
                        c["name"] = sp_data["name"]
                    if sp_data.get("snippet") and not c.get("snippet"):
                        c["snippet"] = sp_data["snippet"]
                    # Re-score candidate with the authentic probed profile name
                    n_score, n_reasons, n_sub, n_ev = score_candidate(
                        {"platform": "spotify", "platform_label": "Spotify", "handle": h_slug, "url": c["url"]},
                        c["name"],
                        c.get("snippet", ""),
                        all_variations,
                        effective_name or resolved_name,
                        resolved_location,
                        gh_username,
                        company_name,
                    )
                    if n_score >= 15:
                        c["score"] = n_score
                        c["reasons"] = n_reasons
                        c["sub_scores"] = n_sub
                        c["evidence"] = n_ev
                        c["confidence_level"] = "strong" if n_score >= 70 else "potential"
                    elif c.get("score", 0) <= 30:
                        # DDG-discovered candidate; probed name doesn't match target → flag for removal
                        c["_remove"] = True
                else:
                    # Probe returned nothing; DDG-discovered candidates are noise → remove
                    if c.get("score", 0) <= 30:
                        c["_remove"] = True

        async with httpx.AsyncClient(timeout=4.0, verify=False) as av_client:
            await asyncio.gather(*[enrich_candidate(plat, c, av_client) for plat, c in enrich_tasks], return_exceptions=True)

    # Purge Spotify candidates that failed post-probe validation (irrelevant DDG noise).
    # Two removal conditions:
    #  1. _remove=True  → enrichment probed and found name doesn't match target
    #  2. _sp_ddg=True AND score ≤ 30 → enrichment was skipped (limit/exception) so
    #     the DDG-discovered candidate was never name-validated; remove as untrusted
    for dk in list(candidates_map.keys()):
        c = candidates_map[dk]
        if c.get("_remove") or (c.get("_sp_ddg") and c.get("score", 0) <= 30):
            del candidates_map[dk]

    # Post-enrichment cross-candidate deduplication pass:
    # Merge duplicate candidate entries that share the same canonical URL or identical profile avatar photo
    merged_candidates: Dict[str, Dict[str, Any]] = {}
    for c in candidates_map.values():
        plat = c["platform"]
        h_clean = c["handle"].lstrip("@").strip().lower()
        p_key = f"{plat}:{h_clean}"
        av_key = f"{plat}:av:{c['avatar_url']}" if c.get("avatar_url") else None

        existing_key = None
        if av_key and av_key in merged_candidates:
            existing_key = av_key
        elif p_key in merged_candidates:
            existing_key = p_key

        if existing_key is None:
            merged_candidates[p_key] = c
            if av_key:
                merged_candidates[av_key] = c
        else:
            existing = merged_candidates[existing_key]
            # Merge: Keep the richer snippet, higher score, and authentic avatar
            has_better_avatar = not existing.get("avatar_url") and c.get("avatar_url")
            has_richer_snippet = len(c.get("snippet", "")) > len(existing.get("snippet", "")) and not "profile for @" in c.get("snippet", "")
            if c["score"] > existing["score"] or has_better_avatar or (c["score"] == existing["score"] and has_richer_snippet):
                if not c.get("avatar_url") and existing.get("avatar_url"):
                    c["avatar_url"] = existing.get("avatar_url")
                merged_candidates[p_key] = c
                if av_key:
                    merged_candidates[av_key] = c
                if existing_key != p_key:
                    merged_candidates[existing_key] = c

    unique_candidates = list({id(v): v for v in merged_candidates.values()}.values())
    all_candidates = sorted(unique_candidates, key=lambda x: -x["score"])

    # Group by platform in priority order: LinkedIn -> GitHub -> Instagram -> Facebook -> X -> Pinterest -> TikTok -> Spotify
    by_platform = {
        "linkedin": [c for c in all_candidates if c["platform"] == "linkedin"],
        "github": [c for c in all_candidates if c["platform"] == "github"],
        "instagram": [c for c in all_candidates if c["platform"] == "instagram"],
        "facebook": [c for c in all_candidates if c["platform"] == "facebook"],
        "twitter": [c for c in all_candidates if c["platform"] == "twitter"],
        "pinterest": [c for c in all_candidates if c["platform"] == "pinterest"],
        "tiktok": [c for c in all_candidates if c["platform"] == "tiktok"],
        "spotify": [c for c in all_candidates if c["platform"] == "spotify"],
    }

    total_count = sum(len(v) for v in by_platform.values())
    print(f"[Social Discovery] [OK] Discovery Complete! Total Unique Ranked Candidates: {len(all_candidates)}", flush=True)
    if all_candidates:
        top = all_candidates[0]
        print(f"[Social Discovery] Top Match: [{top['platform'].upper()}] {top['handle']} ({top['name']}) -> Score: {top['score']}%", flush=True)
    print(f"[Social Discovery] ---------------------------------------------------\n", flush=True)

    return all_candidates[:40], by_platform
