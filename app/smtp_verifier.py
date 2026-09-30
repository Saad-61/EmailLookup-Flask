import socket
import smtplib
import time
import os
import dns.resolver
from typing import Tuple, Optional

# ── helpers ──────────────────────────────────────────────────────────────────

def check_port25() -> bool:
    """Return True if outbound port 25 is reachable (not ISP-blocked)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        result = s.connect_ex(("aspmx.l.google.com", 25))
        s.close()
        return result == 0
    except Exception:
        return False


def get_mx_records(domain: str) -> list[str]:
    """Resolve MX records for a domain, ordered by priority."""
    try:
        records = dns.resolver.resolve(domain, "MX")
        sorted_records = sorted(records, key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".") for r in sorted_records]
    except Exception:
        return []


def detect_mx_provider(mx_host: str) -> str:
    """Identify the mail provider from an MX hostname."""
    mx_lower = mx_host.lower()
    if "google" in mx_lower or "gmail" in mx_lower or "googlemail" in mx_lower:
        return "Google Workspace"
    if "outlook" in mx_lower or "microsoft" in mx_lower or "hotmail" in mx_lower:
        return "Microsoft 365"
    if "yahoodns" in mx_lower or "yahoo" in mx_lower:
        return "Yahoo Mail"
    if "amazonses" in mx_lower:
        return "Amazon SES"
    if "mimecast" in mx_lower:
        return "Mimecast"
    if "protonmail" in mx_lower:
        return "ProtonMail"
    if "zoho" in mx_lower:
        return "Zoho Mail"
    if "mailgun" in mx_lower:
        return "Mailgun"
    if "sendgrid" in mx_lower:
        return "SendGrid"
    return "Custom / Unknown"


def get_smtp_sender_config() -> Tuple[str, str]:
    """
    Get sender email and HELO hostname from environment variables,
    falling back to sensible defaults.
    """
    sender = os.getenv("SMTP_SENDER_EMAIL", "").strip()
    if not sender:
        sender = "probe@emailverify.local"

    helo = os.getenv("SMTP_HELO_HOST", "").strip()
    if not helo:
        if "@" in sender and "." in sender.split("@")[-1]:
            helo = f"mail.{sender.split('@')[-1]}"
        else:
            helo = "mail.emailverify.local"

    return sender, helo


# ── catch-all detection ───────────────────────────────────────────────────────

def _is_catchall(mx_host: str, domain: str, from_addr: str, helo_host: str = "mail.emailverify.local") -> Optional[bool]:
    """
    Send an SMTP probe for a randomly generated address.
    Returns True if catch-all, False if not, None if inconclusive.
    """
    random_addr = f"xq91zz_nonexistent_probe_99@{domain}"
    try:
        with smtplib.SMTP(timeout=10) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo(helo_host)
            smtp.mail(from_addr)
            code, _ = smtp.rcpt(random_addr)
            smtp.quit()
            # 250 = accepted (catch-all), 550/551/552/553 = rejected (not catch-all)
            if code == 250:
                return True
            elif code in (550, 551, 552, 553):
                return False
    except smtplib.SMTPConnectError:
        pass
    except Exception:
        pass
    return None


# ── main verifier ─────────────────────────────────────────────────────────────

def verify_email_smtp(email: str) -> dict:
    """
    Full SMTP verification pipeline.
    Returns a dict matching VerifyResponse schema.
    """
    start = time.time()
    email = email.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""

    base = {
        "email": email,
        "valid": None,
        "catchall": None,
        "mx_provider": None,
        "mx_record": None,
        "confidence": None,
        "response_time_ms": None,
        "port25_available": False,
        "method_used": "smtp",
        "error": None,
    }

    if not domain or "@" not in email:
        base["error"] = "Invalid email format"
        base["response_time_ms"] = int((time.time() - start) * 1000)
        return base

    # 1. MX records
    mx_hosts = get_mx_records(domain)
    if not mx_hosts:
        base["valid"] = False
        base["error"] = f"No MX records found for domain '{domain}'"
        base["response_time_ms"] = int((time.time() - start) * 1000)
        return base

    mx_host = mx_hosts[0]
    base["mx_record"] = mx_host
    base["mx_provider"] = detect_mx_provider(mx_host)

    # 2. Port 25 check
    port_open = check_port25()
    base["port25_available"] = port_open

    if not port_open:
        # Fallback: AbstractAPI
        abstract_key = os.getenv("ABSTRACT_API_KEY", "")
        if abstract_key:
            result = _verify_via_abstract_api(email, abstract_key)
            result["mx_record"] = mx_host
            result["mx_provider"] = base["mx_provider"]
            result["port25_available"] = False
            result["response_time_ms"] = int((time.time() - start) * 1000)
            return result
        else:
            # MX exists, that's all we can say
            base["valid"] = None
            base["confidence"] = 40
            base["method_used"] = "mx_only"
            base["error"] = (
                "Port 25 is blocked by your ISP. "
                "Add an ABSTRACT_API_KEY in .env for full verification."
            )
            base["response_time_ms"] = int((time.time() - start) * 1000)
            return base

    # 3. Catch-all probe
    from_addr, helo_host = get_smtp_sender_config()
    catchall = _is_catchall(mx_host, domain, from_addr, helo_host)
    base["catchall"] = catchall

    if catchall is True:
        base["valid"] = True   # email likely exists, but we can't be sure
        base["confidence"] = 55
        base["method_used"] = "smtp_catchall"
        base["response_time_ms"] = int((time.time() - start) * 1000)
        return base

    # 4. Real SMTP probe
    try:
        with smtplib.SMTP(timeout=10) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo(helo_host)
            smtp.mail(from_addr)
            code, msg = smtp.rcpt(email)
            smtp.quit()

            msg_str = msg.decode("utf-8", errors="ignore").lower() if isinstance(msg, bytes) else str(msg).lower()

            if code == 250:
                base["valid"] = True
                base["confidence"] = 95
            elif code in (550, 551, 552, 553):
                base["valid"] = False
                base["confidence"] = 95
            elif code in (421, 450, 451, 452):
                # Temporary failure — treat as unknown
                base["valid"] = None
                base["confidence"] = 30
                base["error"] = f"Temporary SMTP failure (code {code}). Try again later."
            else:
                base["valid"] = None
                base["confidence"] = 20
                base["error"] = f"Unexpected SMTP response: {code}"

    except smtplib.SMTPConnectError as e:
        base["error"] = f"Could not connect to mail server: {e}"
        base["confidence"] = 0
    except smtplib.SMTPServerDisconnected:
        base["valid"] = None
        base["confidence"] = 25
        base["error"] = "Mail server dropped connection (common with large providers)."
    except Exception as e:
        base["error"] = str(e)
        base["confidence"] = 0

    base["response_time_ms"] = int((time.time() - start) * 1000)
    return base


# ── AbstractAPI fallback ──────────────────────────────────────────────────────

def _verify_via_abstract_api(email: str, api_key: str) -> dict:
    """Verify email using AbstractAPI when port 25 is blocked."""
    import urllib.request
    import json as _json

    url = f"https://emailvalidation.abstractapi.com/v1/?api_key={api_key}&email={email}"
    base = {
        "email": email,
        "valid": None,
        "catchall": None,
        "mx_provider": None,
        "mx_record": None,
        "confidence": None,
        "port25_available": False,
        "method_used": "abstractapi",
        "error": None,
    }
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = _json.loads(resp.read())
            deliverability = data.get("deliverability", "")
            base["valid"] = deliverability == "DELIVERABLE"
            base["catchall"] = data.get("is_catchall_email", {}).get("value", False)
            base["confidence"] = int(data.get("quality_score", 0) * 100)
            base["mx_record"] = data.get("smtp_provider", None)
    except Exception as e:
        base["error"] = f"AbstractAPI error: {e}"
    return base
