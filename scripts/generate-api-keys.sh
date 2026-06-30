#!/bin/bash

# Script to generate API keys for multiple users
# Usage: ./scripts/generate-api-keys.sh --emails "a@mail.com b@mail.com c@mail.com"
#        ./scripts/generate-api-keys.sh --emails "a@mail.com b@mail.com" --expires-days 365
#        ./scripts/generate-api-keys.sh --emails "a@mail.com" --permissions "read,write"
#        ./scripts/generate-api-keys.sh --emails "a@mail.com" --workspace-pool hf
#        ./scripts/generate-api-keys.sh --emails "a@mail.com" --github-token ghp_xxx
#        ./scripts/generate-api-keys.sh --key-uid "uuid-here" --github-token ghp_xxx

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"

# Default values
BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
EXPIRES_DAYS="${EXPIRES_DAYS:-}"
PERMISSIONS="${PERMISSIONS:-user}"
SPECFLOW_API_KEY="${SPECFLOW_API_KEY:-}"
NOTIFY_EMAIL_USERNAME="${NOTIFY_EMAIL_USERNAME:-}"
WORKSPACE_POOL=""
GITHUB_TOKEN_PAT=""
KEY_UID=""

# Parse command line arguments
EMAILS=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --emails)
            EMAILS="$2"
            shift 2
            ;;
        --expires-days)
            EXPIRES_DAYS="$2"
            shift 2
            ;;
        --permissions)
            PERMISSIONS="$2"
            shift 2
            ;;
        --backend-url)
            BACKEND_URL="$2"
            shift 2
            ;;
        --workspace-pool)
            WORKSPACE_POOL="$2"
            shift 2
            ;;
        --github-token)
            GITHUB_TOKEN_PAT="$2"
            shift 2
            ;;
        --key-uid)
            KEY_UID="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --emails EMAILS           Space-separated list of email addresses (required)"
            echo "  --expires-days DAYS       Number of days until expiration (optional, default: never)"
            echo "  --permissions PERMS       Comma-separated permissions: user,admin (optional, default: user)"
            echo "  --backend-url URL         Backend API URL (optional, default: http://localhost:8000)"
            echo "  --workspace-pool POOL     Workspace pool name (optional, default: default)"
            echo "  --github-token TOKEN      GitHub PAT to store encrypted on the key after creation (optional)"
            echo "  --key-uid UID             If a key with this uid exists, skip creation and only update github-token"
            echo "  --help, -h                Show this help message"
            echo ""
            echo "Environment variables (from .env):"
            echo "  SPECFLOW_API_KEY              API key for authenticating requests (required)"
            echo "  NOTIFY_EMAIL_USERNAME                User email for X-User-Email header (required)"
            echo "  BACKEND_URL               Backend API URL (default: http://localhost:8000)"
            echo "  EXPIRES_DAYS              Default expiration days"
            echo "  PERMISSIONS               Default permissions"
            echo ""
            echo "Examples:"
            echo "  $0 --emails \"a@mail.com b@mail.com c@mail.com\""
            echo "  $0 --emails \"user@example.com\" --expires-days 365"
            echo "  $0 --emails \"admin@example.com\" --permissions \"read,write,admin\""
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Load .env file if it exists
if [ -f "$ENV_FILE" ]; then
    echo -e "${BLUE}Loading environment variables from .env file...${NC}"
    set -a
    source "$ENV_FILE"
    set +a
    
    # Override with .env values if not set via command line
    if [ -z "$BACKEND_URL" ] || [ "$BACKEND_URL" = "http://localhost:8000" ]; then
        BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
    fi
    if [ -z "$EXPIRES_DAYS" ] && [ -n "${EXPIRES_DAYS:-}" ]; then
        EXPIRES_DAYS="${EXPIRES_DAYS}"
    fi
    if [ -z "$PERMISSIONS" ] || [ "$PERMISSIONS" = "*" ]; then
        PERMISSIONS="${PERMISSIONS:-user}"
    fi
    # Load SPECFLOW_API_KEY and NOTIFY_EMAIL_USERNAME from .env
    SPECFLOW_API_KEY="${SPECFLOW_API_KEY:-}"
    NOTIFY_EMAIL_USERNAME="${NOTIFY_EMAIL_USERNAME:-}"
else
    echo -e "${YELLOW}Warning: .env file not found at $ENV_FILE${NC}"
    echo -e "${YELLOW}Using default values${NC}"
fi

# Normalize workspace pool: lowercase, default to "default"
WORKSPACE_POOL=$(echo "${WORKSPACE_POOL:-default}" | tr '[:upper:]' '[:lower:]')

# Validate required arguments
if [ -z "$EMAILS" ] && [ -z "$KEY_UID" ]; then
    echo -e "${RED}Error: --emails is required (or --key-uid for token-only update)${NC}"
    echo "Use --help for usage information"
    exit 1
fi

