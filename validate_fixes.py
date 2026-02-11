#!/usr/bin/env python3
"""
Validation script for ProjectHephaestus fixes.
"""

import sys
import tempfile
import os
from pathlib import Path

def test_imports():
    """Test that all imports work correctly."""
    print("Testing imports...")
    try:
        # Test main package imports
        from hephaestus import (
            slugify, retry_with_backoff, human_readable_size, flatten_dict,
            get_setting, load_config, merge_configs, get_config_value,
            read_file, write_file, load_data, save_data, ensure_directory,
            create_parser, add_logging_args, confirm_action, format_table,
            format_output, register_command, COMMAND_REGISTRY
        )
        print("✓ Main package imports successful")
        
        # Test individual module imports
        from hephaestus.helpers.utils import slugify as helper_slugify
        from hephaestus.utils.helpers import slugify as utils_slugify
        from hephaestus.io.utils import read_file as io_read_file
        from hephaestus.logging.utils import get_logger
        from hephaestus.cli.utils import create_parser as cli_create_parser
        print("✓ Individual module imports successful")
        
        return True
    except Exception as e:
        print(f"✗ Import test failed: {e}")
        return False

def test_slugify_consistency():
    """Test that slugify functions are consistent."""
    print("\nTesting slugify consistency...")
    try:
        from hephaestus import slugify
        from hephaestus.helpers.utils import slugify as helper_slugify
        from hephaestus.utils.helpers import slugify as utils_slugify
        
        test_text = "Hello World! This is a Test."
        result1 = slugify(test_text)
        result2 = helper_slugify(test_text)
        result3 = utils_slugify(test_text)
        
        if result1 == result2 == result3 == "hello-world-this-is-a-test":
            print("✓ Slugify consistency test passed")
            return True
        else:
            print(f"✗ Slugify inconsistency: {result1}, {result2}, {result3}")
            return False
    except Exception as e:
        print(f"✗ Slugify test failed: {e}")
        return False

def test_io_functions():
    """Test I/O functions work correctly."""
    print("\nTesting I/O functions...")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Test write_file and read_file
            test_file = temp_path / "test.txt"
            content = "Hello, World!"
            
            success = write_file(test_file, content)
            if not success:
                print("✗ write_file failed")
                return False
                
            read_content = read_file(test_file)
            if read_content != content:
                print(f"✗ read_file content mismatch: {read_content} != {content}")
                return False
            
            # Test ensure_directory
            test_dir = temp_path / "test" / "nested" / "directory"
            success = ensure_directory(test_dir)
            if not success or not test_dir.exists():
                print("✗ ensure_directory failed")
                return False
                
            print("✓ I/O functions test passed")
            return True
    except Exception as e:
        print(f"✗ I/O test failed: {e}")
        return False

def test_logger():
    """Test logger functionality."""
    print("\nTesting logger...")
    try:
        from hephaestus.logging.utils import get_logger, setup_logging
        
        # Setup basic logging
        setup_logging()
        
        # Get logger
        logger = get_logger(__name__)
        logger.info("Test log message")
        
        # Test context logger
        context_logger = logger.bind(user_id="123", session_id="abc")
        context_logger.info("Context log message")
        
        print("✓ Logger test passed")
        return True
    except Exception as e:
        print(f"✗ Logger test failed: {e}")
        return False

def main():
    """Run all validation tests."""
    print("Running ProjectHephaestus validation tests...")
    
    tests = [
        test_imports,
        test_slugify_consistency,
        test_io_functions,
        test_logger
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"Test {test.__name__} crashed: {e}")
            failed += 1
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
