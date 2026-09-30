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
  - Thread-safe SQLite backend cache with TTL for fast repeated queries and rate limit preservation.
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
# Direct execution
python app/main.py

# Or via Flask CLI
flask --app app.main run --host 0.0.0.0 --port 5000 --debug
```

### Production Mode (Waitress WSGI)

```bash
waitress-serve --host=0.0.0.0 --port=5000 wsgi:app
```

---

## 📡 API Reference

### 1. Health & Status
- **`GET /`** — API metadata & service status
- **`GET /api/health`** — Healthcheck endpoint
- **`GET /api/port-check`** — Validates whether outbound port 25 is open

### 2. Reverse Email OSINT Lookup
- **`POST /api/lookup`**

**Request Body:**
```json
{
  "email": "user@example.com",
  "fast_mode": false
}
```

**Response Example:**
```json
{
  "email": "user@example.com",
  "domain": "example.com",
  "is_valid_syntax": true,
  "person": {
    "display_name": "Jane Doe",
    "avatar_url": "https://...",
    "username": "janedoe",
    "location": "San Francisco, CA"
  },
  "platforms": [
    {
      "platform": "GitHub",
      "exists": true,
      "profile_url": "https://github.com/janedoe",
      "confidence": "high"
    }
  ],
  "duration_ms": 342.1
}
```

### 3. SMTP Mailbox Deliverability Check
- **`POST /api/verify`**

**Request Body:**
```json
{
  "email": "user@example.com"
}
```

### 4. Cache Management
- **`POST /api/cache/invalidate`**

**Request Body:**
```json
{
  "email": "user@example.com"
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