# Validate SPECFLOW_API_KEY is set
if [ -z "$SPECFLOW_API_KEY" ]; then
    echo -e "${RED}Error: SPECFLOW_API_KEY is required${NC}"
    echo "Please set SPECFLOW_API_KEY in your .env file or export it as an environment variable"
    echo "Example: SPECFLOW_API_KEY=specflow_xxxxxxxxxxxxx"
    exit 1
fi

# Validate NOTIFY_EMAIL_USERNAME is set
if [ -z "$NOTIFY_EMAIL_USERNAME" ]; then
    echo -e "${RED}Error: NOTIFY_EMAIL_USERNAME is required${NC}"
    echo "Please set NOTIFY_EMAIL_USERNAME in your .env file or export it as an environment variable"
    echo "Example: NOTIFY_EMAIL_USERNAME=admin@example.com"
    exit 1
fi

# Convert permissions to JSON array format
IFS=',' read -ra PERM_ARRAY <<< "$PERMISSIONS"
PERMISSIONS_JSON="["
for i in "${!PERM_ARRAY[@]}"; do
    if [ $i -gt 0 ]; then
        PERMISSIONS_JSON+=","
    fi
    PERMISSIONS_JSON+="\"${PERM_ARRAY[i]}\""
done
PERMISSIONS_JSON+="]"

# Function to check if a key_uid already exists; prints the key_uid if found, empty string if not
find_key_by_uid() {
    local uid="$1"
    local response=$(curl -s -w "\n%{http_code}" -X GET \
        "$BACKEND_URL/api/v1/auth/keys" \
        -H "X-API-Key: $SPECFLOW_API_KEY" \
        -H "X-User-Email: $NOTIFY_EMAIL_USERNAME" 2>&1)
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    if [ "$http_code" -ne 200 ]; then
        echo ""
        return
    fi
    if command -v jq &> /dev/null; then
        echo "$body" | jq -r --arg uid "$uid" '.keys[] | select(.key_uid == $uid) | .key_uid' 2>/dev/null || echo ""
    else
        echo "$body" | grep -o "\"key_uid\":\"$uid\"" | head -1 | grep -o '[^"]*$' || echo ""
    fi
}

# Function to set a GitHub token via PUT /api/v1/auth/github-token
# When target_uid is provided, uses the admin target_key_uid field; otherwise updates the caller's own key.
set_github_token() {
    local auth_key="$1"   # API key to authenticate with
    local token="$2"
    local target_uid="$3" # optional: key_uid of the key to update (admin path)
    echo -e "${BLUE}Setting GitHub token${target_uid:+ for key_uid: ${GREEN}$target_uid${BLUE}}...${NC}"
    local json_payload="{\"token\": \"$token\""
    if [ -n "$target_uid" ]; then
        json_payload+=",\"target_key_uid\": \"$target_uid\""
    fi
    json_payload+="}"
    local response=$(curl -s -w "\n%{http_code}" -X PUT \
        "$BACKEND_URL/api/v1/auth/github-token" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $auth_key" \
        -H "X-User-Email: $NOTIFY_EMAIL_USERNAME" \
        -d "$json_payload" 2>&1)
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    if [ "$http_code" -eq 200 ]; then
        echo -e "${GREEN}✓ GitHub token updated${NC}"
        echo ""
        return 0
    else
        echo -e "${RED}✗ Failed to set GitHub token (HTTP $http_code): $body${NC}"
        echo ""
        return 1
    fi
}

# Function to extract user name from email
extract_user_name() {
    local email="$1"
    # Extract the part before @
    local local_part="${email%%@*}"
    # Replace dots and hyphens with spaces, then capitalize
    local name=$(echo "$local_part" | sed 's/[._-]/ /g' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) tolower(substr($i,2));}1')
    echo "$name"
}

