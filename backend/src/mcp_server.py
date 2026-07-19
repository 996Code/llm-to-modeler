"""
MCP Server — exposes form config tools to AI clients (Claude Code, etc.)

Tools:
  get_form_config(description)    → generate FormConfig from natural language
  validate_form(config)           → validate via upstream
  list_templates()                → list available templates
  get_template(name)              → get template JSON
  get_guide()                     → get guide JSON

Resources:
  njmind://guide                  → guide JSON
"""

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def create_mcp_server(upstream, dispatcher=None):
    """
    Create MCP server with tools and resources.

    Args:
        upstream: UpstreamClient instance
        dispatcher: ToolDispatcher instance(新架构,替代旧 workflow)
    """
    mcp = FastMCP("llm-form-modeler")

    # ── Tools ──────────────────────────────────────────────────

    @mcp.tool()
    def get_form_config(description: str) -> str:
        """
        Generate a form configuration JSON from natural language description.

        Args:
            description: Natural language description of the form to create.
                         e.g., "创建一个请假申请表，包含申请人、日期、原因"

        Returns:
            Generated FormConfig JSON string.
        """
        if not dispatcher:
            return json.dumps({"error": "Dispatcher not configured"}, ensure_ascii=False)

        result = dispatcher.run(user_input=description, conv_id="mcp")
        if result.artifact:
            return json.dumps({
                "config": result.artifact,
                "valid": len(result.extra.get("validation_errors", [])) == 0,
                "errors": result.extra.get("validation_errors", []),
            }, ensure_ascii=False, indent=2)
        elif result.error_for_llm:
            return json.dumps({"error": result.error_for_llm}, ensure_ascii=False)
        return json.dumps({"error": "No config generated"}, ensure_ascii=False)

    @mcp.tool()
    def validate_form(config: str, mode: str = "CREATE") -> str:
        """
        Validate a form configuration via upstream API.

        Args:
            config: FormConfig JSON string to validate.
            mode: "CREATE" or "UPDATE".

        Returns:
            Validation result JSON string.
        """
        try:
            form_config = json.loads(config)
        except json.JSONDecodeError as e:
            return json.dumps({"valid": False, "error": f"Invalid JSON: {e}"})

        result = upstream.validate_form(form_config, mode=mode)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_templates() -> str:
        """List all available template names from upstream."""
        templates = upstream.list_templates()
        return json.dumps(templates, ensure_ascii=False)

    @mcp.tool()
    def get_template(name: str) -> str:
        """
        Get a template by name from upstream.

        Args:
            name: Template name (e.g., "simple_form").
        """
        template = upstream.get_template(name)
        if not template:
            return json.dumps({"error": f"Template '{name}' not found"})
        return json.dumps(template, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_guide() -> str:
        """Get the field type guide from upstream."""
        guide = upstream.get_guide()
        if not guide:
            return json.dumps({"error": "Guide not found"})
        return json.dumps(guide, ensure_ascii=False, indent=2)

    # ── Resources ──────────────────────────────────────────────

    @mcp.resource("njmind://guide")
    def guide_resource() -> str:
        """Field type guide and keyword index."""
        guide = upstream.get_guide()
        return json.dumps(guide or {}, ensure_ascii=False, indent=2)

    @mcp.resource("njmind://templates")
    def templates_resource() -> str:
        """Available templates list."""
        return json.dumps(upstream.list_templates(), ensure_ascii=False)

    return mcp
