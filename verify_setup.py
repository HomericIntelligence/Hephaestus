
#!/usr/bin/env python3
"""Simple verification script for ProjectHephaestus."""

import sys
import os
from pathlib import Path

print("Verifying ProjectHephaestus Setup...")
print("=" * 40)

# Check if key files exist
required_files = [
    "pixi.toml",
    "pyproject.toml", 
    "setup.py",
    "README.md",
    "src/hephaestus/__init__.py"
]

all_good = True
for file_path in required_files:
    if os.path.exists(file_path):
        print(f"✓ {file_path} exists")
    else:
        print(f"✗ {file_path} missing")
        all_good = False

# Try importing core modules
if all_good:
    try:
        # Add src to path
        sys.path.insert(0, "src")
        from hephaestus.utils.helpers import slugify, human_readable_size
        from hephaestus.config.utils import get_setting
        
        # Test basic functionality
        test_slug = slugify("Project Hephaestus Test")
        assert test_slug == "project-hephaestus-test", f"Expected 'project-hephaestus-test', got '{test_slug}'"
        print("✓ Core utilities working correctly")
        
    except Exception as e:
        print(f"✗ Import test failed: {e}")
        all_good = False

if all_good:
    print("\n🎉 ProjectHephaestus verification PASSED")
    print("Ready for cross-repository utility consolidation!")
else:
    print("\n❌ ProjectHephaestus verification FAILED")
    print("Please check the implementation.")

print("\nDirectory structure:")
os.system("find . -name '*.py' -o -name '*.md' -o -name '*.toml' | grep -E 'hephaestus|pixi|README' | head -10")
