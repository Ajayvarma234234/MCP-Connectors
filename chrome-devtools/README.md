# Chrome DevTools MCP connector (FastMCP, Streamable HTTP)

Lets AI agents inspect and debug web pages in a Chrome/Chromium browser through the Chrome DevTools Protocol:
open/close tabs, navigate while recording console messages, JavaScript errors and network failures, screenshots,
DOM / accessibility snapshots, run JavaScript, click and type, and read performance metrics.
HTTP only (no npx, no stdio).

## How it works (read this once)

The server holds **no browser and no state**. Each caller starts **their own** Chrome with a debugging port and sends
its address in a header on every request:

    X-Chrome-CDP-Endpoint: http://<host>:9222

For each tool call the server connects to that browser, does the work, and disconnects (the caller's browser is never
closed). So one deployed connector serves any number of users, each with their own browser.
Because nothing is stored between calls, console and network activity are recorded *during* `navigate` and
`capture_activity`, not retrieved afterwards. Tab identity between calls is the `target_id` from `list_pages`.

Tools: `list_pages`, `new_page`, `close_page`, `navigate`, `capture_activity`, `screenshot`, `evaluate_script`,
`get_page_content`, `click`, `fill`, `performance_metrics`.

## Files

| File | Purpose |
|---|---|
| `server.py`, `auth.py` | The connector and its header helper |
| `requirements.txt`, `Dockerfile` | Dependencies and container build (no browser inside: callers bring their own) |
| `try_chrome.py` | One command: starts a throwaway browser and the connector, runs a full tour |
| `tests/test_chrome_guards.py` | Automated safety tests; no browser needed |
| `tests/test_chrome_real.py` | Automated test of every tool against a real browser |

---

## Step 1. Python 3.12

Use 64-bit Python **3.12 or 3.13** (3.15 does not work yet). Check with `py -0`.

## Step 2. Open PowerShell in this folder

Open the folder in File Explorer, click the address bar, type `powershell`, press Enter.
(A PowerShell opened from the Start menu starts in `C:\WINDOWS\System32`, the wrong place.)

## Step 3. Create the environment and install

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python --version
    python -m pip install --upgrade pip
    $env:PYTHONUTF8 = 1
    pip install -r requirements.txt

The prompt should start with `(.venv)`; `python --version` should say 3.12.x.
If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass`, then activate again.
No browser download is needed: the tour uses Chrome or Edge already installed on your laptop.

## Step 4. Automated safety test (no browser)

    python tests\test_chrome_guards.py

Last line should be `chrome-devtools (guards): PASS`. It checks: missing header rejected, bad URLs rejected,
cloud-metadata addresses refused, secrets in the endpoint never appear in errors, only `http(s)` pages can be opened,
and two users' browsers are never mixed up.

## Step 5. The real check

    python try_chrome.py

It finds Chrome (or Edge), starts it hidden with a private debugging port and a **throwaway profile** (your own
browser, tabs and logins are not touched), starts the connector, and runs a tour. You should see 7 numbered steps and
the last line:

    OK: the Chrome DevTools connector works with a real browser.

It also saves `demo-screenshot.png` (a page showing "Button was clicked" and the name "Aman" in the box).

Useful options:

    python try_chrome.py --visible                        show the browser window while it runs
    python try_chrome.py --site https://example.com       also visit a real site and report on it
    python try_chrome.py --browser "C:\Path\to\msedge.exe"   use a specific browser (e.g. Edge)

**If it says the debugging port never answered,** your company probably blocks remote debugging in the browser (a
policy named `RemoteDebuggingAllowed`) or security software blocks the local port. Try Edge with `--browser`, or ask
IT whether remote debugging can be allowed on a test machine.

## Step 6. Automated real-browser test (optional)

Start any Chrome with a debugging port yourself, then:

    & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\cdp-profile" about:blank
    $env:CHROME_CDP_ENDPOINT = "http://127.0.0.1:9222"
    python tests\test_chrome_real.py

Last line: `chrome-devtools (real browser): PASS`. (Recent Chrome versions ignore `--remote-debugging-port` unless
`--user-data-dir` points to a non-default folder, which is why that flag is there.)

## Step 7. Run it as a server

    python server.py

Serves `http://0.0.0.0:8000/mcp` (health check: `curl.exe http://localhost:8000/health`). Change the port with
`$env:PORT = 8001`. Clients send `X-Chrome-CDP-Endpoint: http://<their-browser-host>:9222`. The connector must be
able to reach that address over the network. To test a running server:
`python try_chrome.py --url http://localhost:8000/mcp --cdp http://127.0.0.1:9222`.

## Step 8. Docker (optional)

    docker build -t mcp-chrome-devtools .
    docker run -p 8000:8000 mcp-chrome-devtools

Inside a container, `127.0.0.1` is the container itself, so callers must give an address the container can reach.

## Security: read before deploying

The caller chooses which address the server connects to, which on a shared deployment could be abused to probe
internal networks. Set these environment variables on the deployment:

- `CDP_ALLOWED_HOSTS=host1,*.corp.example`: only these hosts may be used (unset = any host).
- `CDP_BLOCK_PRIVATE=true`: also refuse loopback and private-network addresses (recommended for shared hosting;
  it also means local-only testing as in Step 5 requires leaving it unset).
- Link-local and cloud-metadata addresses (169.254.0.0/16, fe80::/10) are always refused.
- `navigate` and `new_page` only open `http(s)://` pages (never `file://` or `javascript:`).

Anyone who can reach a browser's debugging port controls that browser, so callers should expose it only to the
connector (for example on a private network), never to the internet.

## Before raising the PR

1. `python tests\test_chrome_guards.py` passes.
2. `python try_chrome.py` ends with `OK:` on your laptop. **The build sandbox ran this on Linux Chromium; Windows
   Chrome and Edge have not been tested by me, so this is your check.**
3. Do not commit `.venv`. `.gitignore` already excludes it, plus the demo screenshot.
