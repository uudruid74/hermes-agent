# Gmail OAUTH2 Integration Plan

## Purpose
Enable secure Gmail API access for Hermes Agent via OAuth2 flow to support email operations (read, send, manage threads) without storing raw credentials.

## Constraints
- Must use Google's OAuth2 authorization code flow (not service accounts unless domain-wide delegation is approved)
- Mobile/desktop app restrictions: can't use localhost redirect URIs; requires `https://localhost:{port}/auth/callback` loopback handling
- Token storage must be encrypted at rest; prefer OS keychain where available
- User consent required; scope approval burdens UX if too broad
- App passwords viable but less secure than OAuth; prefer OAuth for new integrations

## Design
- **Flow**: OAuth2 Authorization Code with PKCE (recommended for public clients)
- **Scopes**: `https://www.googleapis.com/auth/gmail.readonly` (min), `https://www.googleapis.com/auth/gmail.send`, `https://www.googleapis.com/auth/gmail.modify` (if edits required)
- **Storage**: Securely store refresh token; session tokens short-lived
- **Client**: Google OAuth 2.0 client IDs (create project + credentials in Google Cloud Console)
- **UI**: Local server on ephemeral port (e.g. 8765) serving `/auth/callback`; CLI opens browser to auth URL
- **Error handling**: Token refresh on expiry, consent screen retries, scope downshifting on rejection

## Directory Layout
```
.gmail-integration/
├── auth/              # Token storage and session handling
│   ├── __init__.py
│   ├── tokens.py      # Secure token storage
│   └── session.py     # OAuth2 session manager
├── gmail/             # Gmail API abstraction
│   ├── __init__.py
│   ├── client.py      # Authenticated API client wrapper
│   ├── operations.py  # Read/send/thread operations
│   └── exceptions.py  # Custom exceptions
├── config.yaml.sample # Optional: sample OAuth client config
└── tests/            # Test harness and fixtures
    ├── __init__.py
    ├── test_auth.py
    └── test_operations.py
```

## Required Configs
1. Google Cloud Console OAuth credentials:
   - Client ID (public), Client Secret
   - Authorized redirect URIs: `https://localhost:8765/auth/callback`
   - Scopes configured in credentials
2. Environment variables for Hermes Agent:
   - `GMAIL_CLIENT_ID` = <your_client_id>
   - `GMAIL_CLIENT_SECRET` = <your_client_secret>
   - `GMAIL_REDIRECT_URI` = `https://localhost:8765/auth/callback` (default; modifiable via config)
   - `GMAIL_TOKEN_DIR` = ~/.local/share/hermes/gmail/tokens (default; encrypted storage enforced)

## Enable Steps
1. Create Google Cloud project and enable Gmail API
2. Configure OAuth consent screen
3. Create OAuth credentials (Desktop app type recommended for local CLI)
4. Install Hermes Gmail plugin (use plugin manager or hermes plugin install gmail)
5. Run `hermes gmail auth-init` to open browser for OAuth flow
6. Secure storage: tokens stored encrypted in user-specific directory (respected by `GMAIL_TOKEN_DIR`)
7. Test `hermes gmail list-labels` on success

## Error Handling
- **Consent rejected**: Guide user to retry with narrower scopes or generate an app password as fallback
- **Token expired**: Automate refresh using refresh token; surface user prompt if refresh chanllenges occur
- **Rate limits**: Exponential backoff in API client; respect 429s and retry-after headers
- **Invalid token**: Purge tokens and force re-auth (session.storage reset)

## Test Harness
- Unit: Mock OAuth2 flow (pytest fixtures for token refresh and scope rejection)
- Integration: Live OAuth flow with throwaway Google account
- Smoke: `hermes gmail list-labels`, `hermes gmail send`, `hermes gmail threads-list`
- Regression: Capture OAuth consent friction metrics via latency measurement
- Failsafe: Provide offline mode that rejects operations until re-auth as last safety net
