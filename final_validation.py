#!/usr/bin/env python3
"""
Final validation script for ProjectHephaestus implementation.
"""

import sys
from pathlib import Path

# Add src to path for direct imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_imports():
    """Test that all modules can be imported."""
    try:
        from hephaestus import slugify, human_readable_size
        from hephaestus.config.utils import get_setting
        from hephaestus.io.utils import ensure_directory
        from hephaestus.utils.helpers import slugify as helper_slugify
        print("✅ All imports successful")
        return True
    except Exception as e:
        print(f"❌ Import failed: {e}")
        return False

def test_functions():
    """Test core functionality."""
    try:
        from hephaestus import slugify
        
        # Test slugify
        assert slugify("Hello World") == "hello-world"
        assert slugify("My Project v1.0!") == "my-project-v1-0"
        print("✅ Slugify function working correctly")
        
        from hephaestus.utils.helpers import human_readable_size
        assert human_readable_size(1024) == "1.0 KB"
        print("✅ Human readable size function working correctly")
        
        return True
    except Exception as e:
        print(f"❌ Function test failed: {e}")
        return False

def main():
    """Run final validation."""
    print("Running final ProjectHephaestus validation...")
    print("=" * 50)
    
    import_success = test_imports()
    function_success = test_functions()
    
    if import_success and function_success:
        print("\n🎉 PROJECT HEPHAESTUS IMPLEMENTATION VALIDATED!")
        print("\nReady for cross-repository utility consolidation.")
        return 0
    else:
        print("\n❌ VALIDATION FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main())
