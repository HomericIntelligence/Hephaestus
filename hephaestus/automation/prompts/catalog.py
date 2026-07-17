"""Compatibility import for automation prompt builders.

The catalog itself lives in :mod:`hephaestus.prompts` so library packages can
render their prompts without depending on the automation layer.
"""

from hephaestus.prompts.catalog import PromptCatalog, add_prompt_dir_argument

__all__ = ["PromptCatalog", "add_prompt_dir_argument"]
