# Migration Guide

> **Status (as of 2026-06-16):** The latest released version is **0.9.6** (tag-driven
> via hatch-vcs). **1.0 has not been released yet** — the section below is the
> *forthcoming* 1.0 migration guidance, published ahead of the cut so consumers can
> prepare. There are currently **no breaking changes between 0.9.x releases**: upgrading
> within the 0.9.x line requires no code changes for code that uses only the documented
> public API in [`COMPATIBILITY.md`](../COMPATIBILITY.md). The `Development Status ::
> 5 - Production/Stable` classifier in `pyproject.toml` reflects the maturity of the
> stable public API surface, not a 1.0 tag; the package remains tag-driven and is
> currently on the 0.9.x series.

## 0.x → 1.0 (forthcoming — not yet released)

### Summary

**Version 1.0 will introduce no breaking changes to the documented public API.**

The 1.0 release will not change the public API — it will *commit* to it. Every symbol
listed in [`COMPATIBILITY.md`](../COMPATIBILITY.md) keeps the same name, signature,
and behavior it had in the 0.x series. Code that uses only the documented public API
will need **no changes** to move from a recent 0.x release to 1.0.

What 1.0 will mean for consumers:

- The public API surface in `COMPATIBILITY.md` is now covered by the
  [Semantic Versioning](https://semver.org/) guarantees and the deprecation policy —
  it will not change incompatibly without a 2.0 release.
- Non-public API (anything not in `COMPATIBILITY.md`: underscore-prefixed names,
  internal modules, non-`__all__` symbols) remains unguaranteed, as before.

### Upgrade checklist

1. **Widen your version pin.** If you pinned `homericintelligence-hephaestus` with an
   upper bound of `<1`, change it to `<2` so you receive 1.x releases:

   ```toml
   # pyproject.toml or pixi.toml
   "homericintelligence-hephaestus>=1.0,<2"
   ```

2. **No code changes are required** for code that uses the documented public API.

3. Re-run your test suite as you would for any dependency upgrade.

### Behavioral changes to be aware of

These are bug fixes, not API changes — signatures are unchanged — but the runtime
behavior is now correct where it previously was not. If you depended on the buggy
behavior (you should not have), review these:

| Area | 0.x behavior | 1.0 behavior |
|------|--------------|--------------|
| `hephaestus.__version__` | Resolved to `"unknown"` for installed users (wrong distribution-name lookup) | Resolves to the real installed version |
| `hephaestus.io.safe_write` | Not atomic — an interrupted write could leave a partial file | Atomic: writes via a temp file + `os.replace` |
| `hephaestus.io.write_secure` | Restrictive permissions but non-atomic write | Atomic **and** `0o600`-permissioned |
| `hephaestus.github.wait_until` | Raised `ValueError` when called from a worker thread | Safe to call from any thread |

### Deprecated symbols

- `retry_with_jitter` — deprecated in favor of `retry_with_backoff(jitter=True,
  max_delay=...)`. It still works and emits a `DeprecationWarning`; it is retained for
  backwards compatibility and is not removed in 1.0. See the
  [deprecation policy](../COMPATIBILITY.md#deprecation-policy).

### Questions

If an upgrade surfaces an unexpected change in documented public-API behavior, please
[open an issue](https://github.com/HomericIntelligence/ProjectHephaestus/issues).
