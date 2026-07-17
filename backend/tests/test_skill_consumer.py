"""
Tests for Skill Consumer module.
"""

import pytest
from pathlib import Path


def test_skill_consumer_loads_skills(skill_consumer):
    """Test that SkillConsumer loads skill files correctly."""
    skills = skill_consumer.get_skills()
    assert len(skills) > 0, "Should load at least one skill"


def test_skill_consumer_loads_schemas(skill_consumer):
    """Test that SkillConsumer loads schema files correctly."""
    schemas = skill_consumer.get_schemas()
    assert len(schemas) > 0, "Should load at least one schema"
    assert "form-config" in schemas, "Should load form-config schema"


def test_skill_consumer_loads_templates(skill_consumer):
    """Test that SkillConsumer loads template files correctly."""
    templates = skill_consumer.get_templates()
    assert len(templates) > 0, "Should load at least one template"


def test_skill_consumer_loads_guide(skill_consumer):
    """Test that SkillConsumer loads guide.json correctly."""
    guide = skill_consumer.get_guide()
    assert guide is not None, "Should load guide.json"
    assert "fieldTypes" in guide, "Guide should contain fieldTypes"


def test_skill_consumer_loads_rules(skill_consumer):
    """Test that SkillConsumer loads RULES.md correctly."""
    rules = skill_consumer.get_rules()
    assert rules is not None, "Should load RULES.md"
    assert len(rules) > 0, "RULES.md should not be empty"


def test_skill_consumer_get_skill_content(skill_consumer):
    """Test that SkillConsumer can get skill content."""
    skills = skill_consumer.get_skills()
    if skills:
        skill_name = skills[0]
        content = skill_consumer.get_skill(skill_name)
        assert content is not None, f"Should get content for skill {skill_name}"
        assert len(content) > 0, f"Skill {skill_name} content should not be empty"


def test_skill_consumer_get_schema(skill_consumer):
    """Test that SkillConsumer can get specific schema."""
    schema = skill_consumer.get_schema("form-config")
    assert schema is not None, "Should get form-config schema"
    assert "type" in schema, "Schema should have type field"


def test_skill_consumer_get_template(skill_consumer):
    """Test that SkillConsumer can get specific template."""
    templates = skill_consumer.get_templates()
    if templates:
        template_name = templates[0]
        template = skill_consumer.get_template(template_name)
        assert template is not None, f"Should get template {template_name}"


def test_skill_consumer_get_field_types(skill_consumer):
    """Test that SkillConsumer can get field types."""
    field_types = skill_consumer.get_field_types()
    assert len(field_types) > 0, "Should load field types from guide.json"


def test_skill_consumer_cache_update(skill_consumer):
    """Test that SkillConsumer cache can be reloaded."""
    initial_count = len(skill_consumer.get_schemas())
    # Reload by stopping and starting again
    skill_consumer.stop()
    skill_consumer.start()
    updated_count = len(skill_consumer.get_schemas())
    assert updated_count == initial_count, "Cache reload should maintain same count"
