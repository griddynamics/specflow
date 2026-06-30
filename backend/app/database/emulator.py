"""
Firestore emulator database implementation for local development.

Identical to FirestoreDatabase but automatically connects to the Firestore emulator
when FIRESTORE_EMULATOR_HOST environment variable is set.
"""

import os
from typing import Optional

from app.database.firestore import FirestoreDatabase


class EmulatorDatabase(FirestoreDatabase):
    """
    Firestore emulator database implementation.
    
    Inherits all functionality from FirestoreDatabase but ensures it connects
    to the local emulator. The Firestore SDK automatically detects the emulator
    when FIRESTORE_EMULATOR_HOST is set in the environment.
    
    Usage:
        # In docker-compose.yml or .env:
        FIRESTORE_EMULATOR_HOST=firestore-emulator:8080
        
        # In code:
        db = EmulatorDatabase(project_id="local-dev")
    """

    def __init__(self, project_id: Optional[str] = None, database: str = "default"):
        """
        Initialize Firestore client connected to emulator.
        
        Args:
            project_id: Project ID for emulator. Defaults to "local-dev" if not provided.
            database: Firestore database name. Defaults to "default" (which maps to "(default)" database).
        
        Raises:
            RuntimeError: If FIRESTORE_EMULATOR_HOST is not set
        """
        emulator_host = os.getenv("FIRESTORE_EMULATOR_HOST")
        
        if not emulator_host:
            raise RuntimeError(
                "FIRESTORE_EMULATOR_HOST environment variable must be set to use EmulatorDatabase. "
                "Example: FIRESTORE_EMULATOR_HOST=localhost:8080"
            )
        
        # Use default project_id for local development if not provided
        if project_id is None:
            project_id = "local-dev"
        
        # Initialize parent class (FirestoreDatabase)
        # The Firestore SDK will automatically connect to the emulator
        # because FIRESTORE_EMULATOR_HOST is set
        super().__init__(project_id=project_id, database=database)
