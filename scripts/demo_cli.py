#!/usr/bin/env python3
"""
Demonstration of the enhanced CLI utilities in ProjectHephaestus.
"""

import sys
from hephaestus import register_command, create_parser, format_table
from hephaestus.cli.utils import COMMAND_REGISTRY

# Register a sample command
@register_command("demo", "Demonstrate CLI features")
def demo_command(args):
    """Demo command showing CLI features."""
    print("CLI Enhancement Demo")
    print("===================")
    
    # Show command registration
    print("\nRegistered Commands:")
    commands = COMMAND_REGISTRY.commands
    if commands:
        rows = [(name, info['description']) for name, info in commands.items()]
        print(format_table(rows, headers=["Command", "Description"]))
    else:
        print("No commands registered yet.")
    
    return 0

if __name__ == "__main__":
    parser = create_parser("clitest")
    parser.add_argument(
        'command',
        nargs='?',
        help='Command to run'
    )
    
    args = parser.parse_args()
    
    if args.command:
        cmd_info = COMMAND_REGISTRY.get_command(args.command)
        if cmd_info:
            cmd_info['function'](args)
        else:
            print(f"Unknown command: {args.command}")
            sys.exit(1)
    else:
        parser.print_help()