# Function to create API key for a user
create_api_key() {
    local email="$1"
    local user_name=$(extract_user_name "$email")
    
    echo -e "${BLUE}Creating API key for: ${GREEN}$email${NC} (${user_name})"
    
    # Build JSON payload
    local json_payload="{"
    json_payload+="\"user_id\": \"$email\","
    json_payload+="\"user_name\": \"$user_name\","
    json_payload+="\"permissions\": $PERMISSIONS_JSON,"
    json_payload+="\"workspace_pool\": \"$WORKSPACE_POOL\""

    if [ -n "$EXPIRES_DAYS" ]; then
        json_payload+=",\"expires_days\": $EXPIRES_DAYS"
    fi

    json_payload+="}"
    
    # Make API request with authentication
    local response=$(curl -s -w "\n%{http_code}" -X POST \
        "$BACKEND_URL/api/v1/auth/keys" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $SPECFLOW_API_KEY" \
        -H "X-User-Email: $NOTIFY_EMAIL_USERNAME" \
        -d "$json_payload" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    if [ "$http_code" -eq 201 ]; then
        # Extract API key from JSON response (assuming jq is available, fallback to grep)
        if command -v jq &> /dev/null; then
            local api_key=$(echo "$body" | jq -r '.api_key')
            local created_at=$(echo "$body" | jq -r '.created_at')
            local expires_at=$(echo "$body" | jq -r '.expires_at // "Never"')
        else
            # Fallback: extract using grep/sed
            local api_key=$(echo "$body" | grep -o '"api_key":"[^"]*' | cut -d'"' -f4)
            local created_at=$(echo "$body" | grep -o '"created_at":"[^"]*' | cut -d'"' -f4)
            local expires_at=$(echo "$body" | grep -o '"expires_at":"[^"]*' | cut -d'"' -f4 || echo "Never")
        fi
        
        echo -e "${GREEN}✓ Successfully created API key${NC}"
        echo -e "  ${BLUE}API Key:${NC} ${GREEN}$api_key${NC}"
        echo -e "  ${BLUE}User:${NC} $email ($user_name)"
        echo -e "  ${BLUE}Created:${NC} $created_at"
        echo -e "  ${BLUE}Expires:${NC} $expires_at"
        echo -e "  ${BLUE}Permissions:${NC} $PERMISSIONS"
        echo -e "  ${BLUE}Workspace Pool:${NC} $WORKSPACE_POOL"
        echo ""
        echo -e "  ${YELLOW}📋 Copy this for API requests:${NC}"
        echo -e "     ${GREEN}X-API-Key: $api_key${NC}"
        echo ""

        # Set GitHub token on the new key if provided (authenticate as the new key itself)
        if [ -n "$GITHUB_TOKEN_PAT" ]; then
            set_github_token "$api_key" "$GITHUB_TOKEN_PAT"
        fi

        return 0
    else
        echo -e "${RED}✗ Failed to create API key${NC}"
        echo -e "${RED}HTTP Status: $http_code${NC}"
        echo -e "${RED}Response: $body${NC}"
        echo ""
        return 1
    fi
}

# Main execution
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}SpecFlow API Key Generator${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "Backend URL: ${BLUE}$BACKEND_URL${NC}"
echo -e "Using API Key: ${BLUE}${SPECFLOW_API_KEY:0:20}...${NC}"
echo -e "User Email: ${BLUE}$NOTIFY_EMAIL_USERNAME${NC}"
if [ -n "$EXPIRES_DAYS" ]; then
    echo -e "Expires in: ${BLUE}$EXPIRES_DAYS days${NC}"
else
    echo -e "Expires: ${BLUE}Never${NC}"
fi
echo -e "Permissions: ${BLUE}$PERMISSIONS${NC}"
echo -e "Workspace Pool: ${BLUE}$WORKSPACE_POOL${NC}"
echo ""

# If --key-uid is provided, check if it already exists and handle accordingly
if [ -n "$KEY_UID" ]; then
    echo -e "${BLUE}Checking for existing key with uid: ${GREEN}$KEY_UID${NC}"
    existing_uid=$(find_key_by_uid "$KEY_UID")
    if [ -n "$existing_uid" ]; then
        echo -e "${YELLOW}⚠️  Key with uid '$KEY_UID' already exists — skipping creation${NC}"
        if [ -n "$GITHUB_TOKEN_PAT" ]; then
            # Use admin key + target_key_uid to update the token via the single endpoint
            if set_github_token "$SPECFLOW_API_KEY" "$GITHUB_TOKEN_PAT" "$KEY_UID"; then
                echo -e "${GREEN}✅ Done — github token updated for existing key${NC}"
            else
                echo -e "${RED}❌ Failed to update github token${NC}"
                exit 1
            fi
        else
            echo -e "${YELLOW}No --github-token provided; nothing to update.${NC}"
        fi
        exit 0
    else
        echo -e "${BLUE}Key uid not found — proceeding with creation${NC}"
        echo ""
    fi
fi

# Split emails by space and process each
success_count=0
fail_count=0
total_count=0

for email in $EMAILS; do
    # Trim whitespace
    email=$(echo "$email" | xargs)

    if [ -z "$email" ]; then
        continue
    fi

    # Basic email validation
    if [[ ! "$email" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        echo -e "${RED}✗ Invalid email format: $email${NC}"
        fail_count=$((fail_count + 1))
        echo ""
        continue
    fi

    total_count=$((total_count + 1))

    if create_api_key "$email"; then
        success_count=$((success_count + 1))
    else
        fail_count=$((fail_count + 1))
    fi
done

# Summary
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Summary${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "Total emails processed: ${BLUE}$total_count${NC}"
echo -e "Successfully created: ${GREEN}$success_count${NC}"
if [ $fail_count -gt 0 ]; then
    echo -e "Failed: ${RED}$fail_count${NC}"
fi
echo ""

if [ $fail_count -gt 0 ]; then
    exit 1
fi
