#!/usr/bin/env python3
"""
Get and print an active API key from the database.

This script queries the database for active API keys and prints the first one found.
Useful for E2E setup and testing.
"""

import sys
import os
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Set DATABASE_TYPE early: emulator auto-detect takes priority (manually-run emulator),
# otherwise default to sqlite (the local/Docker-dev default).
if os.getenv("FIRESTORE_EMULATOR_HOST") and not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "emulator"
elif not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "sqlite"

from app.database.factory import get_database

def main():
    """Get and print an active API key."""
    try:
        db = get_database()
        
        # Query for active API keys
        api_keys = db.query("api_keys")
        
        if not api_keys:
            print("⚠️  No API keys found in database")
            print("   Run 'make init-db' to create one")
            sys.exit(1)
        
        # Find active keys
        active_keys = [
            k for k in api_keys 
            if k.get("is_active", True) and k.get("api_key")
        ]
        
        if not active_keys:
            print("⚠️  No active API keys found")
            sys.exit(1)
        
        # Print the first active key
        api_key = active_keys[0].get("api_key")
        user_id = active_keys[0].get("user_id", "unknown")
        
        print(f"🔑 API Key: {api_key}")
        print(f"   User: {user_id}")
        print(f"   Total active keys: {len(active_keys)}")
        
        # Also print it in a format that's easy to copy
        print("")
        print("📋 Copy this for API requests:")
        print(f"   X-API-Key: {api_key}")
        print("")
        print("📋 Example curl command:")
        print(f"   curl -H 'X-API-Key: {api_key}' http://localhost:8000/api/v1/workspace/pool/status")
        
    except Exception as e:
        print(f"❌ Error fetching API key: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
