#!/usr/bin/env python3
"""
Consolidated utilities from HomericIntelligence projects.

Functions consolidated from:
- ProjectOdyssey/scripts/plot_training.py
- ProjectScylla/scripts/install_tools.py
- And other shared utility scripts
"""

import subprocess
import argparse
import sys
import os
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

def run_subprocess(cmd: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run subprocess command with proper error handling.
    
    Consolidated from ProjectOdyssey shared/utils.py
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True
        )
        return result
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed: {' '.join(cmd)}")
        print(f"[ERROR] stderr: {e.stderr}")
        raise

def get_proj_root(proj_name: str) -> str:
    """Get absolute path to project root.
    
    Consolidated from ProjectOdyssey shared/utils.py
    """
    proj_env_var = f"{proj_name.upper()}_ROOT"
    proj_root = os.environ.get(proj_env_var)
    
    if not proj_root:
        # Fallback to relative path approach
        current_dir = Path.cwd()
        while current_dir != current_dir.parent:
            if (current_dir / ".git").exists() and current_dir.name == proj_name:
                proj_root = str(current_dir)
                break
            current_dir = current_dir.parent
    
    if not proj_root:
        raise ValueError(f"Could not determine {proj_name} root. "
                         f"Please set {proj_env_var} environment variable.")
    
    return proj_root

def install_package(package_name: str, upgrade: bool = False) -> bool:
    """Install Python package with pip.
    
    Based on functionality from ProjectScylla scripts/install_tools.py
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package_name)
    
    try:
        result = run_subprocess(cmd)
        print(f"Successfully installed {package_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to install {package_name}: {e}")
        return False

def parse_log_file(log_path: str, pattern: str = None) -> List[Dict[str, Any]]:
    """Parse training log file to extract metrics.
    
    Based on functionality from ProjectOdyssey scripts/plot_training.py
    """
    log_path = Path(log_path)
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")
    
    metrics = []
    
    # This would contain actual log parsing logic
    # Simplified for demonstration
    with open(log_path, 'r') as f:
        for line_num, line in enumerate(f, 1):
            if pattern and pattern in line:
                # Parse line for metrics
                # In real implementation, this would extract actual metrics
                metrics.append({
                    "line_number": line_num,
                    "content": line.strip(),
                    "timestamp": "extracted_timestamp_if_available"
                })
    
    return metrics

def main():
    """Main entry point for utility functions."""
    parser = argparse.ArgumentParser(description="Consolidated utilities")
    parser.add_argument("--install", help="Install specified package")
    parser.add_argument("--parse-log", help="Parse log file for metrics")
    parser.add_argument("--project-root", help="Get project root path")
    
    args = parser.parse_args()
    
    if args.install:
        install_package(args.install)
    elif args.parse_log:
        metrics = parse_log_file(args.parse_log)
        print(json.dumps(metrics, indent=2))
    elif args.project_root:
        try:
            root = get_proj_root(args.project_root)
            print(root)
        except ValueError as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
