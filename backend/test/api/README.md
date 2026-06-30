# API Router Testing Guide

This directory contains unit tests for FastAPI router endpoints. The tests use mocking to isolate the API layer from service dependencies.

## Testing Approach

### Architecture

API router tests follow this pattern:

1. **Mock Dependencies**: Services (`WorkspacePoolService`, `GenerationService`) are mocked
2. **Override FastAPI Dependencies**: Use FastAPI's `dependency_overrides` mechanism
3. **Test HTTP Layer**: Use FastAPI's `TestClient` for HTTP request/response testing
4. **Isolate File Operations**: Use temporary directories for file system operations

### Key Components

#### `conftest.py`
Provides reusable fixtures:
- `test_app`: Minimal FastAPI app
- `client`: TestClient instance
- `temp_workspace_dir`: Temporary directory for file operations
- `mock_workspace_pool`: Mock WorkspacePoolService
- `mock_generation_service`: Mock GenerationService
- `sample_tar_archive`: Sample tar.gz archive for testing

#### Test Files
Each router has its own test file:
- `test_specifications.py`: Tests for specification endpoints
- `test_generations.py`: Tests for generation endpoints (to be created)
- `test_workspaces.py`: Tests for workspace endpoints (to be created)

## Running Tests

```bash
# Run all API tests
cd backend
uv run pytest test/api/ -v

# Run specific test file
uv run pytest test/api/test_specifications.py -v

# Run specific test class
uv run pytest test/api/test_specifications.py::TestDownloadSpecificationOutputs -v

# Run specific test
uv run pytest test/api/test_specifications.py::TestDownloadSpecificationOutputs::test_download_outputs_success -v
```

## Writing New Tests

### Example: Testing a Simple GET Endpoint

```python
def test_get_endpoint_success(client, mock_service):
    """Test successful GET request."""
    # Setup mock
    mock_service.get_data.return_value = {"key": "value"}
    
    # Make request
    response = client.get("/api/v1/resource/123")
    
    # Assertions
    assert response.status_code == 200
    assert response.json() == {"key": "value"}
    mock_service.get_data.assert_called_once_with("123")
```

### Example: Testing POST with File Upload

```python
def test_post_with_file(client, mock_service, sample_tar_archive):
    """Test POST with file upload."""
    files = {"archive": ("file.tar.gz", io.BytesIO(sample_tar_archive), "application/gzip")}
    data = {"param": "value"}
    
    response = client.post(
        "/api/v1/endpoint",
        data=data,
        files=files
    )
    
    assert response.status_code == 200
```

### Example: Testing Error Cases

```python
def test_endpoint_error_handling(client, mock_service):
    """Test error handling."""
    mock_service.operation.side_effect = Exception("Service error")
    
    response = client.post("/api/v1/endpoint", json={"data": "value"})
    
    assert response.status_code == 500
    assert "error" in response.json()
```

## Mocking Patterns

### Mocking Async Services

```python
from unittest.mock import AsyncMock

mock_service.async_method = AsyncMock(return_value="result")
```

### Mocking File Operations

```python
from pathlib import Path
import tempfile

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)
```

### Mocking Request State

For endpoints that access `request.state` (set by auth middleware):

```python
from unittest.mock import patch, MagicMock

with patch("app.api.v1.router.Request") as mock_request:
    mock_request_obj = MagicMock()
    mock_request_obj.state.user_email = "test@example.com"
    mock_request.return_value = mock_request_obj
```

## Testing Complex Endpoints

For endpoints that call workflows or modify global state:

1. **Mock Workflows**: Use `patch` to mock workflow functions
2. **Mock Settings**: Use `patch` to mock settings modifications
3. **Use Context Managers**: Ensure cleanup with `try/finally` or context managers

Example:

```python
def test_complex_endpoint(client, mock_service):
    with patch("app.api.v1.router.workflow_function", new_callable=AsyncMock) as mock_workflow:
        with patch("app.api.v1.router.settings") as mock_settings:
            mock_workflow.return_value = MockResult()
            # ... test code ...
```

## Best Practices

1. **Isolate Tests**: Each test should be independent
2. **Use Fixtures**: Reuse common setup via fixtures
3. **Mock External Dependencies**: Don't call real services or workflows
4. **Test Error Cases**: Include tests for error scenarios
5. **Verify Mock Calls**: Assert that mocked services were called correctly
6. **Clean Up**: Use fixtures with `yield` for proper cleanup

## Current Coverage

- ✅ `test_specifications.py`: Download outputs endpoint (fully tested)
- ⚠️ `test_specifications.py`: Check completeness endpoint (structure ready, needs async mocking refinement)
- 📝 `test_generations.py`: To be created
- 📝 `test_workspaces.py`: To be created

## Notes

- The `check-completeness` endpoint requires complex async mocking due to workflow dependencies
- Consider using `pytest-asyncio` for more robust async testing
- For integration testing, see `test/middleware/` and `test/services/`
