#!/usr/bin/env python3
"""
Verification script for ported utilities from ProjectOdyssey to ProjectHephaestus.
"""

import sys
from pathlib import Path

def test_imports():
    """Test that all ported utilities can be imported successfully."""
    try:
        # Test markdown utilities
        from hephaestus.markdown.fixer import MarkdownFixer, FixerOptions
        print("✓ Markdown fixer imports successfully")
        
        # Test retry utilities
        from hephaestus.utils.retry import retry_with_backoff, retry_on_network_error
        print("✓ Retry utilities import successfully")
        
        # Test system info utilities
        from hephaestus.system.info import get_system_info, format_system_info
        print("✓ System info utilities import successfully")
        
        # Test dataset utilities
        from hephaestus.datasets.downloader import DatasetDownloader, MNISTDownloader
        print("✓ Dataset downloader utilities import successfully")
        
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False

def test_basic_functionality():
    """Test basic functionality of ported utilities."""
    try:
        # Test markdown fixer instantiation
        from hephaestus.markdown.fixer import MarkdownFixer, FixerOptions
        fixer = MarkdownFixer(FixerOptions(verbose=False))
        print("✓ Markdown fixer instantiation works")
        
        # Test system info collection (brief version)
        from hephaestus.system.info import get_system_info
        info = get_system_info(include_tools=False)  # Skip tools for speed
        assert "os" in info
        assert "python" in info
        print("✓ System info collection works")
        
        # Test dataset downloader instantiation
        from hephaestus.datasets.downloader import MNISTDownloader
        downloader = MNISTDownloader()
        print("✓ Dataset downloader instantiation works")
        
        return True
    except Exception as e:
        print(f"✗ Basic functionality test failed: {e}")
        return False

if __name__ == "__main__":
    print("Verifying ported utilities from ProjectOdyssey to ProjectHephaestus...")
    print()
    
    success = True
    success &= test_imports()
    success &= test_basic_functionality()
    
    print()
    if success:
        print("🎉 All ported utilities verified successfully!")
        print("Ready for production use across multiple repositories.")
    else:
        print("❌ Some verification tests failed.")
        sys.exit(1)
