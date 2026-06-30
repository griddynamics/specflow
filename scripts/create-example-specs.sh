#!/bin/bash
# Create Example Specifications for E2E Testing
#
# This script creates example specification files in a temporary directory
# that can be used for testing the SpecFlow backend with MCP tools.
#
# Usage:
#   ./scripts/create-example-specs.sh [OUTPUT_DIR]
#
# Default OUTPUT_DIR: /tmp/specflow-e2e-specs

set -e

OUTPUT_DIR="${1:-/tmp/specflow-e2e-specs}"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "Creating Example Specifications"
echo "=========================================="
echo "Output directory: $OUTPUT_DIR"
echo ""

# Create directory
mkdir -p "$OUTPUT_DIR"

# Create example specification files
cat > "$OUTPUT_DIR/README.md" << 'EOF'
# Example Project Specification

This is an example specification folder for testing the SpecFlow backend.

## Project Overview

Build a simple task management web application with user authentication.

## Features

- User registration and login
- Create, read, update, and delete tasks
- Task filtering and search
- User profile management

## Technology Requirements

- **Frontend**: React 18+ with TypeScript
- **Backend**: Node.js 20+ with Express.js 4.x
- **Database**: PostgreSQL 14+
- **Authentication**: JWT tokens
- **Deployment**: Docker Compose

## Architecture

- **Data Persistence**: External Database (PostgreSQL)
- **Infrastructure**: Containerized (Docker/Podman)
- **Scale Target**: Small Team (<100 concurrent users)
- **Quality**: MVP (Critical path tests only, basic linting)

## API Endpoints

- POST /api/auth/register - Register new user
- POST /api/auth/login - Login user
- GET /api/tasks - List user tasks
- POST /api/tasks - Create new task
- PUT /api/tasks/:id - Update task
- DELETE /api/tasks/:id - Delete task

## Testing

- Unit tests for backend services
- Integration tests for API endpoints
- Basic E2E tests for critical flows

## Deployment

- Docker Compose for local development
- Environment variables for configuration
- PostgreSQL database container

EOF

cat > "$OUTPUT_DIR/requirements.md" << 'EOF'
# Detailed Requirements

## User Authentication

### Registration
- User must provide: email, password, full name
- Email must be unique
- Password must be at least 8 characters
- On success, return JWT token

### Login
- User provides email and password
- On success, return JWT token
- Token expires after 24 hours

## Task Management

### Create Task
- User must be authenticated
- Task fields: title (required), description (optional), due_date (optional), status (default: "pending")
- Status values: "pending", "in_progress", "completed"

### List Tasks
- Return all tasks for authenticated user
- Support filtering by status
- Support search by title/description
- Pagination: 20 items per page

### Update Task
- User can only update their own tasks
- All fields are optional (partial update)
- Validate status transitions

