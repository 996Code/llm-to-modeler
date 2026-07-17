"""
Pytest configuration and fixtures.
"""

import os
import sys
from pathlib import Path

import pytest

# Add backend/src to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Set test environment variables
os.environ["SKILLS_DIR"] = str(backend_dir.parent / "skills")
os.environ["LLM_BASE_URL"] = "https://api.openai.com/v1"
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MODEL"] = "gpt-4o-mini"


@pytest.fixture(scope="session")
def skills_dir():
    """Get the skills directory path."""
    return Path(__file__).parent.parent.parent / "skills"


@pytest.fixture
def skill_consumer(skills_dir):
    """Create a SkillConsumer instance for testing."""
    from src.services.skill_consumer import SkillConsumer
    
    consumer = SkillConsumer(str(skills_dir))
    consumer.start()
    yield consumer
    consumer.stop()


@pytest.fixture
def schema_validator(skill_consumer):
    """Create a SchemaValidator instance for testing."""
    from src.services.schema_validator import SchemaValidator
    
    return SchemaValidator(skill_consumer)


@pytest.fixture
def llm_client():
    """Create an LLMClient instance for testing."""
    from src.llm.client import LLMClient
    
    return LLMClient()


@pytest.fixture
def workflow(skill_consumer, llm_client, schema_validator):
    """Create a FormConfigWorkflow instance for testing."""
    from src.graph.graph import create_workflow
    
    return create_workflow(skill_consumer, llm_client, schema_validator)
