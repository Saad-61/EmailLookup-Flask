"""
platform_checker.py
-------------------
Silently checks whether an email is registered on various platforms
by probing their "forgot password" / account-check endpoints.

Technique inspired by Holehe (github.com/megadose/holehe).
No actual password-reset emails are sent to the target.
"""

import asyncio
import httpx
import time
from typing import List

# ── Platform definitions ──────────────────────────────────────────────────────
# Each entry: name, icon slug, check function name, url, method, payload key,
# positive keyword in response body, and optional header overrides.

PLATFORMS = [
    {
        "name": "GitHub",
        "icon": "github",
        "method": "GET",
        "url": "https://api.github.com/search/users?q={email}",
        "check": "github_api",          # handled separately
    },
    {
        "name": "Spotify",
        "icon": "spotify",
        "method": "POST",
        "url": "https://accounts.spotify.com/en/password-reset",
        "payload": {"username": "{email}"},
        "positive": "we'll send you an email",
        "negative": "couldn't find",
    },
    {
        "name": "Adobe",
        "icon": "adobe",
        "method": "GET",
        "url": "https://authservice.adobe.com/prereg?client_id=homepage_milo_mena&email={email}",
        "positive": "found",
        "negative": "not_found",
    },
    {
        "name": "Dropbox",
        "icon": "dropbox",
        "method": "POST",
        "url": "https://www.dropbox.com/lp/account_identifier_exists",
        "payload": {"account_identifier": "{email}", "is_business_user": "false"},
        "positive": "true",
        "negative": "false",
    },
    {
        "name": "Discord",
        "icon": "discord",
        "method": "POST",
        "url": "https://discord.com/api/v9/auth/forgot",
        "payload": {"login": "{email}"},
        "positive": "{}", 
        "negative": "Unknown Account",
    },
    {
        "name": "Duolingo",
        "icon": "duolingo",
        "method": "GET",
        "url": "https://www.duolingo.com/2017-06-30/users?email={email}",
        "positive": "users",
        "negative": "\"users\":[]",
    },
    {
        "name": "Pinterest",
        "icon": "pinterest",
        "method": "GET",
        "url": "https://www.pinterest.com/_ngjs/user/exists/?username={local}",
        "positive": "\"username_available\":false",
        "negative": "username_available\":true",
        "use_local": True,
    },
    {
        "name": "Patreon",
        "icon": "patreon",
        "method": "POST",
        "url": "https://www.patreon.com/api/auth?include=campaign%2Cuser-newsletter-subscriptions&json-api-version=1.0&json-api-use-default-includes=false",
        "payload": {"data": {"type": "user", "attributes": {"email": "{email}", "password": "DUMMY_SKIP_CHECK"}}},
        "positive": "email_address_already_taken",
        "negative": "",
    },
    {
        "name": "Gravatar",
        "icon": "gravatar",
        "check": "gravatar_exists",   # handled separately using SHA256
    },
    {
        "name": "Tumblr",
        "icon": "tumblr",
        "method": "GET",
        "url": "https://www.tumblr.com/api/v2/email/check?email={email}",
        "positive": "\"exists\":true",
        "negative": "\"exists\":false",
    },
    {
        "name": "Archive.org",
        "icon": "archive",
        "method": "POST",
        "url": "https://archive.org/account/login",
        "payload": {"username": "{email}", "password": "DUMMY_INVALID"},
        "positive": "incorrect password",
        "negative": "cannot find",
    },
    {
        "name": "Airbnb",
        "icon": "airbnb",
        "method": "POST",
        "url": "https://www.airbnb.com/api/v2/authentications",
        "payload": {"email": "{email}", "password": "DUMMY"},
        "headers": {"X-Airbnb-API-Key": "d306zoyjsyarp7usu3tq"},
        "positive": "password",
        "negative": "email_address",
    },
    {
        "name": "Reddit",
        "icon": "reddit",
        "method": "GET",
        "url": "https://www.reddit.com/api/username_available.json?user={local}",
        "positive": "false",
        "negative": "true",
        "use_local": True,
    },
]

# ── Common headers to look like a real browser ────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Individual check helpers ──────────────────────────────────────────────────

async def _check_github_api(email: str, client: httpx.AsyncClient, github_token: str) -> bool:
    """Check via GitHub user search API."""
    headers = {**BROWSER_HEADERS}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    try:
        resp = await client.get(
            f"https://api.github.com/search/users?q={email}",
            headers=headers,
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("total_count", 0) > 0
    except Exception:
        pass
    return False


async def _check_gravatar_exists(email: str, client: httpx.AsyncClient) -> bool:
    """Check if a Gravatar profile exists for this email."""
    import hashlib
    email_hash = hashlib.sha256(email.lower().strip().encode()).hexdigest()
    try:
        resp = await client.get(
            f"https://www.gravatar.com/avatar/{email_hash}?d=404",
            headers=BROWSER_HEADERS,
            timeout=6,
        )
        return resp.status_code == 200
    except Exception:
        return False


async def _check_generic(platform: dict, email: str, client: httpx.AsyncClient) -> bool:
    """Generic platform checker using forgot-password / account-check endpoints."""
    local = email.split("@")[0] if "@" in email else email

    url_template = platform.get("url", "")
    url = url_template.replace("{email}", email).replace("{local}", local)

    method = platform.get("method", "GET").upper()
    positive = platform.get("positive", "")
    negative = platform.get("negative", "")
    extra_headers = platform.get("headers", {})

    headers = {**BROWSER_HEADERS, **extra_headers}

    try:
        if method == "GET":
            resp = await client.get(url, headers=headers, timeout=8, follow_redirects=True)
        else:
            payload_template = platform.get("payload", {})
            # Deep-substitute email into payload values
            payload = _fill_payload(payload_template, email)
            resp = await client.post(url, json=payload, headers=headers, timeout=8)

        body = resp.text.lower()

        if positive and positive.lower() in body:
            return True
        if negative and negative.lower() in body:
            return False
    except Exception:
        pass
    return False


def _fill_payload(template, email: str):
    """Recursively replace {email} in payload dict values."""
    if isinstance(template, dict):
        return {k: _fill_payload(v, email) for k, v in template.items()}
    if isinstance(template, str):
        return template.replace("{email}", email)
    return template


# ── Main entry point ──────────────────────────────────────────────────────────

async def check_platforms(email: str, github_token: str = "") -> List[dict]:
    """
    Run all platform checks concurrently.
    Returns list of {name, icon, found, url} dicts.
    """
    results = []

    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        platform_list = []

        for p in PLATFORMS:
            check_type = p.get("check", "generic")

            if check_type == "github_api":
                tasks.append(_check_github_api(email, client, github_token))
            elif check_type == "gravatar_exists":
                tasks.append(_check_gravatar_exists(email, client))
            else:
                tasks.append(_check_generic(p, email, client))

            platform_list.append(p)

        check_results = await asyncio.gather(*tasks, return_exceptions=True)

        for platform, found in zip(platform_list, check_results):
            if isinstance(found, Exception):
                found = False
            results.append({
                "name": platform["name"],
                "icon": platform["icon"],
                "found": bool(found),
                "url": None,
            })

    return results
