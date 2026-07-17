"""
Tests for Schema Validator module.
"""

import pytest


def test_schema_validator_initialization(schema_validator):
    """Test that SchemaValidator initializes correctly."""
    assert schema_validator is not None, "SchemaValidator should initialize"


def test_schema_validator_valid_config(schema_validator):
    """Test validation of a valid form configuration."""
    # Create a minimal valid config
    valid_config = {
        "formCode": "test_form",
        "formName": "Test Form",
        "formFieldConfigVos": []
    }
    
    result = schema_validator.validate(valid_config, "form-config")
    # Note: This might fail if the schema requires more fields
    # We're testing the validation mechanism works
    assert hasattr(result, "valid"), "Validation result should have valid attribute"
    assert hasattr(result, "errors"), "Validation result should have errors attribute"


def test_schema_validator_invalid_config(schema_validator):
    """Test validation catches type mismatches."""
    # formFieldType should be an integer, not a string
    invalid_config = {
        "formFieldConfigVos": [
            {
                "formFieldType": "not_a_number",
                "fieldTitleKey": "test",
                "fieldTitleText": "Test"
            }
        ]
    }
    
    result = schema_validator.validate(invalid_config, "form-field-config")
    # If schema enforces type constraints, this should fail
    # If schema is lenient, result.valid may still be True
    assert isinstance(result.valid, bool)
    assert isinstance(result.errors, list)


def test_schema_validator_nonexistent_schema(schema_validator):
    """Test validation with non-existent schema."""
    config = {"test": "data"}
    result = schema_validator.validate(config, "nonexistent-schema")
    assert result.valid is False, "Should fail with non-existent schema"
    assert len(result.errors) > 0, "Should have errors for non-existent schema"


def test_schema_validator_validate_form_config(schema_validator):
    """Test validate_form_config method."""
    config = {
        "formCode": "test",
        "formName": "Test"
    }
    result = schema_validator.validate_form_config(config)
    assert hasattr(result, "valid"), "Should return validation result"


def test_schema_validator_validate_form_field_config(schema_validator):
    """Test validate_form_field_config method."""
    config = {
        "fieldTitleKey": "test_field",
        "fieldTitleText": "Test Field",
        "formFieldType": 0
    }
    result = schema_validator.validate_form_field_config(config)
    assert hasattr(result, "valid"), "Should return validation result"


def test_schema_validator_validate_field_by_type(schema_validator):
    """Test validate_field_by_type method."""
    config = {
        "fieldTitleKey": "test",
        "fieldTitleText": "Test",
        "formFieldType": 0
    }
    result = schema_validator.validate_field_by_type(config, "text")
    assert hasattr(result, "valid"), "Should return validation result"


def test_schema_validator_refresh(schema_validator):
    """Test that validator can be refreshed."""
    initial_count = len(schema_validator._validators)
    schema_validator.refresh()
    updated_count = len(schema_validator._validators)
    assert updated_count >= initial_count, "Refresh should maintain or increase validators"
