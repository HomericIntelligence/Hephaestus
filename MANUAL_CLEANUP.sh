#!/usr/bin/env bash
# Manual cleanup script for ProjectHephaestus consolidation
# Run this script to complete the implementation

set -e  # Exit on error

echo "=== ProjectHephaestus Manual Cleanup ==="
echo ""

# Check we're in the right directory
if [ ! -f "CLAUDE.md" ]; then
    echo "Error: Must run from ProjectHephaestus root directory"
    exit 1
fi

echo "Step 1: Deleting obsolete directories..."
rm -rf shared/
rm -rf tools/
rm -rf hephaestus/shared/
echo "✓ Deleted: shared/, tools/, hephaestus/shared/"

echo ""
echo "Step 2: Deleting ad-hoc test scripts..."
rm -f verify_setup.py
rm -f manual_test.py
rm -f validate_implementation.py
rm -f final_validation.py
rm -f comprehensive_test.py
rm -f end_to_end_test.py
rm -f fixed_test.py
rm -f validate_fixes.py
rm -f verify_ported_utilities.py
echo "✓ Deleted 9 ad-hoc test scripts"

echo ""
echo "Step 3: Deleting deployment stubs..."
rm -rf scripts/deployment/
echo "✓ Deleted: scripts/deployment/"

echo ""
echo "=== Cleanup Complete ==="
echo ""
echo "Next steps:"
echo "1. Run verification: python -c \"import hephaestus; print(hephaestus.__version__)\""
echo "2. Run tests: python -m pytest tests/ -v"
echo "3. Review IMPLEMENTATION_SUMMARY.md for full details"
echo "4. Commit changes: git add -A && git commit -m 'feat: Port Odyssey scripts and consolidate codebase to v0.2.0'"
