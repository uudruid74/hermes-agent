---
name: google-workspace
description: "📧 EMAIL — THIS IS THE EMAIL SKILL. Gmail, Calendar, Drive, Docs, Sheets via Gmail API OAuth2. All agents: load this skill whenever you need to send/read/search email. See also: email/gmail-api bridge skill."
version: 1.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials (downloaded from Google Cloud Console)
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya, gmail-api]
---
# Google Workspace

Gmail, Calendar, Drive, Contacts, Sheets, and Docs — through Hermes-managed OAuth and a thin CLI wrapper. When `gws` is installed, the skill uses it as the execution backend for broader Google Workspace coverage; otherwise it falls back to the bundled Python client implementation.

## References

- `references/gmail-search-syntax.md` — Gmail search operators (is:unread, from:, newer_than:, etc.)

## Scripts

- `scripts/setup.py` — OAuth2 setup (run once to authorize)
- `scripts/google_api.py` — compatibility wrapper CLI. It prefers `gws` for operations when available, while preserving Hermes' existing JSON output contract.

## First-Time Setup

The setup is fully non-interactive — you drive it step by step so it works
on CLI, Telegram, Discord, or any platform.

```bash
# In subagent contexts, HERMES_REAL_HOME points to the real ~/.hermes shared pool.
# HERMES_HOME points to the profile dir (e.g. ~/.hermes/profiles/gopher) which
# does NOT contain skill scripts — they live in the shared pool at REAL_HOME/.hermes/skills/.
_SKILLS_BASE="${HERMES_REAL_HOME:-$HOME}"
GSETUP="python $_SKILLS_BASE/.hermes/skills/productivity/google-workspace/scripts/setup.py"
```

### Step 0: Check if already set up

```bash
$GSETUP --check
```

If it prints `AUTHENTICATED`, skip to Usage — setup is already done.

### Step 1: Triage — ask the user what they need

Before starting OAuth setup, ask the user TWO questions:

- **Email only** → They don't need this skill at all. Use the `himalaya` skill
  instead — it works with a Gmail App Password (Settings → Security → App
  Load the himalaya skill and follow its setup instructions.

- **Email + Calendar** → Continue with this skill, but use
  `--services email,calendar` during auth so the consent screen only asks for

- **Calendar/Drive/Sheets/Docs only** → Continue with this skill and use a
  narrower `--services` set like `calendar,drive,sheets,docs`.

- **Full Workspace access** → Continue with this skill and use the default
  `all` service set.

security keys required to sign in)? If you're not sure, you probably don't

- **No / Not sure** → Normal setup. Continue below.
- **Yes** → Their Workspace admin must add the OAuth client ID to the org's

### Step 2: Create OAuth credentials (one-time, ~5 minutes)

> You need a Google Cloud OAuth client. This is a one-time setup:
> 1. Create or select a project:
>    https://console.cloud.google.com/projectselector2/home/dashboard
> 2. Enable the required APIs from the API Library:
>    https://console.cloud.google.com/apis/library
>    Enable: Gmail API, Google Calendar API, Google Drive API,
>    Google Sheets API, Google Docs API, People API
> 3. Create the OAuth client here:
>    https://console.cloud.google.com/apis/credentials
>    Credentials → Create Credentials → OAuth 2.0 Client ID
> 4. Application type: "Desktop app" → Create
> 5. If the app is still in Testing, add the user's Google account as a test user here:
>    https://console.cloud.google.com/auth/audience
>    Audience → Test users → Add users
> 6. Download the JSON file and tell me the file path
> Important Hermes CLI note: if the file path starts with `/`, do NOT send only the bare path as its own message in the CLI, because it can be mistaken for a slash command. Send it in a sentence instead, like:
> `The JSON file path is: /home/user/Downloads/client_secret_....json`

Once they provide the path:

```bash
$GSETUP --client-secret /path/to/client_secret.json
```

If they paste the raw client ID / client secret values instead of a file path,
write a valid Desktop OAuth JSON file for them yourself, save it somewhere
explicit (for example `~/Downloads/hermes-google-client-secret.json`), then run
`--client-secret` against that file.

### Step 3: Get authorization URL

```bash
$GSETUP --auth-url --services email,calendar --format json
$GSETUP --auth-url --services calendar,drive,sheets,docs --format json
$GSETUP --auth-url --services all --format json
```

This returns JSON with an `auth_url` field and also saves the exact URL to
`~/.hermes/google_oauth_last_url.txt`.

