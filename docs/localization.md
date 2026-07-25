# Localization boundary

Hephaestus localizes its human-facing CLI, plain-log, and curses TUI text
through `hephaestus.cli.localization`. English source templates are catalog
keys, and a missing catalog entry returns the complete English source text.
The implementation uses only the Python standard library.

```python
from hephaestus.cli import Localizer, text, using_localizer

catalog = Localizer(
    {
        "Processed %(count)d files": "%(count)d fichiers traités",
        "Show details": "Afficher les détails",
    }
)

with using_localizer(catalog):
    print(text("Processed %(count)d files", count=2))
```

Translations must preserve every named placeholder and its conversion type.
Positional placeholders must preserve their order and conversion types.
`Localizer` validates this contract at construction, copies the supplied
mapping, and exposes no mutable catalog. Use `%%` for a literal percent sign in
a formatted template.

CLI metadata is translated when a parser is constructed. This applies only to
Hephaestus-authored descriptions, epilogs, usage text, help text, group titles,
and subparser help. Flags, destinations, metavars, choices, defaults, actions,
types, version payloads, and exit codes remain unchanged. Stdlib-owned argparse
headings and diagnostics remain in the stdlib fallback language.

Plain-text log formatters and `CursesUI` capture the active immutable localizer
when they are constructed so later background-thread rendering is deterministic.
JSON formatters and serialized system-information output bypass localization.
Machine-readable output, log field names, protocol tokens, runtime values, and
environment keys must never be translated.

The boundary never mutates `argparse._`, `argparse.ngettext`, locale globals,
environment variables, or other process-global state. Nested
`using_localizer()` contexts restore the previous selection even when an
exception exits the context.
