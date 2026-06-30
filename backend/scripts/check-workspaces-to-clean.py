#!/usr/bin/env python3
"""
Check available workspaces for stale data on disk/git.

This script calls the API endpoint to check all available workspaces
and displays which ones need cleaning.

Usage:
    # Check workspaces (requires BACKEND_URL env var or --url)
    uv run python scripts/check-workspaces-to-clean.py
    
    # Use custom backend URL
    uv run python scripts/check-workspaces-to-clean.py --url http://localhost:8000
    
    # Or use Makefile shortcut from project root:
    make check-workspaces-to-clean
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

# Add backend to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def get_api_key() -> str:
    """Get API key from environment or file."""
    api_key = os.getenv("API_KEY")
    if api_key:
        return api_key
    
    # Try to read from .env file
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.startswith("API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    
    return None


def format_issues(issues: list) -> str:
    """Format issues list for display."""
    if not issues:
        return "None"
    return "\n    ".join([f"• {issue}" for issue in issues])


def main():
    """Main script execution."""
    parser = argparse.ArgumentParser(
        description="Check available workspaces for stale data"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=os.getenv("BACKEND_URL", "http://localhost:8000"),
        help="Backend API URL (default: BACKEND_URL env var or http://localhost:8000)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON"
    )
    
    args = parser.parse_args()
    
    # Get API key
    api_key = get_api_key()
    if not api_key:
        print("⚠️  Warning: API_KEY not found. Some endpoints may require authentication.")
        print("   Set API_KEY environment variable or add it to .env file")
        print()
    
    # Build request
    url = f"{args.url}/api/v1/workspace/cleanup/check"
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    
    print("=" * 80)
    print("🔍 Checking Available Workspaces for Stale Data")
    print("=" * 80)
    print(f"Backend URL: {args.url}")
    print()
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        
        if args.json:
            print(json.dumps(data, indent=2))
            return
        
        # Display summary
        print(f"Total workspaces checked: {data['total']}")
        print(f"✅ Clean workspaces: {data['clean']}")
        print(f"🧹 Workspaces needing cleaning: {data['needs_cleaning']}")
        print()
        
        if data['needs_cleaning'] == 0:
            print("✨ All available workspaces are clean!")
            return
        
        # Display workspaces that need cleaning
        print("=" * 80)
        print("Workspaces Needing Cleaning:")
        print("=" * 80)
        print()
        
        needs_cleaning = [w for w in data['workspaces'] if not w.get('is_clean')]
        
        for i, ws in enumerate(needs_cleaning, 1):
            ws_id = ws['workspace_id']
            issues = ws.get('issues', [])
            error = ws.get('error')
            
            print(f"{i}. {ws_id}")
            print(f"   Status: {ws.get('status', 'unknown')}")
            
            if error:
                print(f"   ❌ Error: {error}")
            else:
                print(f"   Directory exists: {ws.get('directory_exists', False)}")
                print(f"   Git repo exists: {ws.get('git_repo_exists', False)}")
                print(f"   Current branch: {ws.get('current_branch', 'N/A')}")
                print(f"   Has uncommitted changes: {ws.get('has_uncommitted_changes', False)}")
                print(f"   Has commits on main: {ws.get('has_commits_on_main', False)}")
                print(f"   Has generation artifacts: {ws.get('has_estimation_artifacts', False)}")
            
            if issues:
                print("   Issues:")
                for issue in issues:
                    print(f"     • {issue}")
            
            print()
        
        # Show curl command examples
        print("=" * 80)
        print("To clean a workspace, use:")
        print("=" * 80)
        print()
        
        for ws in needs_cleaning[:3]:  # Show first 3 as examples
            ws_id = ws['workspace_id']
            curl_cmd = f"curl -X POST '{args.url}/api/v1/workspace/cleanup/clean' \\"
            if api_key:
                curl_cmd += f"\n  -H 'X-API-Key: {api_key}' \\"
            curl_cmd += "\n  -H 'Content-Type: application/json' \\"
            curl_cmd += f"\n  -d '{{\"workspace_id\": \"{ws_id}\", \"reason\": \"manual_cleanup\"}}'"
            print(curl_cmd)
            print()
        
        if len(needs_cleaning) > 3:
            print(f"... and {len(needs_cleaning) - 3} more workspace(s)")
            print()
    
    except httpx.HTTPError as e:
        print(f"❌ Failed to connect to backend: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"   Status code: {e.response.status_code}")
            try:
                print(f"   Response: {e.response.text}")
            except Exception:
                pass
        sys.exit(1)
    
    except Exception as e:
        print(f"❌ Script failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
