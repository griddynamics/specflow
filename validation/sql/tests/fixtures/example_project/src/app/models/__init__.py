"""Example models: the package re-exports them so `app.models:Base.metadata` resolves."""

from app.models.tables import AuditLog, Base, CacheEntry, Customer, CustomerOrder

__all__ = ["AuditLog", "Base", "CacheEntry", "Customer", "CustomerOrder"]
