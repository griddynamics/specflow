"""Pytest configuration and shared fixtures for backend tests."""

from pathlib import Path
import sys

# Add the app directory to Python path for imports
app_dir = Path(__file__).parent / "app"
sys.path.insert(0, str(app_dir.parent))

