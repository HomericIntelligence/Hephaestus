#!/usr/bin/env python3
"""
Validation script for ProjectHephaestus implementation.

This script verifies that all core components are functional
without requiring external dependencies or installations.
"""

import sys
import os
import tempfile
from pathlib import Path

# Add current directory to path for direct imports
sys.path.insert(0, str(Path(__file__).parent))

def validate_core_utilities():
    """Validate core utility functions."""
    print("Validating ProjectHephaestus core utilities...")
    print("=" * 50)
    
    try:
        # Test configuration utilities
        from hephaestus.config.utils import get_setting
        config = {"database": {"host": "localhost", "port": 5432}}
        host = get_setting(config, "database.host")
        assert host == "localhost"
        print("✅ Configuration utilities: PASSED")
        
        # Test general helpers
        from hephaestus.utils.helpers import slugify, human_readable_size
        slug = slugify("My Project Name")
        assert slug == "my-project-name"
        size_str = human_readable_size(1024)
        assert size_str == "1.0 KB"
        print("✅ General helpers: PASSED")
        
        # Test I/O utilities
        from hephaestus.io.utils import ensure_directory
        with tempfile.TemporaryDirectory() as temp_dir:
            test_path = Path(temp_dir) / "test" / "directory"
            result = ensure_directory(test_path)
            assert result == True
            assert test_path.exists()
        print("✅ I/O utilities: PASSED")
        
        # Test CLI utilities
        from hephaestus.cli.utils import create_parser
        parser = create_parser("Test parser")
        assert parser.description == "Test parser"
        print("✅ CLI utilities: PASSED")
        
        print("\n🎉 ALL CORE UTILITIES VALIDATED SUCCESSFULLY!")
        return True
        
    except Exception as e:
        print(f"❌ Validation failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def validate_scripts_structure():
    """Validate scripts and tools structure.""" 
    print("\nValidating scripts and tools structure...")
    print("=" * 50)
    
    required_paths = [
        "scripts/README.md",
        "tools/README.md", 
        "shared/README.md",
        "shared/utils/common.py",
        "scripts/deployment/deploy_utils.py"
    ]
    
    base_path = Path(__file__).parent
    missing_paths = []
    
    for path in required_paths:
        full_path = base_path / path
        if not full_path.exists():
            missing_paths.append(path)
            
    if missing_paths:
        print(f"❌ Missing paths: {missing_paths}")
        return False
    else:
        print("✅ Scripts structure: PASSED")
        return True

def main():
    """Run all validations."""
    print("ProjectHephaestus Validation Suite")
    print("=" * 50)
    
    core_ok = validate_core_utilities()
    struct_ok = validate_scripts_structure()
    
    print("\n" + "=" * 50)
    if core_ok and struct_ok:
        print("🎉 OVERALL VALIDATION: PASSED")
        print("\nProjectHephaestus is ready for use!")
        return 0
    else:
        print("❌ OVERALL VALIDATION: FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(main())