- Extract the `auth_url` field and send that exact URL to the user as a single line.
- Tell the user that the browser will likely fail on `http://localhost:1` after approval, and that this is expected.
- Tell them to copy the ENTIRE redirected URL from the browser address bar.
- If the user gets `Error 403: access_denied`, send them directly to `https://console.cloud.google.com/auth/audience` to add themselves as a test user.

### Step 4: Exchange the code

The user will paste back either a URL like `http://localhost:1/?code=4/0A...&scope=...`
or just the code string. Either works. The `--auth-url` step stores a temporary
pending OAuth session locally so `--auth-code` can complete the PKCE exchange

```bash
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED" --format json
```

If `--auth-code` fails because the code expired, was already used, or came from
an older browser tab, it now returns a fresh `fresh_auth_url`. In that case,
immediately send the new URL to the user and have them retry with the newest
browser redirect only.

### Step 5: Verify

```bash
$GSETUP --check
```

Should print `AUTHENTICATED`. Setup is complete — token refreshes automatically from now on.

### Notes

- Token is stored at `~/.hermes/google_token.json` and auto-refreshes.
- Pending OAuth session state/verifier are stored temporarily at `~/.hermes/google_oauth_pending.json` until exchange completes.
- If `gws` is installed, `google_api.py` points it at the same `~/.hermes/google_token.json` credentials file. Users do not need to run a separate `gws auth login` flow.
- To revoke: `$GSETUP --revoke`

## Usage

All commands go through the API script. Set `GAPI` as a shorthand:

```bash
_SKILLS_BASE="${HERMES_REAL_HOME:-$HOME}"
GAPI="python $_SKILLS_BASE/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
```

### Gmail

```bash
# Search (returns JSON array with id, from, subject, date, snippet)
$GAPI gmail search "is:unread" --max 10
$GAPI gmail search "from:boss@company.com newer_than:1d"
$GAPI gmail search "has:attachment filename:pdf newer_than:7d"

# Read full message (returns JSON with body text)
$GAPI gmail get MESSAGE_ID

# Send
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"
$GAPI gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1><p>Details...</p>" --html
$GAPI gmail send --to user@example.com --subject "Hello" --from '"Research Agent" <user@example.com>' --body "Message text"

# Reply (automatically threads and sets In-Reply-To)
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
$GAPI gmail reply MESSAGE_ID --from '"Support Bot" <user@example.com>' --body "Thanks"

# Labels
$GAPI gmail labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
$GAPI gmail modify MESSAGE_ID --remove-labels UNREAD
```

### Calendar

```bash
# List events (defaults to next 7 days)
$GAPI calendar list
$GAPI calendar list --start 2026-03-01T00:00:00Z --end 2026-03-07T23:59:59Z

# Create event (ISO 8601 with timezone required)
$GAPI calendar create --summary "Team Standup" --start 2026-03-01T10:00:00-06:00 --end 2026-03-01T10:30:00-06:00
$GAPI calendar create --summary "Lunch" --start 2026-03-01T12:00:00Z --end 2026-03-01T13:00:00Z --location "Cafe"
$GAPI calendar create --summary "Review" --start 2026-03-01T14:00:00Z --end 2026-03-01T15:00:00Z --attendees "alice@co.com,bob@co.com"

# Delete event
$GAPI calendar delete EVENT_ID
```

### Drive

```bash
# Search existing files
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5

# Get metadata for a single file
$GAPI drive get FILE_ID

# Upload a local file (auto-detects MIME type)
$GAPI drive upload /path/to/report.pdf
$GAPI drive upload /path/to/image.png --name "Logo.png" --parent FOLDER_ID

# Download (binary files download as-is; Google-native files export to a
# sensible default — Docs→pdf, Sheets→csv, Slides→pdf, Drawings→png)
$GAPI drive download FILE_ID
$GAPI drive download DOC_ID --output ~/doc.pdf
$GAPI drive download DOC_ID --export-mime text/plain --output ~/doc.txt

# Create a folder
$GAPI drive create-folder "Reports"
$GAPI drive create-folder "Q4" --parent FOLDER_ID

# Share
$GAPI drive share FILE_ID --email alice@example.com --role reader
$GAPI drive share FILE_ID --email alice@example.com --role writer --notify
$GAPI drive share FILE_ID --type anyone --role reader        # anyone with link
$GAPI drive share FILE_ID --type domain --domain example.com --role reader

# Delete — defaults to trash (reversible). Use --permanent to skip the trash.
$GAPI drive delete FILE_ID
$GAPI drive delete FILE_ID --permanent
```

