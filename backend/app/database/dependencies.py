"""
FastAPI dependency injection for database.

This module provides a FastAPI dependency that returns the database instance.
Use this in your API endpoints to get access to the database.

Example:
    from fastapi import APIRouter, Depends
    from app.database.dependencies import get_db
    from app.database import IDatabase
    
    router = APIRouter()
    
    @router.get("/workspaces")
    async def list_workspaces(db: IDatabase = Depends(get_db)):
        workspaces = db.query("workspaces", filters=[("status", "==", "available")])
        return {"workspaces": workspaces}
"""

from app.database import IDatabase, get_database


def get_db() -> IDatabase:
    """
    FastAPI dependency that returns the database instance.
    
    The database implementation is selected based on the DATABASE_TYPE
    environment variable. This dependency can be injected into any
    FastAPI route handler.
    
    Returns:
        IDatabase: Configured database instance
        
    Example:
        @router.post("/generation-sessions")
        async def create_generation_session(
            data: GenerationCreate,
            db: IDatabase = Depends(get_db)
        ):
            generation_id = str(uuid.uuid4())
            db.set(COL_GENERATION_SESSIONS, generation_id, {
                "status": "pending",
                "created_at": datetime.now(UTC),
                **data.dict()
            })
            return {"id": generation_id}
    """
    return get_database()
