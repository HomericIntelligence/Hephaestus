#!/usr/bin/env python3
"""
Deployment utilities for HomericIntelligence projects.

Consolidated from various project utility scripts.
"""

import subprocess
import argparse
import sys
import os
from pathlib import Path

def run_command(cmd, check=True):
    """Run shell command with proper error handling."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}")
        print(f"Error: {e.stderr}")
        if check:
            raise
        return e

def deploy_project(project_name, environment="staging"):
    """Deploy project to specified environment."""
    print(f"Deploying {project_name} to {environment}...")
    
    # This would contain actual deployment logic
    # Simplified for demonstration
    commands = [
        f"echo Deploying {project_name}",
        f"echo Environment: {environment}",
        "echo Deployment completed successfully"
    ]
    
    for cmd in commands:
        result = run_command(cmd)
        if result.stdout:
            print(result.stdout.strip())

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Deployment utilities")
    parser.add_argument("project", help="Project name to deploy")
    parser.add_argument("--env", default="staging", 
                        choices=["staging", "production"],
                        help="Target environment")
    
    args = parser.parse_args()
    deploy_project(args.project, args.env)

if __name__ == "__main__":
    main()
