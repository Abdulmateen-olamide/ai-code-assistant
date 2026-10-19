# Stellar notifier plugin (example)

A read-only example plugin that reacts to `stellar.analysis.completed` and
formats a short, bounded summary. It is used by
[`docs/plugins.md`](../../../docs/plugins.md) and
[`tests/test_stellar_notifier_example.py`](../../../tests/test_stellar_notifier_example.py).

- `manifest.json` — manifest (id `stellar-notifier`, entry point, declared
  `STELLAR_READ` capability).
- `plugin.py` — `StellarNotifierPlugin`, which subscribes to
  `stellar.analysis.completed` and records at most
  `MAX_RECENT_SUMMARIES` summaries.

## Behaviour

For each event the plugin reads only `project_id`, `detected`, `confidence`,
and an optional `network`:

```
Project 7 — Stellar detected, confidence 0.98
```

Every field is truncated (`MAX_FIELD_CHARS`) and the final summary is capped at
`MAX_SUMMARY_CHARS`. The event payload is copied, never mutated. The plugin
performs **no network requests and no database writes**, so it runs entirely
offline under the mock network.

Nothing is granted automatically: the plugin must be installed, enabled in a
workspace, and explicitly granted `STELLAR_READ` before its handler runs.

## Try it

```bash
# Register the plugin (operator scope; no code is executed, no grants created)
flask plugins install examples/plugins/stellar_notifier
flask plugins inspect stellar-notifier
flask plugins enable stellar-notifier
```

Make the entry point importable during local development:

```bash
# POSIX
export PYTHONPATH="$PWD/examples/plugins:$PYTHONPATH"
# PowerShell
$env:PYTHONPATH = "$PWD\examples\plugins;$env:PYTHONPATH"
```

Then install it into a workspace and grant `STELLAR_READ` through the
management API or UI, or run the offline demo:

```bash
python examples/plugins/stellar_notifier/plugin.py
```
