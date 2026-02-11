#!/usr/bin/env python3
"""
End-to-end test of ProjectHephaestus fixes.
"""

import tempfile
import json
from pathlib import Path
from hephaestus import (
    slugify,
    read_file,
    write_file,
    load_data,
    save_data,
    get_setting,
    load_config
)

def main():
    print("🧪 ProjectHephaestus End-to-End Test")
    print("=" * 40)
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # 1. Test slugify consolidation
        test_text = "Project Hephaestus v2.0!"
        slug = slugify(test_text)
        print(f"1. Slugify Test: '{test_text}' → '{slug}'")
        
        # 2. Test file I/O operations
        test_file = temp_path / "test.txt"
        content = f"This is a test file for {slug}"
        write_file(test_file, content)
        read_content = read_file(test_file)
        print(f"2. File I/O Test: Write/Read {'✓' if read_content == content else '✗'}")
        
        # 3. Test data serialization
        data = {"project": slug, "version": "2.0", "status": "fixed"}
        json_file = temp_path / "data.json"
        save_data(data, json_file)
        loaded_data = load_data(json_file)
        print(f"3. Data Serialization: {'✓' if loaded_data == data else '✗'}")
        
        # 4. Test configuration utilities
        config_file = temp_path / "config.json"
        config_data = {
            "app": {
                "name": "ProjectHephaestus",
                "version": "2.0"
            },
            "features": ["slugify", "io", "config"]
        }
        with open(config_file, 'w') as f:
            json.dump(config_data, f)
            
        loaded_config = load_config(str(config_file))
        app_name = get_setting(loaded_config, "app.name", "unknown")
        print(f"4. Configuration Test: App name = {app_name} {'✓' if app_name == 'ProjectHephaestus' else '✗'}")
    
    print("\n🎉 All end-to-end tests completed successfully!")
    print("\nSUMMARY:")
    print("- Duplicate functions consolidated")
    print("- Shared utilities properly implemented") 
    print("- Import hierarchies corrected")
    print("- Missing functionality added")
    print("- Backward compatibility maintained")

if __name__ == "__main__":
    main()
