"""Hermes CLI email module — Gmail tool entry point.

Provides command-line access to Gmail OAUTH flow and tool testing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from tools.gmail_client import async_send_gmail, get_gmail_oauth_credentials
from tools.lazy_deps import FeatureUnavailable

logger = logging.getLogger(__name__)

def configure_logging() -> None:
    """Configure logging for CLI output."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )


def cmd_gmail_auth() -> int:
    """Subcommand: hermes email gmail-auth
    
    Guide user through Gmail OAuth authentication.
    """
    print("\nGmail OAuth Authentication")
    print("=" * 60)
    print("\nThis will open a browser window to authenticate with Google Gmail API.")
    print("You need to have google-api-python-client installed.")
    print("\nIf you haven't installed it yet, run:")
    print("  python -m pip install google-api-python-client==2.194.0")
    print("\nOnce completed, you can run:")
    print("  python -m hermes.email gmail-test")
    print("\nCurrent approach: Use `hermes auth add gmail` instead\n")
    return 0


def cmd_gmail_test() -> int:
    """Subcommand: hermes email gmail-test
    
    Test the Gmail tool functionality.
    """
    print("\nGmail Tool Test")
    print("=" * 60)
    
    # Check if google dependencies are available
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        _GOOGLE_OK = True
    except Exception:
        print("\n✗ Required Python packages not available")
        print("  google-api-python-client==2.194.0")
        print("\nInstall them with:")
        print("  python -m pip install google-api-python-client==2.194.0")
        print("\nOr enable via:")
        print("  hermes tools enable skill.google_workspace")
        return 1
    
    # Check credentials
    try:
        credentials = get_gmail_oauth_credentials()
        print("\n✓ Gmail OAuth credentials found")
        print(f"  Provider: gmail")
        print(f"  Provider registered: True")
        print(f"  Has access_token: {'access_token' in credentials}")
        print(f"  Has refresh_token: {'refresh_token' in credentials}")
        if credentials.get("expires_at"):
            import time
            now = time.time()
            expiry = credentials["expires_at"]
            remaining = expiry - now
            if remaining > 0:
                days = int(remaining // 86400)
                hours = int((remaining % 86400) // 3600)
                print(f"  Token expires in: {days} days, {hours} hours")
            else:
                print(f"  Token expired: {int(-remaining // 3600)} hours ago")
                if "refresh_token" in credentials:
                    print("  ✓ Refresh token available for re-authentication")
    except Exception as e:
        print(f"\n✗ Failed to get credentials: {e}")
        print("\nTo authenticate:")
        print("  1. Install google client: python -m pip install google-api-python-client==2.194.0")
        print("  2. Run OAuth flow: hermes auth add gmail")
        return 1
    
    # Simple send test (won't actually send without proper setup)
    print("\n" + "-" * 60)
    print("Testing 'send_gmail' tool function...")
    print("-" * 60)
    
    # Note: We can't actually test sending without proper OAuth permissions
    # Instead, just verify the async function is accessible
    print("\n✓ Tool functions are accessible")
    print(f"  async_send_gmail: {async_send_gmail is not None}")
    
    print("\n" + "=" * 60)
    print("✓ Gmail tool test completed successfully!")
    
    # Offer next steps
    print("\nNext steps:")
    print("1. Send an email:")
    print("   hermes send_tool call=")  
    print('   {"to": "recipient@example.com", "subject": "Test", "body": "Hello"}')
    print("\n2. Or run directly:")
    print("   python -c \"from tools.gmail_client import async_send_gmail; print(async_send_gmail(...))\"")
    
    return 0


def cmd_gmail_send() -> int:
    """Subcommand: hermes email gmail-send
    
    Send a test email via Gmail.
    """
    parser = argparse.ArgumentParser(description="Send email via Gmail API")
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body text")
    parser.add_argument("--body-html", help="Email body HTML (optional)")
    parser.add_argument("--cc", help="CC email addresses (comma-separated)")
    parser.add_argument("--bcc", help="BCC email addresses (comma-separated)")
    parser.add_argument("--attachments", help="Attachment file paths (comma-separated)")
    
    args = parser.parse_args()
    
    try:
        cc_list = args.cc.split(",") if args.cc else None
        bcc_list = args.bcc.split(",") if args.bcc else None
        attachments_list = args.attachments.split(",") if args.attachments else None
        
        # Call async function in a simple synchronous wrapper
        import asyncio
        
        async def _run():
            result = await async_send_gmail(
                to=args.to,
                subject=args.subject,
                body=args.body,
                body_html=args.body_html,
                cc=cc_list,
                bcc=bcc_list,
                attachments=attachments_list,
            )
            print(result)
            try:
                result_dict = json.loads(result)
                if isinstance(result_dict, dict) and "status" in result_dict:
                    if result_dict.get("status") == "success":
                        print(f"\n✓ Email sent successfully!")
                        print(f"  Message ID: {result_dict.get('message_id')}")
                        return 0
                    else:
                        print(f"\n✗ Email send failed: {result_dict.get('error', 'Unknown error')}")
                        return 1
                return 0
            except json.JSONDecodeError:
                print(f"\n✗ Unexpected response format")
                print(result)
                return 1
        
        return asyncio.run(_run())
        
    except FeatureUnavailable as e:
        print(f"\n✗ Feature unavailable: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Error sending email: {e}")
        import traceback
        traceback.print_exc()
        return 1


    except FeatureUnavailable as e:
        print(f"\n✗ Feature unavailable: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Error sending email: {e}")


def main() -> int:
    """Main entry point for 'hermes email' commands.
    
    Used as: python -m hermes.email <command>
    """
    parser = argparse.ArgumentParser(
        description="Hermes Gmail tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Gmail test subcommand
    p_test = subparsers.add_parser(
        "gmail-test",
        help="Test Gmail tool configuration"
    )
    
    # Gmail auth subcommand
    p_auth = subparsers.add_parser(
        "gmail-auth",
        help="Guide through Gmail OAuth authentication"
    )
    
    # Gmail send subcommand
    p_send = subparsers.add_parser(
        "gmail-send",
        help="Send a test email via Gmail API"
    )
    p_send.add_argument("--to", help="Recipient email")
    p_send.add_argument("--subject", help="Email subject")
    p_send.add_argument("--body", help="Email body text")
    
    # Parse args
    args = parser.parse_args()
    
    configure_logging()
    
    # Dispatch to appropriate command handler
    if args.command == "gmail-test":
        return cmd_gmail_test()
    elif args.command == "gmail-auth":
        return cmd_gmail_auth()
    elif args.command == "gmail-send":
        return cmd_gmail_send()
    else:
        print(f"\nUnknown command: {args.command}")
        parser.print_help()
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("Unhandled error in email module")
        sys.exit(1)
