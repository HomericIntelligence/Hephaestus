"""Shared Jinja prompt-template infrastructure for Hephaestus packages."""

from .catalog import PromptCatalog, add_prompt_dir_argument

__all__ = ["PromptCatalog", "add_prompt_dir_argument"]