### Contacts

```bash
$GAPI contacts list --max 20
```

### Sheets

```bash
# Create a new spreadsheet
$GAPI sheets create --title "Q4 Budget"
$GAPI sheets create --title "Inventory" --sheet-name "Stock"

# Read
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"

# Write
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'

# Append rows
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

### Docs

```bash
# Read
$GAPI docs get DOC_ID

# Create a new Doc (optionally seeded with body text)
$GAPI docs create --title "Meeting Notes"
$GAPI docs create --title "Draft" --body "First paragraph..."

# Append text to the end of an existing Doc
$GAPI docs append DOC_ID --text "Additional content to append"
```

## Output Format

All commands return JSON. Parse with `jq` or read directly. Key fields:

- **Gmail search**: `[{id, threadId, from, to, subject, date, snippet, labels}]`
- **Gmail get**: `{id, threadId, from, to, subject, date, labels, body}`
- **Gmail send/reply**: `{status: "sent", id, threadId}`
- **Calendar list**: `[{id, summary, start, end, location, description, htmlLink}]`
- **Calendar create**: `{status: "created", id, summary, htmlLink}`
- **Drive search**: `[{id, name, mimeType, modifiedTime, webViewLink}]`
- **Drive get**: `{id, name, mimeType, modifiedTime, size, webViewLink, parents, owners}`
- **Drive upload**: `{status: "uploaded", id, name, mimeType, webViewLink}`
- **Drive download**: `{status: "downloaded", id, name, path, mimeType}`
- **Drive create-folder**: `{status: "created", id, name, webViewLink}`
- **Drive share**: `{status: "shared", permissionId, fileId, role, type}`
- **Drive delete**: `{status: "trashed" | "deleted", fileId, permanent}`
- **Contacts list**: `[{name, emails: [...], phones: [...]}]`
- **Sheets get**: `[[cell, cell, ...], ...]`
- **Sheets create**: `{status: "created", spreadsheetId, title, spreadsheetUrl}`
- **Docs create**: `{status: "created", documentId, title, url}`
- **Docs append**: `{status: "appended", documentId, inserted_at, characters}`

## Rules

1. **Never send email, create/delete calendar events, delete Drive files, share files, or modify Docs/Sheets without confirming with the user first.** Show what will be done (recipients, file IDs, content, share role) and ask for approval. For `drive delete`, prefer the default trash (reversible) over `--permanent`.
2. **Check auth before first use** — run `setup.py --check`. If it fails, guide the user through setup.
3. **Use the Gmail search syntax reference** for complex queries — load it with `skill_view("google-workspace", file_path="references/gmail-search-syntax.md")`.
4. **Calendar times must include timezone** — always use ISO 8601 with offset (e.g., `2026-03-01T10:00:00-06:00`) or UTC (`Z`).
5. **Respect rate limits** — avoid rapid-fire sequential API calls. Batch reads when possible.

### Direct Gmail API Approach (Recommended for Sending Email)

**IMPORTANT**: As of March 14, 2025, Google has deprecated all password-based authentication for Gmail access. Standard SMTP authentication no longer works. **Use the Gmail API with OAuth2 tokens** instead.

**Recommended Authentication Method**: Gmail API + OAuth2 via Hermes credential pool

#### Step-by-Step Implementation

**Prerequisite**: Install the Gmail API client library (one-time):

```bash
pip install google-api-python-client==2.194.0 google-auth-oauthlib==1.3.1
```

**Step 1: Configure Gmail OAuth2 Client**

```bash
# Quick setup using existing google-workspace auth if available:
# (Tokens are automatically used if scopes include gmail.send)

# OR minimal setup for email-only needs:
hermes auth add gmail --scopes "https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/userinfo.email"
```

**Step 2: Send Email via Gmail API**

Use the `users.messages.send` endpoint with proper MIME formatting and base64URL encoding:

```python
import base64
from email.message import EmailMessage
from googleapiclient.discovery import build

# Create RFC 2822 MIME message
message = EmailMessage()
message.set_content("Hello from Hermes Agent")
message["To"] = "recipient@example.com"
message["From"] = "you@gmail.com"  # Must match authenticated user
message["Subject"] = "Notification"

# Encode as base64URL (Google API requirement)
encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