### Delete Task
- User can only delete their own tasks
- Soft delete (mark as deleted, don't remove from database)

## User Profile

### View Profile
- Return user information (email, full name, created_at)
- Exclude sensitive information

### Update Profile
- User can update full name
- Email cannot be changed

EOF

cat > "$OUTPUT_DIR/tech-stack.md" << 'EOF'
# Technology Stack Specification

## Frontend

- **Framework**: React 18.2+
- **Language**: TypeScript 5.0+
- **Build Tool**: Vite 4.0+
- **State Management**: React Context API
- **HTTP Client**: Axios 1.0+
- **Styling**: Tailwind CSS 3.0+
- **Component Library**: shadcn/ui

## Backend

- **Runtime**: Node.js 20 LTS
- **Framework**: Express.js 4.18+
- **Language**: TypeScript 5.0+
- **ORM**: Prisma 5.0+
- **Validation**: Zod 3.22+
- **Authentication**: jsonwebtoken 9.0+

## Database

- **Type**: PostgreSQL 14+
- **ORM**: Prisma Client
- **Migrations**: Prisma Migrate

## Infrastructure

- **Containerization**: Docker 24.0+
- **Orchestration**: Docker Compose 2.20+
- **Environment**: Development (local)

## Testing

- **Unit Tests**: Jest 29.0+
- **Integration Tests**: Supertest 6.3+
- **E2E Tests**: Playwright 1.40+

## Code Quality

- **Linter**: ESLint 8.0+
- **Formatter**: Prettier 3.0+
- **Type Checking**: TypeScript compiler

EOF

cat > "$OUTPUT_DIR/api-design.md" << 'EOF'
# API Design Specification

## Base URL

- Development: `http://localhost:3000/api`
- Production: `https://api.example.com/api`

## Authentication

All protected endpoints require a JWT token in the Authorization header:

```
Authorization: Bearer <token>
```

## Endpoints

### Authentication

#### POST /auth/register
Register a new user.

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123",
  "fullName": "John Doe"
}
```

**Response (201):**
```json
{
  "token": "<jwt-token>",
  "user": {
    "id": "123",
    "email": "user@example.com",
    "fullName": "John Doe"
  }
}
```

#### POST /auth/login
Login with email and password.

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Response (200):**
```json
{
  "token": "<jwt-token>",
  "user": {
    "id": "123",
    "email": "user@example.com",
    "fullName": "John Doe"
  }
}
```

### Tasks

#### GET /tasks
List all tasks for the authenticated user.

**Query Parameters:**
- `status` (optional): Filter by status (pending, in_progress, completed)
- `search` (optional): Search in title and description
- `page` (optional): Page number (default: 1)
- `limit` (optional): Items per page (default: 20)

**Response (200):**
```json
{
  "tasks": [
    {
      "id": "456",
      "title": "Complete project",
      "description": "Finish the task management app",
      "status": "in_progress",
      "dueDate": "2024-12-31",
      "createdAt": "2024-01-01T00:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total": 1,
    "totalPages": 1
  }
}
```

#### POST /tasks
Create a new task.

**Request Body:**
```json
{
  "title": "New Task",
  "description": "Task description",
  "dueDate": "2024-12-31",
  "status": "pending"
}
```

**Response (201):**
```json
{
  "id": "789",
  "title": "New Task",
  "description": "Task description",
  "status": "pending",
  "dueDate": "2024-12-31",
  "createdAt": "2024-01-01T00:00:00Z"
}
```

#### PUT /tasks/:id
Update an existing task.

**Request Body:** (all fields optional)
```json
{
  "title": "Updated Task",
  "status": "completed"
}
```

**Response (200):**
```json
{
  "id": "789",
  "title": "Updated Task",
  "description": "Task description",
  "status": "completed",
  "dueDate": "2024-12-31",
  "updatedAt": "2024-01-02T00:00:00Z"
}
```

#### DELETE /tasks/:id
Delete a task (soft delete).

**Response (204):** No content

### User Profile

#### GET /profile
Get current user's profile.

**Response (200):**
```json
{
  "id": "123",
  "email": "user@example.com",
  "fullName": "John Doe",
  "createdAt": "2024-01-01T00:00:00Z"
}
```

#### PUT /profile
Update current user's profile.

**Request Body:**
```json
{
  "fullName": "Jane Doe"
}
```

**Response (200):**
```json
{
  "id": "123",
  "email": "user@example.com",
  "fullName": "Jane Doe",
  "updatedAt": "2024-01-02T00:00:00Z"
}
```

## Error Responses

All errors follow this format:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {}
  }
}
```

### Common Error Codes

- `VALIDATION_ERROR`: Request validation failed
- `UNAUTHORIZED`: Missing or invalid authentication token
- `FORBIDDEN`: User doesn't have permission
- `NOT_FOUND`: Resource not found
- `CONFLICT`: Resource conflict (e.g., duplicate email)
- `INTERNAL_ERROR`: Server error

EOF

echo -e "${GREEN}✅ Created example specifications${NC}"
echo ""
echo -e "${BLUE}Files created:${NC}"
ls -lh "$OUTPUT_DIR"
echo ""
echo -e "${YELLOW}Specification directory: $OUTPUT_DIR${NC}"
echo ""
echo "You can now use this directory with SpecFlow MCP tools:"
echo "  spec_path: $OUTPUT_DIR"
