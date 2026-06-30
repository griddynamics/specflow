#!/bin/bash
# Quick test script for authentication system
# Tests all auth endpoints and scenarios locally

set -e

echo "🧪 SpecFlow Backend Authentication - Local Testing Script"
echo "====================================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if backend is running
echo "📡 Checking if backend is running..."
if ! curl -s http://localhost:8000/health > /dev/null; then
    echo -e "${RED}❌ Backend is not running!${NC}"
    echo ""
    echo "Start the backend with:"
    echo "  make run"
    echo "  or: docker-compose up -d"
    exit 1
fi
echo -e "${GREEN}✅ Backend is running${NC}"
echo ""

# Test 1: Create API Key
echo "Test 1: Creating API key..."
RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/auth/keys" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test@example.com",
    "user_name": "Test User",
    "expires_days": 365
  }')

API_KEY=$(echo $RESPONSE | jq -r '.api_key')
USER_ID=$(echo $RESPONSE | jq -r '.user_id')

if [ "$API_KEY" = "null" ] || [ -z "$API_KEY" ]; then
    echo -e "${RED}❌ Failed to create API key${NC}"
    echo "Response: $RESPONSE"
    exit 1
fi

echo -e "${GREEN}✅ API key created${NC}"
echo "   User: $USER_ID"
echo "   Key: ${API_KEY:0:20}..."
echo ""

# Test 2: Try accessing protected endpoint WITHOUT auth
echo "Test 2: Testing protected endpoint WITHOUT auth..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/v1/workspace/pool/status)

if [ "$STATUS" = "401" ]; then
    echo -e "${GREEN}✅ Protected endpoint correctly requires auth (401)${NC}"
else
    echo -e "${RED}❌ Expected 401, got $STATUS${NC}"
    exit 1
fi
echo ""

# Test 3: Try with auth
echo "Test 3: Testing protected endpoint WITH auth..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/workspace/pool/status)

if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}✅ Authentication successful (200)${NC}"
    
    # Show the response
    POOL_STATUS=$(curl -s -H "X-API-Key: $API_KEY" \
      http://localhost:8000/api/v1/workspace/pool/status)
    echo "   Pool Status: $(echo $POOL_STATUS | jq -c '.total, .available')"
else
    echo -e "${RED}❌ Expected 200, got $STATUS${NC}"
    exit 1
fi
echo ""

# Test 4: Test Bearer token format
echo "Test 4: Testing Bearer token format..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $API_KEY" \
  http://localhost:8000/api/v1/workspace/pool/status)

if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}✅ Bearer token format works (200)${NC}"
else
    echo -e "${RED}❌ Expected 200, got $STATUS${NC}"
    exit 1
fi
echo ""

# Test 5: List API keys
echo "Test 5: Listing API keys..."
KEYS=$(curl -s http://localhost:8000/api/v1/auth/keys)
TOTAL=$(echo $KEYS | jq -r '.total')

if [ "$TOTAL" -ge "1" ]; then
    echo -e "${GREEN}✅ API keys listed successfully${NC}"
    echo "   Total keys: $TOTAL"
else
    echo -e "${RED}❌ Expected at least 1 key, got $TOTAL${NC}"
    exit 1
fi
echo ""

# Test 6: Create another key
echo "Test 6: Creating second API key..."
RESPONSE2=$(curl -s -X POST "http://localhost:8000/api/v1/auth/keys" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice@example.com",
    "user_name": "Alice"
  }')

API_KEY2=$(echo $RESPONSE2 | jq -r '.api_key')

if [ "$API_KEY2" = "null" ] || [ -z "$API_KEY2" ]; then
    echo -e "${RED}❌ Failed to create second API key${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Second API key created${NC}"
echo "   Key: ${API_KEY2:0:20}..."
echo ""

# Test 7: Revoke a key
echo "Test 7: Revoking Alice's API key..."
API_KEY_PREFIX="${API_KEY2:0:15}"
REVOKE_RESPONSE=$(curl -s -X DELETE "http://localhost:8000/api/v1/auth/keys/$API_KEY_PREFIX")

if echo $REVOKE_RESPONSE | jq -e '.message' > /dev/null; then
    echo -e "${GREEN}✅ API key revoked${NC}"
else
    echo -e "${RED}❌ Failed to revoke API key${NC}"
    echo "Response: $REVOKE_RESPONSE"
    exit 1
fi
echo ""

# Test 8: Verify revoked key doesn't work
echo "Test 8: Verifying revoked key is rejected..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: $API_KEY2" \
  http://localhost:8000/api/v1/workspace/pool/status)

if [ "$STATUS" = "401" ]; then
    echo -e "${GREEN}✅ Revoked key correctly rejected (401)${NC}"
else
    echo -e "${RED}❌ Expected 401, got $STATUS${NC}"
    exit 1
fi
echo ""

# Test 9: Reactivate the key
echo "Test 9: Reactivating Alice's API key..."
REACTIVATE_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/auth/keys/$API_KEY_PREFIX/reactivate")

if echo $REACTIVATE_RESPONSE | jq -e '.message' > /dev/null; then
    echo -e "${GREEN}✅ API key reactivated${NC}"
else
    echo -e "${RED}❌ Failed to reactivate API key${NC}"
    echo "Response: $REACTIVATE_RESPONSE"
    exit 1
fi
echo ""

# Test 10: Verify reactivated key works
echo "Test 10: Verifying reactivated key works..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: $API_KEY2" \
  http://localhost:8000/api/v1/workspace/pool/status)

if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}✅ Reactivated key works (200)${NC}"
else
    echo -e "${RED}❌ Expected 200, got $STATUS${NC}"
    exit 1
fi
echo ""

# Test 11: Public endpoints
echo "Test 11: Testing public endpoints (no auth required)..."
declare -a PUBLIC_ENDPOINTS=(
    "/health"
    "/health/live"
    "/health/ready"
    "/docs"
    "/openapi.json"
)

ALL_PUBLIC_PASSED=true
for ENDPOINT in "${PUBLIC_ENDPOINTS[@]}"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000$ENDPOINT")
    if [ "$STATUS" = "200" ]; then
        echo -e "   ${GREEN}✅${NC} $ENDPOINT"
    else
        echo -e "   ${RED}❌${NC} $ENDPOINT (got $STATUS)"
        ALL_PUBLIC_PASSED=false
    fi
done

if [ "$ALL_PUBLIC_PASSED" = true ]; then
    echo -e "${GREEN}✅ All public endpoints accessible${NC}"
else
    echo -e "${RED}❌ Some public endpoints failed${NC}"
    exit 1
fi
echo ""

# Summary
echo "=================================================="
echo -e "${GREEN}🎉 All authentication tests passed!${NC}"
echo "=================================================="
echo ""
echo "Summary:"
echo "  ✅ API key creation"
echo "  ✅ Authentication middleware"
echo "  ✅ X-API-Key header format"
echo "  ✅ Bearer token format"
echo "  ✅ API key listing"
echo "  ✅ API key revocation"
echo "  ✅ API key reactivation"
echo "  ✅ Public endpoints"
echo ""
echo "Your test API key (save for manual testing):"
echo "  $API_KEY"
echo ""
echo "Test it manually:"
echo "  curl -H 'X-API-Key: $API_KEY' http://localhost:8000/api/v1/workspace/pool/status"
echo ""
