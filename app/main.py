"""
main.py
-------
Flask application - entry point for the Email Lookup backend.
Run with: flask --app app.main run --port 5000
Or production: waitress-serve --port=5000 wsgi:app
"""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

# Ensure app directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)

from flask import Flask, request, jsonify
from flask_cors import CORS

from models import (
    LookupRequest, LookupResponse, PersonInfo, PlatformResult,
    CacheInvalidateRequest, CacheInvalidateResponse,
    VerifyRequest, VerifyResponse, PortCheckResponse,
)
from lookup_engine import run_lookup
from smtp_verifier import verify_email_smtp, check_port25
from platform_checker import check_platforms
from cache import (
    init_db, get_lookup_cache, set_lookup_cache,
    delete_lookup_cache, get_verify_cache, set_verify_cache,
)

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Ensure database tables exist at startup
with app.app_context():
    try:
        asyncio.run(init_db())
    except Exception as e:
        print(f"Database initialization error: {e}", flush=True)


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "status": "online",
        "service": "Email Lookup API (Flask)",
        "version": "1.0.0",
        "health": "/api/health"
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


@app.route("/api/port-check", methods=["GET"])
def port_check():
    """Check if outbound port 25 is available (not ISP-blocked)."""
    available = check_port25()
    resp = PortCheckResponse(
        port25_available=available,
        message=(
            "Port 25 is open - full direct SMTP verification active."
            if available
            else "Port 25 is restricted by ISP/network. Using API fallback if configured."
        ),
    )
    return jsonify(resp.model_dump())


@app.route("/api/lookup", methods=["POST"])
async def email_lookup():
    """
    Reverse email lookup - takes an email, returns all public info we can find.
    Results are cached for 24 hours.
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        req = LookupRequest.model_validate(data)
    except Exception as e:
        return jsonify({"detail": f"Validation error: {e}"}), 422

    email = req.email.lower().strip()
    if not EMAIL_REGEX.match(email):
        return jsonify({
            "detail": "Invalid email address syntax. Please enter a valid email (e.g. name@company.com)."
        }), 422

    start_time = time.time()

    # Check cache first unless force_refresh is requested
    if not req.force_refresh:
        cached = await get_lookup_cache(email)
        if cached:
            cached["cached"] = True
            cached["query_time_ms"] = max(1, int((time.time() - start_time) * 1000))
            if "social_candidates_by_platform" in cached and isinstance(cached["social_candidates_by_platform"], dict):
                cached["social_candidates_by_platform"].setdefault("spotify", [])
            return jsonify(cached)

    try:
        lookup_result = await run_lookup(email)
    except Exception as e:
        return jsonify({"detail": f"Lookup failed: {e}"}), 500

    platform_results = []
    profiles_found = lookup_result.get("profiles", {})
    person_data = lookup_result.get("person", {})

    for p in platform_results:
        p_name = p.get("name", "").lower()
        if p_name == "github" and profiles_found.get("github"):
            p["found"] = True
            p["url"] = profiles_found["github"].get("url") if isinstance(profiles_found["github"], dict) else None
        elif p_name == "gravatar" and (person_data.get("avatar") or "").startswith("https://www.gravatar.com"):
            p["found"] = True
        elif p_name == "linkedin" and profiles_found.get("linkedin"):
            p["found"] = True
            p["url"] = profiles_found.get("linkedin")

    person = PersonInfo(
        name=person_data.get("name"),
        avatar=person_data.get("avatar"),
        bio=person_data.get("bio"),
        location=person_data.get("location"),
        website=person_data.get("website"),
    )

    platforms = [
        PlatformResult(
            name=p["name"],
            found=p["found"],
            icon=p["icon"],
            url=p.get("url"),
        )
        for p in platform_results
    ]

    response = LookupResponse(
        email=email,
        query_time_ms=lookup_result.get("query_time_ms", 0),
        email_type=lookup_result.get("email_type", "personal"),
        domain=lookup_result.get("domain"),
        person=person,
        profiles=lookup_result.get("profiles", {}),
        platforms=platforms,
        phone=lookup_result.get("phone"),
        address=None,
        deliverability=lookup_result.get("deliverability"),
        autocorrect=lookup_result.get("autocorrect"),
        company=lookup_result.get("company"),
        social_candidates=lookup_result.get("social_candidates", []),
        social_candidates_by_platform=lookup_result.get("social_candidates_by_platform", {}),
    )

    # Cache the result
    await set_lookup_cache(email, response.model_dump())
    return jsonify(response.model_dump())


@app.route("/api/cache/invalidate", methods=["POST"])
async def invalidate_cache():
    """Purge cached lookup result for the specified email to force a fresh live lookup."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        req = CacheInvalidateRequest.model_validate(data)
    except Exception as e:
        return jsonify({"detail": f"Validation error: {e}"}), 422

    email = req.email.lower().strip()
    if not email:
        return jsonify({"detail": "Email is required."}), 422

    success = await delete_lookup_cache(email)
    resp = CacheInvalidateResponse(
        success=success,
        email=email,
        message="Cache entry successfully purged." if success else "No cache entry found or error purging."
    )
    return jsonify(resp.model_dump())


@app.route("/api/verify", methods=["POST"])
async def email_verify():
    """Email verifier - performs SMTP handshake or uses AbstractAPI fallback."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        req = VerifyRequest.model_validate(data)
    except Exception as e:
        return jsonify({"detail": f"Validation error: {e}"}), 422

    email = req.email.lower().strip()
    if not EMAIL_REGEX.match(email):
        return jsonify({
            "detail": "Invalid email address syntax. Please enter a valid email (e.g. name@company.com)."
        }), 422

    cached = await get_verify_cache(email)
    if cached:
        return jsonify(cached)

    result = await asyncio.to_thread(verify_email_smtp, email)
    await set_verify_cache(email, result)
    return jsonify(result)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "true").lower() in ("true", "1", "yes")
    app.run(host=host, port=port, debug=debug)