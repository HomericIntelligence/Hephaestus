#!/usr/bin/env python3
"""
Command-line interface utilities for ProjectHephaestus.

Standardized argument parsing, command registration, and CLI helpers.

Usage:
    from hephaestus.cli.utils import create_parser, add_logging_args
    
    parser = create_parser(description="My utility")
    add_logging_args(parser)
    args = parser.parse_args()
"""

import argparse
import sys
from typing import Optional, Sequence

def create_parser(description: str = "", 
                  prog: Optional[str] = None) -> argparse.ArgumentParser:
    """Create standardized argument parser.
    
    Args:
        description: Program description
        prog: Program name
        
    Returns:
        Configured ArgumentParser instance
    """
    parser = argparse.ArgumentParser(
        description=description,
        prog=prog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Add version argument by default
    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s 0.1.0'
    )
    
    return parser

def add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Add standard logging arguments to parser.
    
    Args:
        parser: ArgumentParser instance
    """
    logging_group = parser.add_argument_group('logging options')
    logging_group.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    logging_group.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Suppress informational messages'
    )
    logging_group.add_argument(
        '--log-file',
        help='Log to file instead of stdout'
    )

def confirm_action(prompt: str = "Are you sure?", 
                   default: bool = False) -> bool:
    """Prompt user for confirmation.
    
    Args:
        prompt: Confirmation prompt
        default: Default response if user just presses Enter
        
    Returns:
        User's confirmation decision
    """
    choices = "Y/n" if default else "y/N"
    choice = input(f"{prompt} [{choices}] ").strip().lower()
    
    if not choice:
        return default
    elif choice in ['y', 'yes']:
        return True
    elif choice in ['n', 'no']:
        return False
    else:
        print("Invalid choice. Please enter 'y' or 'n'.")
        return confirm_action(prompt, default)

def format_table(rows: Sequence[Sequence[str]], 
                headers: Optional[Sequence[str]] = None,
                separator: str = "  ") -> str:
    """Format data as a pretty table.
    
    Args:
        rows: Table data rows
        headers: Optional header row
        separator: Column separator
        
    Returns:
        Formatted table string
    """
    # Combine headers and rows
    all_rows = [headers] if headers else []
    all_rows.extend(rows)
    
    if not all_rows:
        return ""
    
    # Calculate column widths
    col_widths = [
        max(len(str(row[i])) for row in all_rows if i < len(row))
        for i in range(max(len(row) for row in all_rows))
    ]
    
    # Format rows
    result = []
    for row_idx, row in enumerate(all_rows):
        formatted_row = separator.join(
            str(cell).ljust(col_widths[i]) 
            for i, cell in enumerate(row)
        )
        result.append(formatted_row)
        
        # Add separator line after headers
        if headers and row_idx == 0:
            separator_line = separator.join(
                "-" * width for width in col_widths
            )
            result.append(separator_line)
    
    return "\n".join(result)
