"""
Test token sanitization in workspace pool service.

Ensures that sensitive GitHub tokens are not exposed in error messages or logs.
"""

from app.services.workspace_pool import WorkspacePoolService


class TestTokenSanitization:
    """Test cases for token sanitization functionality."""
    
    def test_sanitize_github_personal_access_token(self):
        """Test sanitization of GitHub personal access token (ghp_ prefix)."""
        token = "ghp_FAKE0000000000000000000000000000000000"
        message = f"Command '['git', 'clone', 'https://awrobel-gd:{token}@github.com/griddynamics/generation-workspace1']' returned non-zero exit status 128."
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message, token)
        
        assert token not in sanitized
        assert "[REDACTED]" in sanitized
        assert "github.com" in sanitized
        assert "awrobel-gd" in sanitized
    
    def test_sanitize_github_pat_token(self):
        """Test sanitization of GitHub PAT token (github_pat_ prefix)."""
        message = "Error: github_pat_11AAAA2QQ3ccccddddeeee4ffff failed"
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message)
        
        assert "github_pat_" not in sanitized
        assert "[REDACTED]" in sanitized
    
    def test_sanitize_token_in_url(self):
        """Test sanitization of token embedded in URL."""
        message = "Command '['git', 'clone', 'https://user:ghp_SecretToken123@github.com/org/repo', 'dest']' failed"
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message)
        
        assert "ghp_SecretToken123" not in sanitized
        assert "[REDACTED]" in sanitized
        assert "https://user:[REDACTED]@github.com" in sanitized
    
    def test_sanitize_specific_token(self):
        """Test sanitization with specific token provided."""
        token = "my_secret_token_12345"
        message = f"Failed with token: {token} in URL"
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message, token)
        
        assert token not in sanitized
        assert "[REDACTED]" in sanitized
    
    def test_sanitize_multiple_tokens(self):
        """Test sanitization of multiple tokens in same message."""
        message = (
            "First error with ghp_Token1234567890 and "
            "second with github_pat_SecretXYZ123 "
            "and url https://user:ghp_AnotherToken@github.com/repo"
        )
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message)
        
        assert "ghp_Token1234567890" not in sanitized
        assert "github_pat_SecretXYZ123" not in sanitized
        assert "ghp_AnotherToken" not in sanitized
        assert sanitized.count("[REDACTED]") == 3
    
    def test_sanitize_empty_message(self):
        """Test sanitization handles empty message gracefully."""
        sanitized = WorkspacePoolService._sanitize_token_in_message("")
        assert sanitized == ""
    
    def test_sanitize_none_message(self):
        """Test sanitization handles None message gracefully."""
        sanitized = WorkspacePoolService._sanitize_token_in_message(None)
        assert sanitized is None
    
    def test_sanitize_message_without_tokens(self):
        """Test sanitization doesn't modify messages without tokens."""
        message = "This is a normal error message without any tokens"
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message)
        
        assert sanitized == message
    
    def test_sanitize_preserves_other_content(self):
        """Test sanitization preserves non-sensitive content."""
        token = "ghp_SecretToken"
        message = (
            f"Command '['git', 'clone', 'https://user:{token}@github.com/org/repo']' "
            "returned non-zero exit status 128. "
            "Repository: org/repo, User: user"
        )
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message, token)
        
        assert token not in sanitized
        assert "[REDACTED]" in sanitized
        assert "org/repo" in sanitized
        assert "User: user" in sanitized
        assert "exit status 128" in sanitized
    
    def test_sanitize_calledprocesserror_format(self):
        """Test sanitization of actual CalledProcessError format."""
        # This is the actual format from subprocess.CalledProcessError
        token = "ghp_FAKE0000000000000000000000000000000000"
        message = (
            f"Command '['git', 'clone', 'https://awrobel-gd:{token}@github.com/"
            f"griddynamics/generation-workspace1', 'ws-01-1']' "
            f"returned non-zero exit status 128."
        )
        
        sanitized = WorkspacePoolService._sanitize_token_in_message(message, token)
        
        # Token should be redacted
        assert token not in sanitized
        assert "[REDACTED]" in sanitized
        
        # Other parts should be preserved
        assert "awrobel-gd" in sanitized
        assert "github.com" in sanitized
        assert "griddynamics/generation-workspace1" in sanitized
        assert "ws-01-1" in sanitized
        assert "exit status 128" in sanitized
