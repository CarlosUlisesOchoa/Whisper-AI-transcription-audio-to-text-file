import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Paths that skip authentication (useful for monitoring)
PUBLIC_PATHS = {"/health"}


class SecurityMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces API key authentication."""

    async def dispatch(self, request: Request, call_next):
        # CORS preflight requests do not include API auth headers.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip auth for public endpoints
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # --- API Key ---
        api_key = os.environ.get("API_KEY", "").strip()
        if api_key:
            request_key = request.headers.get("X-API-Key", "")
            if request_key != api_key:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid or missing API key"},
                )

        return await call_next(request)
