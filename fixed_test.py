#!/usr/bin/env python3
"""
Fixed validation test for ProjectHephaestus.
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
        return True
    except Exception as e:
        print(f"✗ Import test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_io_operations():
    """Test I/O operations work correctly."""
    print("\n=== Testing I/O Operations ===")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Test read_file/write_file - these should be available directly
            test_file = temp_path / "test.txt"
            content = "Hello, ProjectHephaestus!"
            
            # Test the actual signatures from io/utils.py
            from hephaestus.io.utils import write_file, read_file
            
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
            
            # Test get_setting - first load the config, then get values from it
            from hephaestus import get_setting, load_config
            
            # Load the config first
            loaded_config = load_config(str(config_file))
            
            # Now get settings from the loaded config
            db_host = get_setting(loaded_config, "database.host", "default-host")
            api_timeout = get_setting(loaded_config, "api.timeout", 60)
            username = get_setting(loaded_config, "database.credentials.username", "guest")
            
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

def main():
    """Run key tests."""
    print("Running key ProjectHephaestus validation tests...\n")
    
    tests = [
        ("Imports", test_all_imports),
        ("I/O Operations", test_io_operations),
        ("Configuration Utilities", test_configuration_utilities)
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
        print("🎉 KEY TESTS PASSED! ProjectHephaestus fixes are working correctly.")
        return True
    else:
        print(f"⚠️  {failed} test(s) failed. Please review the issues above.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
