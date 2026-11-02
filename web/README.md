# Browser demo

An interactive demo page for opcua-sim: the example bench from `examples/plant.yaml` plus a
PLC with the S7-1500 layout and a bar code reader with the BCL 300 layout. The site is static,
with no backend and no requests to other hosts.

**The wire protocol does not run here.** A browser has no TCP sockets and asyncua cannot run in
[Pyodide](https://pyodide.org) without them. The real device code runs (`device.py`, `models.py`,
`profile.py`, `faults.py`), and `py/demo.py` answers the OPC UA services directly by the same
rules: status codes, null for Bad values, Acknowledge by EventId, a session that a restart ends.
The wheel is loaded without its dependencies, and a test makes sure the device code imports
without asyncua.

This directory is not part of the PyPI package.

| Path | What it is |
|---|---|
| `py/demo.py` | the Python layer: terminal commands, sessions, server faults, device state as JSON |
| `site/` | the page (HTML, CSS, JavaScript; no framework, no bundler) |
| `build_site.py` | builds `web/dist`: page, `demo.py`, bench, a wheel of this checkout, Pyodide (pinned by SHA-256) |
| `nginx.conf`, `Dockerfile` | the container that serves `web/dist` |
| `tests/` | pytest for `demo.py` and the build, Playwright for the page |

## Build and run

```bash
pip install -e ".[dev]"
python web/build_site.py                 # -> web/dist
python -m http.server -d web/dist 8000   # quick look, without the nginx headers

docker build -f web/Dockerfile -t opcua-sim-web .   # from the repository root
docker run --rm -p 8080:80 opcua-sim-web            # http://localhost:8080
```

## Test

```bash
pytest web/tests                       # Python layer and build
cd web && npm ci && npx playwright install chromium firefox
npx playwright test                    # against http://localhost:8080 (DEMO_URL to change it)
node tests/screenshot.mjs              # refresh site/img/bench.png from the running demo
```
