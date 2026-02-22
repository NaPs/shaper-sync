# shaper-sync

Synchronize a local directory to [Shaper Hub](https://hub.shapertools.com).

Built for [Shaper Origin](https://www.shapertools.com/en-us/origin) owners who
want to automatically push SVG files to the Hub without going through the web
UI. Shaper doesn't provide a public API, so this tool relies on a
reverse-engineered API.

Uploads new and modified files from a local directory to your Shaper Hub
personal space. Supports recursive synchronization, dry-run mode, and
continuous watch mode using inotify.

## Installation

```bash
pip install .
```

## Usage

Credentials can be passed as arguments or via environment variables
`SHAPER_EMAIL` and `SHAPER_PASSWORD`.

```bash
# One-shot sync
shaper-sync ./my-designs --email user@example.com --password secret

# Using environment variables
export SHAPER_EMAIL=user@example.com
export SHAPER_PASSWORD=secret
shaper-sync ./my-designs

# Sync to a specific remote folder
shaper-sync ./my-designs --remote-path /Projects

# Watch mode (initial sync + continuous monitoring)
shaper-sync ./my-designs --watch

# Dry-run (no changes made)
shaper-sync ./my-designs --dry-run

# Verbose output
shaper-sync ./my-designs --verbose
```

## Docker

Build the image:

```bash
docker build -t shaper-sync .
```

Run a one-shot sync:

```bash
docker run --rm \
  -e SHAPER_EMAIL=user@example.com \
  -e SHAPER_PASSWORD=secret \
  -v /path/to/designs:/data \
  shaper-sync /data
```

Run in watch mode:

```bash
docker run --rm \
  -e SHAPER_EMAIL=user@example.com \
  -e SHAPER_PASSWORD=secret \
  -v /path/to/designs:/data \
  shaper-sync /data --watch
```
