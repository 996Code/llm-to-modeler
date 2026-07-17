"""
Health Check API

Provides health check endpoints for monitoring.
Version is sourced from the app state to stay in sync with main.py.
"""

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(request: Request):
    """
    Health check endpoint.

    Returns the current health status of the application.
    """
    return {
        "status": "healthy",
        "service": "LLM Form Modeler",
        "version": request.app.version,
        "upstream": getattr(request.app.state, "upstream", None) is not None,
    }


@router.get("/")
async def root(request: Request):
    """
    Root endpoint.

    Returns basic information about the API.
    """
    return {
        "message": "LLM Form Modeler API",
        "version": request.app.version,
        "docs": "/docs",
    }
