# BrowserStack MCP connector (FastMCP, Streamable HTTP)

Lets AI agents look into BrowserStack test runs: check plan and parallel-session usage, find supported browsers and
real devices, list builds and sessions, read a session's details and logs (command, network, console, device,
Appium), and mark a session passed or failed. Covers **Automate** (web) and **App Automate** (mobile app).
HTTP only (no npx, no stdio).

## How it works (read this once)

The server holds **no credentials and no state**. Each caller sends their own BrowserStack username and access key
in headers on every request:

    X-BrowserStack-Username:   <username>
    X-BrowserStack-Access-Key: <access key>

For each tool call the server calls the BrowserStack REST API as that caller (HTTP Basic auth, a fresh client, nothing
cached). So one deployed connector serves any number of users and accounts. The BrowserStack hosts are fixed in the
code (`api.browserstack.com`, `api-cloud.browserstack.com`); callers cannot point the server anywhere else.

Tools (web): `automate_plan`, `automate_browsers`, `automate_list_builds`, `automate_list_sessions`,
`automate_get_session`, `automate_get_session_log`, `automate_set_session_status`.
Tools (mobile): `app_automate_plan`, `app_automate_devices`, `app_automate_list_builds`,
`app_automate_list_sessions`, `app_automate_get_session`, `app_automate_get_session_log`,
`app_automate_set_session_status`.

Typical flow: `*_list_builds` -> `*_list_sessions` (needs a build id) -> `*_get_session` / `*_get_session_log`
(needs a session id). Every tool is read-only except `*_set_session_status`. There are deliberately **no delete tools**.

## Files

| File | Purpose |
|---|---|
| `server.py`, `auth.py` | The connector and its header helper (same `auth.py` as the Chrome connector) |
| `requirements.txt`, `Dockerfile` | Dependencies and container build |
| `try_browserstack.py` | One command: starts the connector and runs a read-only tour against your account |
| `sample_session.py` | Optional: runs one tiny cloud test so a brand-new trial account has something to inspect |
| `tests/test_browserstack_guards.py` | Automated tests against a fake BrowserStack; no account needed |

---

## Step 1. Get a BrowserStack account and your credentials

1. Sign up for the free trial at <https://www.browserstack.com> (Automate is included in the trial; App Automate may
   need to be enabled separately, and the tour simply skips it if it is not).
2. In the dashboard open **Account -> Summary** (or Settings) and copy your **Username** and **Access Key**.
   Treat the access key like a password. Never paste it into a file you will commit.

## Step 2. Python 3.12

Use 64-bit Python **3.12 or 3.13**. Check with `py -0`.

## Step 3. Open PowerShell in this folder

Open the folder in File Explorer (the one that contains `server.py`), click the address bar, type `powershell`,
press Enter.

## Step 4. Create the environment and install

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python --version
    python -m pip install --upgrade pip
    $env:PYTHONUTF8 = 1
    pip install -r requirements.txt

The prompt should start with `(.venv)`. If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass`, then
activate again. No browser is needed for this connector.

## Step 5. Automated tests (no account needed)

    python tests\test_browserstack_guards.py

Last line should be `browserstack (guards): PASS`. It runs the connector against a fake BrowserStack and checks:
missing credentials rejected, ids that could alter a URL (`../`, `/`, `?`) rejected before any request is made, the
right Basic-auth is sent, two users' calls interleaved under load never get mixed up, a rejected key gives a clear
error without echoing the key, an unreachable BrowserStack gives a clear error without echoing the key, paging
limits are capped, only passed/failed can be set, and big result lists are filtered and capped.

## Step 6. The real check (uses your account, read-only)

Set your credentials for this PowerShell window only (they are not saved anywhere):

    $env:BROWSERSTACK_USERNAME = "your_username"
    $env:BROWSERSTACK_ACCESS_KEY = "your_access_key"
    python try_browserstack.py

(If you skip the two `$env:` lines the script asks for them, hiding the key as you type.)

You should see numbered steps and the last line:

    OK: the BrowserStack connector works with your account.

What each step proves: [1] your credentials work, [2] the browser list can be read and filtered, [3] builds,
[4] sessions, a session's details and log, [5] App Automate (shown as "skipped" if your trial does not include it).

**A brand-new trial has no builds yet** ("none yet" in step 3 is normal). To create one:

    pip install selenium
    python sample_session.py
    python try_browserstack.py

`sample_session.py` runs one tiny test in BrowserStack's cloud (about a minute of trial time), then the tour will show
that build, its session, and the start of its log.

To also try the one write operation (marks a session passed/failed on your dashboard; use a session id printed in step 4):

    python try_browserstack.py --mark <session_id> passed

## Step 7. Run it as a server

    python server.py

Serves `http://0.0.0.0:8000/mcp` (health check: `curl.exe http://localhost:8000/health`). Change the port with
`$env:PORT = 8001`. Clients must send the two `X-BrowserStack-*` headers on every request. To test a running server:
`python try_browserstack.py --url http://localhost:8000/mcp`.

## Step 8. Docker (optional)

    docker build -t mcp-browserstack .
    docker run -p 8000:8000 mcp-browserstack

## Security: read before deploying

- Credentials travel only in request headers, are used for that one request, and are never stored, logged, or put in
  error messages. Always serve this connector over HTTPS when it is not on your own machine.
- The BrowserStack hosts are fixed, so a caller cannot make the server contact other addresses.
- Build/session ids are checked (letters, digits, `-`, `_` only) before they are placed in a URL.
- Log and result sizes are capped so one call cannot flood an agent's context.
- `*_set_session_status` changes data in the caller's account. If you want a strictly read-only deployment, remove
  those two functions from `server.py` before deploying.
- Operator-only settings `BROWSERSTACK_AUTOMATE_BASE` / `BROWSERSTACK_APP_BASE` exist for the automated tests;
  do not set them in production.

## Before pushing / raising the PR

1. `python tests\test_browserstack_guards.py` passes.
2. `python try_browserstack.py` ends with `OK:` using your trial account. **The build sandbox could only test against
   a simulated BrowserStack; the real API calls (especially the App Automate ones and the log endpoints) are first
   exercised by your run, so this is your check.**
3. Do not commit `.venv` or any file containing your access key. `.gitignore` already excludes `.venv` and `.env`.
