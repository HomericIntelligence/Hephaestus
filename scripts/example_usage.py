#!/usr/bin/env python3
"""
Example usage of ProjectHephaestus utilities.

This script demonstrates how to use the various utility modules
in ProjectHephaestus.
"""

import argparse
import sys
import tempfile
from pathlib import Path

# Import our utilities
from hephaestus.config.utils import load_config, get_setting
from hephaestus.logging.utils import get_logger, setup_logging
from hephaestus.io.utils import ensure_directory, safe_write
from hephaestus.utils.helpers import slugify, human_readable_size
from hephaestus.cli.utils import create_parser, add_logging_args


def main():
    """Main function demonstrating utility usage."""
    # Setup logging
    setup_logging()
    logger = get_logger(__name__)
    logger.info("Starting example script")
    
    # Demonstrate CLI utilities
    parser = create_parser(description="ProjectHephaestus example script")
    add_logging_args(parser)
    parser.add_argument("--output", "-o", default="output.txt",
                       help="Output file path")
    args = parser.parse_args()
    
    # Demonstrate general utilities
    print("Slugifying 'My Project Name':", slugify("My Project Name"))
    print("Human readable size of 1048576 bytes:", human_readable_size(1048576))
    
    # Demonstrate I/O utilities
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # Create directory
        data_dir = temp_path / "data"
        ensure_directory(data_dir)
        print(f"Created directory: {data_dir}")
        
        # Write data
        sample_data = {
            "name": "ProjectHephaestus",
            "version": "0.1.0",
            "modules": ["config", "logging", "io", "utils", "cli"]
        }
        
        json_file = data_dir / "sample.json"
        safe_write(json_file, str(sample_data).replace("'", '"'))
        print(f"Wrote data to: {json_file}")
        
        # Demonstrate configuration utilities would go here
        # (would normally load from actual config file)
        config_example = {
            "database": {
                "host": "localhost",
                "port": 5432
            },
            "api": {
                "timeout": 30
            }
        }
        
        db_host = get_setting(config_example, "database.host", "default-host")
        api_timeout = get_setting(config_example, "api.timeout", 60)
        
        print(f"Database host from config: {db_host}")
        print(f"API timeout from config: {api_timeout}")
    
    logger.info("Example script completed successfully")


if __name__ == "__main__":
    main()
