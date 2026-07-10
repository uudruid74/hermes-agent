"""Gmail tool using Google OAuth PKCE and Hermes secrets integration.

This module provides a tool function 'send_gmail' that sends emails via Gmail API
using OAuth credentials from Hermes' credential pool.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from tools.lazy_deps import ensure, FeatureUnavailable
from tools.registry import tool_error

logger = __import__("logging").getLogger(__name__)

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _GOOGLE_DEPS_OK = True
except Exception:
    _GOOGLE_DEPS_OK = False

# Ensure dependencies are available
if _GOOGLE_DEPS_OK:
    try:
        ensure("provider.google")
    except Exception:
        _GOOGLE_DEPS_OK = False


class GmailToolError(RuntimeError):
    """Base class for Gmail tool errors."""


def get_gmail_oauth_credentials() -> dict[str, Any]:
    """Get Gmail OAuth credentials from Hermes credential pool.
    
    Returns:
        Dictionary containing 'access_token' and optionally 'refresh_token' and 'expires_at'
        
    Raises:
        GmailToolError: If credentials are not found or invalid
    """
    try:
        import hermes_cli.auth as auth
        
        # Try to get credentials for 'gmail' provider
        result = auth.get_provider_credentials("gmail")
        
        if not result or "access_token" not in result:
            raise GmailToolError(
                "No Gmail OAuth credentials found.\n"
                "1. Install google-api-python-client dependency:\n"
                "   python -m pip install google-api-python-client==2.194.0\n"
                "2. Run `hermes auth add gmail` to authenticate with Gmail API"
            )
        
        # Normalize expiry if it's a string
        if isinstance(result.get("expires_at"), str):
            try:
                result["expires_at"] = float(result["expires_at"])
            except ValueError:
                del result["expires_at"]
                
        return result
    except GmailToolError:
        raise
    except Exception as e:
        raise GmailToolError(f"Failed to get Gmail credentials: {e}")


async def async_send_gmail(
    to: str,
    subject: str,
    body: str,
    body_html: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    attachments: Optional[list[str]] = None,
    redact_strings: Optional[list[str]] = None,
) -> str:
    """Send email via Gmail API.
    
    Args:
        to: Recipient email address or comma-separated addresses
        subject: Email subject
        body: Plain text email body
        body_html: HTML email body (optional)
        cc: List of CC recipient email addresses
        bcc: List of BCC recipient email addresses
        attachments: List of file paths to attach (optional)
        redact_strings: Optional list of strings to redact from message (optional)
    
    Returns:
        JSON string with result or error
    """
    if not _GOOGLE_DEPS_OK:
        return tool_error(
            "Gmail tool unavailable: google-api-python-client is not installed.\n"
            "Install it with: python -m pip install google-api-python-client==2.194.0\n"
            "Or enable via: hermes tools enable skill.google_workspace"
        )
    
    try:
        from agent.redact import redact_sensitive_text
        
        # Redact sensitive data from input
        safe_body = redact_sensitive_text(body, redact_strings or ["REDACT", "HIDDEN"])
        safe_subject = redact_sensitive_text(subject, redact_strings or ["REDACT", "HIDDEN"])
        safe_to = redact_sensitive_text(to, redact_strings or ["REDACT", "HIDDEN"])
        
        if cc:
            safe_cc = [redact_sensitive_text(c, redact_strings or ["REDACT", "HIDDEN"]) for c in cc]
        else:
            safe_cc = None
            
        if bcc:
            safe_bcc = [redact_sensitive_text(b, redact_strings or ["REDACT", "HIDDEN"]) for b in bcc]
        else:
            safe_bcc = None
            
        # Get OAuth credentials from Hermes credential pool
        credentials_data = get_gmail_oauth_credentials()
        
        # Build credentials object
        gmail_creds = Credentials(
            token=credentials_data["access_token"],
            refresh_token=credentials_data.get("refresh_token"),
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            expiry=credentials_data.get("expires_at"),
        )
        
        # Build Gmail service
        service = build("gmail", "v1", credentials=gmail_creds, static_discovery=True)
        
        # Build email message
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        message = MIMEMultipart()
        message["to"] = safe_to
        message["subject"] = safe_subject
        
        if safe_cc:
            message["cc"] = ", ".join(safe_cc)
            
        if safe_bcc:
            message["bcc"] = ", ".join(safe_bcc)
        
        # Add body content
        if body_html:
            message.attach(MIMEText(safe_body, "plain"))
            message.attach(MIMEText(body_html, "html"))
        else:
            message.attach(MIMEText(safe_body, "plain"))
        
        # Process attachments
        if attachments:
            import base64
            import mimetypes
            from email import encoders
            
            if not isinstance(attachments, list):
                attachments = [attachments]
            
            for attachment_path in attachments:
                attachment_path_obj = Path(attachment_path)
                if not attachment_path_obj.exists():
                    return tool_error(f"Attachment not found: {attachment_path_obj}")
                
                # Read file and encode
                with open(attachment_path_obj, "rb") as f:
                    file_data = f.read()
                
                # Guess mime type
                mime_type, _ = mimetypes.guess_type(attachment_path_obj.name)
                if not mime_type:
                    mime_type = "application/octet-stream"
                
                # Create attachment part
                part = MIMEText("Streaming email attachment...", "plain")
                part["Content-Type"] = mime_type
                part["Content-Disposition"] = f'attachment; filename="{attachment_path_obj.name}"'
                part.set_payload(file_data)
                encoders.encode_base64(part)
                
                message.attach(part)
        
        # Encode message in base64url format for Gmail API
        message_str = message.as_string()
        message_bytes = message_str.encode("utf-8")
        message_b64 = base64.urlsafe_b64encode(message_bytes).decode("utf-8")
        
        # Send message via Gmail API
        sent_message = service.users().messages().send(
            userId="me",
            body={"raw": message_b64}
        ).execute()
        
        result = {
            "status": "success",
            "message_id": sent_message.get("id"),
            "thread_id": sent_message.get("threadId"),
            "to": to,
            "subject": subject,
            "size_bytes": len(message_bytes),
        }
        
        return json.dumps(result, ensure_ascii=False)
        
    except HttpError as e:
        error_content = (
            e.content.decode("utf-8") if e.content 
            else str(e)
        )
        return tool_error(f"Gmail API error: {error_content}")
    except GmailToolError as e:
        return tool_error(str(e))
    except FeatureUnavailable as e:
        return tool_error(f"{str(e)}")
    except Exception as e:
        logger.exception("Unexpected error sending email")
        return tool_error(f"Failed to send email: {e}")


def check_gmail_requirements() -> bool:
    """Check if Gmail tool requirements are met."""
    return _GOOGLE_DEPS_OK


def run_gmail_test() -> str:
    """Minimal demo function for testing Gmail functionality.
    
    This can be invoked via: python -m hermes.email gmail-test
    """
    print("\n=== Gmail Tool Test ===\n")
    
    # Check credentials
    try:
        credentials = get_gmail_oauth_credentials()
        print("✓ Credentials found")
        print(f"  Provider: gmail")
        print(f"  Has access_token: {'access_token' in credentials}")
        print(f"  Has refresh_token: {'refresh_token' in credentials}")
        if credentials.get("expires_at"):
            from datetime import datetime
            expiry = datetime.fromtimestamp(credentials["expires_at"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  Token expires: {expiry}")
    except GmailToolError as e:
        print(f"✗ Credential error: {e}")
        return "fail"
    except Exception as e:
        print(f"✗ Unexpected credential error: {e}")
        return "fail"
    
    # Check if dependencies are available
    if not _GOOGLE_DEPS_OK:
        print("✗ Dependencies not available")
        print("  Need: google-api-python-client==2.194.0")
        return "fail"
    print("✓ Dependencies available")
    
    print("\n✓ Gmail tool test passed!")
    return "pass"


if __name__ == "__main__":
    result = run_gmail_test()
    exit(0 if result == "pass" else 1)
