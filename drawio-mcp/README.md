# draw.io MCP connector (FastMCP, Streamable HTTP)

Lets AI agents create draw.io (diagrams.net) diagrams. HTTP only (no npx, no stdio). It needs **no token and no
account**: it builds and validates diagram XML locally, stores nothing, and returns an editor link plus the `.drawio`
file contents. Safe for any user and any header.

## Files

| File | Purpose |
|---|---|
| `server.py` | The connector (3 tools) |
| `requirements.txt`, `Dockerfile` | Dependencies and container build |
| `tests/test_drawio.py` | Automated test (valid XML, layouts, rejects malicious XML, link round-trip) |
| `try_drawio.py` | One command: starts the connector, makes a demo diagram, opens it in your browser |

Tools: `create_diagram` (nodes and edges, automatic layout), `create_diagram_from_xml` (validate and open your own
draw.io XML), `list_shapes`.

---

## Step 1. Python 3.12

Use 64-bit Python **3.12 or 3.13** (3.15 does not work yet). Check with `py -0`.

## Step 2. Open PowerShell in this folder

Open this folder in File Explorer, click the address bar, type `powershell`, press Enter.
(A PowerShell window opened from the Start menu starts in `C:\WINDOWS\System32`, which is the wrong place.)

## Step 3. Create the environment and install

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python --version
    python -m pip install --upgrade pip
    $env:PYTHONUTF8 = 1
    pip install -r requirements.txt

The prompt should start with `(.venv)` and `python --version` should say 3.12.x.
If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass`, then activate again.
Next time you only need to open PowerShell in the folder and run the `Activate.ps1` line.

## Step 4. Automated test

    python tests\test_drawio.py

Last line should be `drawio: PASS`. Lines saying "Error calling tool" are expected (the test triggers errors on purpose).

## Step 5. See it work (the real check)

    python try_drawio.py

It prints the tool names, writes `demo.drawio` and `demo-link.html` into this folder, and opens your browser at
draw.io. You should see four shapes (User, Web App, API, Database) joined by arrows labelled uses / HTTPS / SQL.

If the browser does not show the diagram:
1. Double-click `demo-link.html` and click the link on that page.
2. Or go to https://app.diagrams.net, choose **File > Open from > Device**, and pick `demo.drawio`. This route does
   not use links, so if it works the connector's output is fine and only the link is the problem.
3. If https://app.diagrams.net itself does not load, your company network blocks it. Ask IT, or set
   `DRAWIO_BASE_URL` to a self-hosted draw.io.

## Step 6. Run it as a server (what the workflow designer connects to)

    python server.py

Serves `http://0.0.0.0:8000/mcp` (health check: `curl.exe http://localhost:8000/health`). Change the port with
`$env:PORT = 8001` before starting. No headers are needed. To test a running server:

    python try_drawio.py --url http://localhost:8000/mcp

## Step 7. Docker (optional)

    docker build -t mcp-drawio .
    docker run -p 8000:8000 mcp-drawio

## Settings (environment variables)

- `DRAWIO_BASE_URL`: editor used in links (default `https://app.diagrams.net`).
- `HOST`, `PORT`: bind address (default `0.0.0.0:8000`).

## Before raising the PR

1. `python tests\test_drawio.py` passes.
2. `python try_drawio.py` shows the diagram in the draw.io editor. **This is the one check not done in the build
   sandbox, which cannot open a browser.**
3. Do not commit `.venv`. `.gitignore` already excludes it, plus the generated demo files.
