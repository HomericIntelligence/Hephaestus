#!/usr/bin/env python3
"""
Comprehensive validation test for ProjectHephaestus fixes.
"""

import sys
import tempfile
import json
from pathlib import Path

def test_all_imports():
    """Test that all functions can be imported correctly."""
    print("=== Testing All Imports ===")
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
        
        # Test individual modules
        from hephaestus.helpers.utils import slugify as helpers_slugify
        from hephaestus.utils.helpers import slugify as utils_slugify
        from hephaestus.io.utils import read_file as io_read_file, write_file as io_write_file
        from hephaestus.config.utils import load_config as config_load_config
        from hephaestus.logging.utils import get_logger
        from hephaestus.cli.utils import create_parser as cli_parser
        
        print("✓ All individual module imports successful")
        return True
    except Exception as e:
        print(f"✗ Import test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_slugify_consistency():
    """Test that all slugify functions produce consistent results."""
    print("\n=== Testing Slugify Consistency ===")
    try:
        from hephaestus import slugify
        from hephaestus.helpers.utils import slugify as helpers_slugify
        from hephaestus.utils.helpers import slugify as utils_slugify
        
        test_cases = [
            "Hello World",
            "My Project v1.0!",
            "Special@#$Characters",
            "  Extra   Spaces  ",
            "UPPERCASE-text"
        ]
        
        for test_case in test_cases:
            result1 = slugify(test_case)
            result2 = helpers_slugify(test_case)
            result3 = utils_slugify(test_case)
            
            if result1 == result2 == result3:
                print(f"  ✓ '{test_case}' -> '{result1}'")
            else:
                print(f"  ✗ Inconsistent results for '{test_case}': {result1}, {result2}, {result3}")
                return False
        
        print("✓ All slugify functions consistent")
        return True
    except Exception as e:
        print(f"✗ Slugify consistency test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_io_operations():
    """Test I/O operations work correctly."""
    print("\n=== Testing I/O Operations ===")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Test read_file/write_file
            test_file = temp_path / "test.txt"
            content = "Hello, ProjectHephaestus!"
            
            # Write file using hephaestus function
            success = write_file(test_file, content)
            if not success:
                print("✗ write_file failed")
                return False
            
            # Read file using hephaestus function
            read_content = read_file(test_file)
            if read_content != content:
                print(f"✗ read_file content mismatch: '{read_content}' != '{content}'")
                return False
            
            print("  ✓ Basic read/write test passed")
            
            # Test JSON data operations
            json_file = temp_path / "test.json"
            test_data = {"name": "ProjectHephaestus", "version": "0.1.0", "features": ["config", "logging", "io"]}
            
            # Save data
            success = save_data(test_data, json_file)
            if not success:
                print("✗ save_data failed")
                return False
            
            # Load data
            loaded_data = load_data(json_file)
            if loaded_data != test_data:
                print(f"✗ load_data content mismatch: {loaded_data} != {test_data}")
                return False
            
            print("  ✓ JSON data operations test passed")
            
            # Test directory creation
            test_dir = temp_path / "test" / "nested" / "directories"
            success = ensure_directory(test_dir)
            if not success or not test_dir.exists():
                print("✗ ensure_directory failed")
                return False
                
            print("  ✓ Directory creation test passed")
        
        print("✓ All I/O operations passed")
        return True
    except Exception as e:
        print(f"✗ I/O operations test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_configuration_utilities():
    """Test configuration utilities."""
    print("\n=== Testing Configuration Utilities ===")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Create test config
            config_data = {
                "database": {
                    "host": "localhost",
                    "port": 5432,
                    "credentials": {
                        "username": "admin",
                        "password": "secret"
                    }
                },
                "api": {
                    "timeout": 30,
                    "endpoints": ["/users", "/posts"]
                }
            }
            
            # Save config as JSON
            config_file = temp_path / "config.json"
            with open(config_file, 'w') as f:
                json.dump(config_data, f)
            
            # Test get_setting
            from hephaestus import get_setting
            
            db_host = get_setting(str(config_file), "database.host", "default-host")
            api_timeout = get_setting(str(config_file), "api.timeout", 60)
            username = get_setting(str(config_file), "database.credentials.username", "guest")
            
            if db_host != "localhost":
                print(f"✗ get_setting db_host failed: {db_host}")
                return False
                
            if api_timeout != 30:
                print(f"✗ get_setting api_timeout failed: {api_timeout}")
                return False
                
            if username != "admin":
                print(f"✗ get_setting username failed: {username}")
                return False
            
            print("  ✓ Configuration utilities test passed")
        
        print("✓ All configuration tests passed")
        return True
    except Exception as e:
        print(f"✗ Configuration utilities test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_logging_utilities():
    """Test logging utilities."""
    print("\n=== Testing Logging Utilities ===")
    try:
        from hephaestus.logging.utils import get_logger, setup_logging, ContextLogger
        
        # Setup logging
        setup_logging()
        
        # Get logger
        logger = get_logger(__name__)
        if not isinstance(logger, ContextLogger):
            print("✗ get_logger did not return ContextLogger")
            return False
        
        # Test logging works
        logger.info("Test log message")
        logger.bind(user_id="123").info("Context log message")
        
        print("✓ Logging utilities test passed")
        return True
    except Exception as e:
        print(f"✗ Logging utilities test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_cli_utilities():
    """Test CLI utilities."""
    print("\n=== Testing CLI Utilities ===")
    try:
        from hephaestus.cli.utils import create_parser, add_logging_args, format_table, format_output
        
        # Test parser creation
        parser = create_parser("test-cli")
        if parser.prog != "test-cli":
            print("✗ create_parser failed")
            return False
        
        # Test adding logging args
        add_logging_args(parser)
        
        # Test table formatting
        headers = ["Name", "Version", "Status"]
        rows = [
            ["ProjectA", "1.0.0", "Active"],
            ["ProjectB", "2.1.0", "Inactive"],
            ["ProjectC", "0.5.0", "Beta"]
        ]
        
        table_output = format_table(rows, headers)
        if not table_output or "Name" not in table_output:
            print("✗ format_table failed")
            return False
        
        # Test output formatting
        data = {"status": "success", "items": [1, 2, 3]}
        json_output = format_output(data, "json")
        if "status" not in json_output:
            print("✗ format_output failed")
            return False
        
        print("✓ CLI utilities test passed")
        return True
    except Exception as e:
        print(f"✗ CLI utilities test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all comprehensive tests."""
    print("Running comprehensive ProjectHephaestus validation tests...\n")
    
    tests = [
        ("Imports", test_all_imports),
        ("Slugify Consistency", test_slugify_consistency),
        ("I/O Operations", test_io_operations),
        ("Configuration Utilities", test_configuration_utilities),
        ("Logging Utilities", test_logging_utilities),
        ("CLI Utilities", test_cli_utilities)
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
                print(f"✅ {test_name} PASSED\n")
            else:
                failed += 1
                print(f"❌ {test_name} FAILED\n")
        except Exception as e:
            print(f"💥 {test_name} CRASHED: {e}\n")
            failed += 1
    
    print("=" * 50)
    print(f"FINAL RESULTS: {passed} passed, {failed} failed")
    print("=" * 50)
    
    if failed == 0:
        print("🎉 ALL TESTS PASSED! ProjectHephaestus fixes are working correctly.")
        return True
    else:
        print(f"⚠️  {failed} test(s) failed. Please review the issues above.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
