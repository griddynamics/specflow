import logging
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("api.middleware")

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        # Process the request
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            status_code = 500
            raise e
        finally:
            process_time = time.time() - start_time
            log_data = {
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "process_time": f"{process_time:.4f}s",
                "client": request.client.host if request.client else "unknown"
            }
            logger.info(str(log_data))
        
        return response

