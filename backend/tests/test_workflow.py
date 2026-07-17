"""
Tests for LangGraph workflow.
"""

import pytest
from src.graph.state import AgentState


def test_agent_state_initialization():
    """Test that AgentState initializes correctly."""
    state = AgentState()
    assert state.skill_name == "njmind-form-field-create"
    assert state.conversation_history == []
    assert state.current_config is None
    assert state.validation_errors == []
    assert state.retry_count == 0
    assert state.max_retries == 3


def test_agent_state_to_dict():
    """Test AgentState to_dict method."""
    state = AgentState(
        skill_name="test-skill",
        conversation_history=[{"role": "user", "content": "test"}]
    )
    state_dict = state.to_dict()
    assert isinstance(state_dict, dict)
    assert state_dict["skill_name"] == "test-skill"
    assert len(state_dict["conversation_history"]) == 1


def test_agent_state_from_dict():
    """Test AgentState from_dict method."""
    state_dict = {
        "skill_name": "test-skill",
        "conversation_history": [{"role": "user", "content": "test"}],
        "current_config": None,
        "validation_errors": [],
        "retry_count": 0,
        "max_retries": 3
    }
    state = AgentState.from_dict(state_dict)
    assert state.skill_name == "test-skill"
    assert len(state.conversation_history) == 1


@pytest.mark.asyncio
async def test_workflow_initialization(workflow):
    """Test that workflow initializes correctly."""
    assert workflow is not None
    assert workflow.skill_consumer is not None
    assert workflow.llm_client is not None
    assert workflow.schema_validator is not None


@pytest.mark.asyncio
async def test_workflow_run_simple(workflow):
    """Test workflow with simple form generation request."""
    # This test requires a valid LLM API key
    # Skip if running in CI without API key
    import os
    if not os.getenv("LLM_API_KEY") or os.getenv("LLM_API_KEY") == "test-key":
        pytest.skip("Skipping test that requires LLM API key")
    
    state = await workflow.run(
        user_input="创建一个简单的表单，包含姓名和邮箱字段",
        conversation_history=[],
        current_config=None,
        skill_name="njmind-form-field-create"
    )
    
    assert state is not None
    assert state.is_complete is True
    assert state.current_config is not None
    assert isinstance(state.current_config, dict)


@pytest.mark.asyncio
async def test_workflow_run_with_history(workflow):
    """Test workflow with conversation history."""
    import os
    if not os.getenv("LLM_API_KEY") or os.getenv("LLM_API_KEY") == "test-key":
        pytest.skip("Skipping test that requires LLM API key")
    
    history = [
        {"role": "user", "content": "我想创建一个表单"},
        {"role": "assistant", "content": "好的，请告诉我表单的用途"}
    ]
    
    state = await workflow.run(
        user_input="创建一个请假申请表",
        conversation_history=history,
        current_config=None,
        skill_name="njmind-form-field-create"
    )
    
    assert state is not None
    assert state.is_complete is True


@pytest.mark.asyncio
async def test_workflow_run_update(workflow):
    """Test workflow with update operation."""
    import os
    if not os.getenv("LLM_API_KEY") or os.getenv("LLM_API_KEY") == "test-key":
        pytest.skip("Skipping test that requires LLM API key")
    
    current_config = {
        "formCode": "test_form",
        "formName": "Test Form",
        "formFieldConfigVos": [
            {
                "fieldTitleKey": "name",
                "fieldTitleText": "姓名",
                "formFieldType": 0
            }
        ]
    }
    
    state = await workflow.run(
        user_input="添加一个邮箱字段",
        conversation_history=[],
        current_config=current_config,
        skill_name="njmind-form-field-update"
    )
    
    assert state is not None
    assert state.is_complete is True
    assert state.current_config is not None