# Send via API
service = build("gmail", "v1", credentials=oauth_credentials)
response = service.users().messages().send(
    userId="me",  # Use "me" for authenticated user
    body={"raw": encoded_message}
).execute()

print(f"Message sent! ID: {response['id']}")
```

**Python CLI Implementation**:

```bash
# Example: /tools/gmail_send.py

from googleapiclient.discovery import build
from agent.credential_pool import CredentialPool

def send_email(to, subject, body, cc=None, bcc=None):
    """Send email via Gmail API with proper MIME formatting."""
    # Get credentials from Hermes credential pool
    pool = CredentialPool(provider="gmail-api")
    credentials = pool.get("gmail-send-creds")

    # Build Gmail API service
    service = build("gmail", "v1", credentials=credentials)

    # Create and encode message
    from email.message import EmailMessage
    import base64

    msg = EmailMessage()
    msg.set_content(body)
    msg["To"] = to
    msg["From"] = credentials.email
    msg["Subject"] = subject

    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    # Send with retry logic
    try:
        response = service.users().messages().send(
            userId="me",
            body={"raw": encoded}
        ).execute()
        return {"status": "sent", "id": response["id"]}
    except Exception as e:
        # Implement exponential backoff for rate limits
        raise Exception(f"Failed to send email: {str(e)}")
```

**Step 3: Handle Rate Limits & Errors**

Google enforces strict rate limits. Use this algorithm for `429` and `503` errors:

```python
import time
import random

def exponential_backoff(retry_count, max_retries=10):
    """Handle Gmail API quota/rate limit errors."""
    # Wait before retrying
    delay = min(64.0, min(0.1 * (2 ** retry_count), 64.0))
    delay += random.uniform(0, 1.0)  # Add jitter to prevent synchronized retries

    time.sleep(delay)

    return retry_count < max_retries
```

**Key Gmail API Limitations:**
- ✓ **500 recipients per message maximum** (spread across To/Cc/Bcc)
- ✓ **500 messages per day** (free accounts), **up to 1,000-2,000/day** (Google Workspace)
- ✓ **6,000 quota units per minute per user**
- ✓ **messages.send = 100 quota units per email**
- ✓ Messages must be RFC 2822 MIME format, base64URL encoded

1. Go to: https://console.cloud.google.com/iam-admin/quotas
2. Search for "Gmail API"
3. Request increase for "Gmail API - Messages per day"
4. Approval not guaranteed; provide business justification

### Legacy SMTP Methods (Deprecated - DO NOT USE)

⚠️ **WARNING**: All SMTP password-based methods are deprecated by Google:

- ❌ SMTP with username/password: **Disabled March 14, 2025**
- ❌ SMTP with OAuth2 client credentials: **Disabled September 30, 2024**
- ❌ SMTP with app passwords: **Legacy only, discouraged** (requires 2SV, Advanced Protection blocks this)

**Use Gmail API instead** for all email sending operations.

### Deprecated "python -m hermes.email" References

Updated: Previous version referenced hypothetical `python -m hermes.email` commands which don't match current Hermes architecture. Use the Gmail API implementation above with Hermes credential pool instead.

- Gmail API via `users.messages.send` with proper MIME/base64URL encoding
- Hermes credential pool for token storage and management
- Rate limit handling with exponential backoff (critical for Google API)
- Standard Python libraries rather than custom module paths

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `NOT_AUTHENTICATED` | Run setup Steps 2-5 above |
| `ModuleNotFoundError` when using direct Gmail tool | Run `pip install google-api-python-client==2.194.0` |
| Direct tool shows 'No Gmail OAuth credentials found' | Run `hermes auth add gmail` or use `hermes google-workspace` skill's OAuth flow |
| `REFRESH_FAILED` | Token revoked or expired — redo Steps 3-5 |
| `HttpError 403: Insufficient Permission` | Missing API scope — `$GSETUP --revoke` then redo Steps 3-5 |
| `AUTHENTICATED (partial)` or "Token missing scopes" | New write capabilities (Drive write/delete, Docs create/edit) require re-authorization. `$GSETUP --revoke` then redo Steps 3-5 to grant the upgraded scopes. |
| `HttpError 403: Access Not Configured` | API not enabled — user needs to enable it in Google Cloud Console |
| `ModuleNotFoundError` | Run `$GSETUP --install-deps` |
| Advanced Protection blocks auth | Workspace admin must allowlist the OAuth client ID |

## Revoking Access

```bash
$GSETUP --revoke
```
