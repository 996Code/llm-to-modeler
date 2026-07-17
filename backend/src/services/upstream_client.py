"""
Upstream Client

HTTP client for calling upstream njmind-modeler API (:7001).
This project is a BRIDGE — all data formats (templates, schemas, guide,
validation) come from upstream via HTTP.

Key upstream endpoints:
  GET  /api/mcp/templates/list-templates     → ["simple_form.json", ...]
  GET  /api/mcp/templates/{filename}         → template JSON
  GET  /api/mcp/schemas/list-schemas         → ["form-config.schema.json", ...]
  GET  /api/mcp/schemas/{filename}           → schema JSON
  GET  /api/mcp/guides/guide.json            → guide JSON
  POST /api/mcp/forms/validate?mode=CREATE   → {pass: bool, errors: [str], warnings: [str]}
  GET  /api/mcp/forms/{formCode}             → FormConfigVo JSON
  POST /api/mcp/forms/create                 → {success, message}
  POST /api/mcp/forms/{formCode}/update      → {success, formCode, message}

Note: create/update/validate bodies are BARE FormConfigVo JSON (no wrapper).
"""

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Thread-local storage for per-request forwarded headers.
# Set by the workflow runner before invoking nodes, read by every upstream call.
_forward_headers = threading.local()


def set_forward_headers(headers: Optional[Dict[str, str]]):
    """Set forwarded headers for the current thread (per-request scope)."""
    if headers:
        _forward_headers.value = dict(headers)
    else:
        _forward_headers.value = None


def _get_forward_headers() -> Dict[str, str]:
    """Get forwarded headers for the current thread (or empty dict)."""
    return getattr(_forward_headers, 'value', None) or {}


class UpstreamConfig:
    """Upstream server configuration."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7001",
        timeout: int = 30,
        cache_ttl: int = 300,  # 5 minutes
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl


class UpstreamClient:
    """
    HTTP client for upstream njmind-modeler API.

    All data (templates, schemas, guide, validation) is fetched from
    upstream. This project does NOT store or generate these locally.
    """

    def __init__(self, config: Optional[UpstreamConfig] = None):
        import os

        if config is None:
            config = UpstreamConfig(
                base_url=os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:7001"),
                timeout=int(os.getenv("UPSTREAM_TIMEOUT", "30")),
                cache_ttl=int(os.getenv("UPSTREAM_CACHE_TTL", "300")),
            )

        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout,
        )
        self._cache: Dict[str, tuple] = {}  # key → (data, timestamp)

        logger.info(f"UpstreamClient initialized: {config.base_url}")

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Merge forwarded headers (from embed/parent system) with any extra."""
        headers = _get_forward_headers()
        if extra:
            headers = {**headers, **extra}
        return headers or None

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached item if not expired."""
        if key in self._cache:
            data, ts = self._cache[key]
            if time.time() - ts < self.config.cache_ttl:
                return data
        return None

    def _set_cached(self, key: str, data: Any):
        """Cache an item."""
        self._cache[key] = (data, time.time())

    # ── Templates ──────────────────────────────────────────────

    def list_templates(self) -> List[str]:
        """Get list of template filenames from upstream."""
        try:
            resp = self._client.get("/api/mcp/templates/list-templates", headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to list templates: {e}")
            return []

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get a template from upstream.

        Args:
            name: Template filename (e.g., "simple_form", "simple_form.json")
                  — auto-appends .json if missing.
        """
        filename = name if name.endswith(".json") else f"{name}.json"
        cache_key = f"template:{filename}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get(f"/api/mcp/templates/{filename}", headers=self._headers())
            resp.raise_for_status()
            template = resp.json()
            self._set_cached(cache_key, template)
            return template
        except Exception as e:
            logger.error(f"Failed to get template '{filename}': {e}")
            return None

    # ── Schemas ────────────────────────────────────────────────

    def list_schemas(self) -> List[str]:
        """Get list of schema filenames from upstream."""
        try:
            resp = self._client.get("/api/mcp/schemas/list-schemas", headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to list schemas: {e}")
            return []

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get a JSON Schema from upstream.

        Args:
            name: Schema filename (e.g., "form-config", "form-config.schema.json")
        """
        filename = name if name.endswith(".json") else f"{name}.schema.json"
        cache_key = f"schema:{filename}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get(f"/api/mcp/schemas/{filename}", headers=self._headers())
            resp.raise_for_status()
            schema = resp.json()
            self._set_cached(cache_key, schema)
            return schema
        except Exception as e:
            logger.error(f"Failed to get schema '{filename}': {e}")
            return None

    # ── Guide ──────────────────────────────────────────────────

    def get_guide(self) -> Optional[Dict[str, Any]]:
        """Get guide.json from upstream."""
        cache_key = "guide"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get("/api/mcp/guides/guide.json", headers=self._headers())
            resp.raise_for_status()
            guide = resp.json()
            self._set_cached(cache_key, guide)
            return guide
        except Exception as e:
            logger.error(f"Failed to get guide: {e}")
            return None

    # ── Validation (delegated to upstream) ─────────────────────

    def validate_form(
        self,
        form_config: Dict[str, Any],
        mode: str = "CREATE",
    ) -> Dict[str, Any]:
        """
        Validate form configuration via upstream API.

        Upstream returns: {pass: bool, errors: [str], warnings: [str]}
        We normalize to:  {valid: bool, errors: [{message: str}], warnings: [str]}

        Args:
            form_config: FormConfigVo JSON (sent as bare body)
            mode: "CREATE" or "UPDATE"

        Returns:
            Normalized validation result dict.
        """
        try:
            resp = self._client.post(
                "/api/mcp/forms/validate",
                params={"mode": mode},
                json=form_config,  # bare JSON body, no wrapper
                headers=self._headers(),
            )
            resp.raise_for_status()
            raw = resp.json()

            # Normalize upstream response format
            # upstream: {pass: bool, errors: [str], warnings: [str]}
            # normalized: {valid: bool, errors: [{message}], warnings: [str]}
            is_valid = raw.get("pass", False)
            raw_errors = raw.get("errors", [])
            normalized_errors = [
                {"message": e} if isinstance(e, str) else e
                for e in raw_errors
            ]

            return {
                "valid": is_valid,
                "errors": normalized_errors,
                "warnings": raw.get("warnings", []),
            }

        except Exception as e:
            logger.error(f"Upstream validation failed: {e}")
            return {
                "valid": False,
                "errors": [{"message": f"Upstream validation request failed: {e}"}],
                "warnings": [],
            }

    # ── Forms CRUD ─────────────────────────────────────────────

    def get_form(self, form_code: str) -> Optional[Dict[str, Any]]:
        """Get an existing form configuration by formCode."""
        try:
            resp = self._client.get(f"/api/mcp/forms/{form_code}", headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get form '{form_code}': {e}")
            return None

    def create_form(self, form_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Create a new form via upstream API (bare JSON body)."""
        try:
            resp = self._client.post("/api/mcp/forms/create", json=form_config, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to create form: {e}")
            return None

    def update_form(
        self,
        form_code: str,
        form_config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Update an existing form via upstream API (bare JSON body)."""
        try:
            resp = self._client.post(
                f"/api/mcp/forms/{form_code}/update",
                json=form_config,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to update form '{form_code}': {e}")
            return None

    # ── Health ─────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Check if upstream is reachable."""
        try:
            resp = self._client.get("/api/mcp/guides/guide.json", timeout=5, headers=self._headers())
            return resp.status_code == 200
        except Exception:
            return False

    def clear_cache(self):
        """Clear the in-memory cache."""
        self._cache.clear()
        logger.info("Upstream cache cleared")
