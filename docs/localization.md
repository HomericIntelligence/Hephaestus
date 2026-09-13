# Localization boundary

Hephaestus localizes its human-facing command-line interface (CLI) and plain-log
text through `hephaestus.cli.localization`. English source templates are catalog
keys. A missing catalog entry returns the complete English source text. The
implementation uses only the Python standard library.

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

Translations must keep each named placeholder and its conversion type.
Positional placeholders must keep their order and conversion types. `Localizer`
validates this contract when it is constructed. It copies the supplied mapping
and does not expose a mutable catalog. Use `%%` for a literal percent sign in a
formatted template.

CLI metadata is translated when a parser is constructed. This rule applies only
to Hephaestus descriptions, epilogs, usage text, help text, group titles, and
subparser help. Flags, destinations, metavars, choices, defaults, actions,
types, version payloads, and exit codes do not change. Standard-library
`argparse` headings and diagnostics remain in the standard-library fallback
language.

Hephaestus plain-text formatters capture the active immutable localizer when they
are constructed. A background thread that uses the formatter uses this same
catalog. This mechanism does not replace the process-global log-record factory.
JSON formatters and serialized system-information output do not use localization.
Machine-readable output, log field names, protocol tokens, runtime values, and
environment keys must not be translated.

The boundary does not mutate `argparse._`, `argparse.ngettext`, locale globals,
environment variables, or other process-global state. Nested `using_localizer()`
contexts restore the previous selection when the context exits, including when
an exception occurs.
