# Email Lookup API (Flask) 🔍✉️

[![GitHub Repository](https://img.shields.io/badge/GitHub-Saad--61%2FEmailLookup--Flask-blue?logo=github)](https://github.com/Saad-61/EmailLookup-Flask)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-brightgreen.svg)](https://python.org)
[![Framework](https://img.shields.io/badge/framework-Flask-black.svg?logo=flask)](https://flask.palletsprojects.com/)
[![WSGI Server](https://img.shields.io/badge/server-Waitress-orange.svg)](https://docs.pylonsproject.org/projects/waitress/en/stable/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A high-performance reverse email OSINT and deliverability verification API built with **Python & Flask**. Designed for seamless developer integration, high accuracy, robust validation, and zero-conflict deployment on Port `5000`.

---

## 🚀 Features

- **Reverse Email OSINT Investigation**:
  - Scrapes and probes public footprints across GitHub, Gravatar, Spotify, Discord, Instagram, and more.
  - Resolves person metadata (display names, usernames, profile avatars, bio).
- **Direct SMTP Mailbox Verification**:
  - Multi-stage DNS MX resolution and SMTP handshake (`HELO` -> `MAIL FROM` -> `RCPT TO`).
  - Catch-all domain detection and mailbox existence verification.
- **Port 25 Outbound ISP Check**:
  - Instant diagnostic endpoint to verify if ISP or host firewall blocks outbound port 25.
- **Persistent Caching**:
  - Thread-safe SQLite backend cache with TTL for fast repeated queries (~1ms) and rate limit preservation.
- **Production-Ready**:
  - Asynchronous probe orchestration, environment-driven configurations, and Waitress WSGI integration for production.

---

## 📋 Requirements

- Python 3.10+
- Active internet connection (Port 25 outbound optional for direct SMTP verifications)

---

## 🛠️ Quickstart & Installation

### 1. Clone & Navigate

```bash
git clone https://github.com/Saad-61/EmailLookup-Flask.git
cd EmailLookup-Flask
```

### 2. Set Up Virtual Environment

```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
# Windows (cmd / PowerShell)
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Edit `.env` to configure your keys (all optional, but recommended for full OSINT capabilities):

| Variable | Description |
| :--- | :--- |
| `HOST` | Bind address (default: `127.0.0.1` or `0.0.0.0`) |
| `PORT` | API server port (default: `5000`) |
| `DEBUG` | Enable debug mode (`true` / `false`) |
| `GITHUB_TOKEN` | GitHub Personal Access Token (boosts rate limit from 60 to 5,000 req/hr) |
| `SMTP_SENDER_EMAIL` | Sender address used in SMTP `MAIL FROM` verification handshakes |
| `SMTP_HELO_HOST` | HELO host name used in SMTP handshake (e.g. `mail.gmail.com`) |
| `PROXY_IPS` | Comma-separated residential proxy IP:port list for rotation |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | Proxy authentication credentials |
| `SPOTIFY_CLIENT_TOKEN` / `SPOTIFY_AUTH_TOKEN` | Spotify Pathfinder GraphQL user search tokens |

---

## 🏃 Running the Server

### Development Mode (Port 5000)

```bash
# Direct execution:
python app/main.py

# Or via Flask CLI:
flask --app app.main run --host 0.0.0.0 --port 5000 --debug
```

### Production Mode (Waitress WSGI)

```bash
waitress-serve --host=0.0.0.0 --port=5000 wsgi:app
```

---

## 🧪 Testing the API & Checking Results

You can test the running API directly using any of the following methods without needing a frontend UI:

### Method 1: PowerShell (Built-in on Windows)

#### 1. Full Reverse OSINT Lookup
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/lookup" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email": "torvalds@linux-foundation.org"}' | ConvertTo-Json -Depth 6
```

#### 2. Force Refresh (Bypass 24h Cache)
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/lookup" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email": "torvalds@linux-foundation.org", "force_refresh": true}' | ConvertTo-Json -Depth 6
```

#### 3. Direct SMTP Mailbox Deliverability Check
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/verify" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email": "test@gmail.com"}' | ConvertTo-Json -Depth 5
```

#### 4. Outbound Port 25 Diagnostic
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/port-check"
```

#### 5. Invalidate / Purge Cache for an Email
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/cache/invalidate" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"email": "torvalds@linux-foundation.org"}'
```

---

### Method 2: cURL (Command Line)

#### OSINT Lookup:
```bash
curl -X POST http://127.0.0.1:5000/api/lookup \
  -H "Content-Type: application/json" \
  -d '{"email": "torvalds@linux-foundation.org", "force_refresh": false}'
```

#### SMTP Verification:
```bash
curl -X POST http://127.0.0.1:5000/api/verify \
  -H "Content-Type: application/json" \
  -d '{"email": "test@gmail.com"}'
```

#### Port 25 Check:
```bash
curl http://127.0.0.1:5000/api/port-check
```

---

### Method 3: Python Script

```python
import requests

# 1. Reverse Email Lookup
res = requests.post("http://127.0.0.1:5000/api/lookup", json={
    "email": "torvalds@linux-foundation.org",
    "force_refresh": False
})
print("Lookup Result:", res.json())

# 2. SMTP Verification
res_verify = requests.post("http://127.0.0.1:5000/api/verify", json={
    "email": "test@gmail.com"
})
print("Verify Result:", res_verify.json())
```

---

### Method 4: Postman / API Client GUI

1. Open **Postman** and create a new request (`+`).
2. Set the method to **`POST`**.
3. Enter URL: `http://127.0.0.1:5000/api/lookup`
4. Click the **Body** tab $\rightarrow$ select **raw** $\rightarrow$ choose **JSON** from the format dropdown.
5. Paste the request payload:
   ```json
   {
     "email": "user@example.com",
     "force_refresh": false
   }
   ```
6. Click **Send** to view the formatted JSON response and timing.

---

### Method 5: Web Browser (GET Endpoints)

Open directly in your web browser:
- [http://localhost:5000/](http://localhost:5000/) — API Server Status
- [http://localhost:5000/api/health](http://localhost:5000/api/health) — Health Check
- [http://localhost:5000/api/port-check](http://localhost:5000/api/port-check) — Outbound Port 25 ISP Status

---

## 📡 API Reference & Schema

### `POST /api/lookup`
Performs a deep reverse email OSINT search across platforms and public sources.

#### Request Fields:
| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `email` | `string` | Yes | Target email address to analyze |
| `force_refresh` | `boolean` | No | `false` (default): Return cached data if queried within 24h (~1ms response).<br>`true`: Bypass cache and trigger live web scraping across all platforms. |

#### Response Schema:
```json
{
  "email": "torvalds@linux-foundation.org",
  "domain": "linux-foundation.org",
  "email_type": "corporate",
  "query_time_ms": 342,
  "cached": false,
  "person": {
    "name": "Linus Torvalds",
    "avatar": "https://avatars.githubusercontent.com/u/1024025",
    "bio": "Creator of Linux and Git",
    "location": "Portland, OR",
    "website": "https://kernel.org"
  },
  "platforms": [
    {
      "name": "GitHub",
      "found": true,
      "icon": "github",
      "url": "https://github.com/torvalds"
    }
  ],
  "social_candidates": [],
  "social_candidates_by_platform": {}
}
```

---

### `POST /api/verify`
Performs multi-stage DNS MX resolution and SMTP mailbox deliverability handshake.

#### Request:
```json
{
  "email": "test@gmail.com"
}
```

#### Response:
```json
{
  "email": "test@gmail.com",
  "valid": true,
  "catchall": false,
  "mx_provider": "Google Workspace / Gmail",
  "mx_record": "gmail-smtp-in.l.google.com",
  "confidence": 95,
  "response_time_ms": 280,
  "port25_available": true,
  "method_used": "smtp"
}
```

---

### `POST /api/cache/invalidate`
Purges the 24-hour cache entry for a given email address.

#### Request:
```json
{
  "email": "torvalds@linux-foundation.org"
}
```

---

## 📁 Project Structure

```
EmailLookup-Flask/
├── app/
│   ├── __init__.py
│   ├── cache.py              # SQLite cache implementation
│   ├── lookup_engine.py      # Core OSINT orchestrator
│   ├── main.py               # Flask endpoints & routes
│   ├── models.py             # Data models & validation
│   ├── platform_checker.py   # Multi-platform probe coordinator
│   ├── smtp_verifier.py      # Direct SMTP handshake & port checking
│   └── social_finder.py      # Deep social profile scrapers
├── data/
│   └── .gitkeep              # SQLite cache database location
├── .env.example              # Template environment configuration
├── .gitignore                # Comprehensive Git exclusion rules
├── requirements.txt          # Python package dependencies
├── wsgi.py                   # Production WSGI entry point
└── README.md                 # Project documentation
```

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
