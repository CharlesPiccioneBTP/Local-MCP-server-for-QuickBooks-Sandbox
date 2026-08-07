# QuickBooks MCP server — local, read-only

Ask Claude Desktop questions about a QuickBooks Online company in plain English
and get answers from the live books.

> **You:** Who owes us money and how overdue is it?
>
> **Claude:** Sandbox Company US 4768 has **$5,281.52** outstanding across 20 open
> invoices. Most of it isn't late yet — $3,756.02 is still current. Of the $1,525.50
> that *is* overdue, the worst is Red Rock Diner at **$156.00, 65 days past due**
> (invoice 1024, due 3 June). Kookies by Kathy ($75.00) and Bill's Windsurf Shop
> ($85.00) are both around 45 days out.

Everything runs on your own machine. Nothing is hosted or deployed, and no
company data leaves your computer except to Claude, in the answer.

---

## What you can ask

| Question | Tool used |
|---|---|
| Who owes us money? How overdue? Who should we chase? | `get_receivables_aging` |
| What bills do we owe? What's due soon? | `get_payables_aging` |
| How profitable were we this year? What did we spend on X? | `get_profit_and_loss` |
| What do we own and owe? How much cash? | `get_balance_sheet` |
| What's Amy's email? Who are our customers? | `find_contacts` |
| Which company am I connected to? | `get_company_info` |
| Anything else in the books | `run_query` + `describe_schema` |

The aging tools compute days overdue and bucket everything (current, 1–30,
31–60, 61–90, 90+) rather than making Claude do date arithmetic on raw invoices.

---

## What "read-only" means here

**The server cannot write to QuickBooks.** Not "writes are switched off" — there
is no code that could write:

- `client.py` is the only module that can reach the QuickBooks API. It exposes a
  single request method, the HTTP verb is a hardcoded literal `"GET"`, and every
  path is checked against an allowlist of exactly two prefixes (`/query` and
  `/reports/`). QuickBooks writes are `POST`s to `/v3/company/{realm}/{entity}`,
  which is neither.
- `tests/test_readonly.py` parses the source and **fails the build** if a non-GET
  verb appears in that module, if any other module grows its own HTTP client, or
  if the path allowlist changes.

**The honest caveat:** Intuit publishes no read-only scope for accounting data.
The OAuth token this server holds *is* capable of writing — the restriction lives
in this code, not in the token. If you don't trust the code, don't trust the
claim; read `client.py` (about 200 lines) and the test that guards it.

One `POST` does exist, in `auth.py`: the OAuth token exchange. It is pinned to
Intuit's token endpoint, which is a different host from the accounting API, and
the test asserts that too.

**Want to see for yourself?** Every request is logged. Open the MCP log
(`%APPDATA%\Claude\logs\mcp-server-quickbooks.log`) and you'll see only `GET`
lines.

---

## Install

Takes about 10 minutes, most of it on Intuit's website.

### 1. Install `uv`

`uv` runs the server and handles Python and dependencies for you.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal afterwards. Check it worked:

```bash
uv --version
```

*(macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`)*

### 2. Get this code

```bash
git clone https://github.com/CharlesPiccioneBTP/Local-MCP-server-for-QuickBooks-Sandbox.git
```

Remember where you put it — you'll need the full path in step 5.

### 3. Create an Intuit app

Free, no card required.

1. Sign up at **[developer.intuit.com](https://developer.intuit.com)**. A **sandbox
   company** with realistic sample data is created for you automatically.
2. **My Hub → Workspaces → +** → *Get Started* → fill in the basic details.
3. **+** to add an app → choose **QuickBooks Online (Accounting)** → name it
   anything (e.g. "Claude read-only").
4. Go to **Settings → Redirect URIs → Development** tab → **Add URI** and paste
   this **exactly**:

   ```
   http://localhost:8000/callback
   ```

   > This is the step people get wrong. It must be `http` (not `https`), with no
   > trailing slash. If it doesn't match character-for-character, Intuit refuses
   > the sign-in and setup fails.

5. Open **Keys & credentials** and keep the tab open. You need the **Client ID**
   and **Client Secret** from the **Development** section. (Sandbox and
   production have different keys — use Development.)

### 4. Connect your company

In a terminal, from the folder you cloned into:

```bash
uv run qbo-mcp-setup
```

It asks for the environment (press Enter for `sandbox`), then your Client ID and
Client Secret. Your browser opens; sign in and choose the sandbox company. The
secret isn't shown as you type and isn't saved to your shell history.

You should see:

```
Saved credentials to C:\Users\you\AppData\Local\qbo-mcp\credentials.json
Verifying the connection...
Success - connected to "Sandbox Company US 4768".
```

### 5. Tell Claude Desktop about it

Open Claude Desktop → **Settings → Developer → Edit Config**. That opens
`claude_desktop_config.json`. Add the `quickbooks` block inside `mcpServers`:

```json
{
  "mcpServers": {
    "quickbooks": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\you\\path\\to\\Local-MCP-server-for-QuickBooks-Sandbox",
        "run",
        "qbo-mcp"
      ]
    }
  }
}
```

Two things to get right:

- Replace the path with **your** folder from step 2. On Windows use **double**
  backslashes (`\\`) as shown.
- If Claude Desktop reports it can't find `uv`, replace `"uv"` with its full
  path. Find it by running `where uv` (Windows) or `which uv` (Mac/Linux) — it's
  usually `C:\Users\you\.local\bin\uv.exe`.

**No credentials go in this file** — that's deliberate. This file gets shared and
screenshotted when people ask for help. Your secrets live in the separate
credentials file from step 4.

### 6. Restart Claude Desktop

Fully quit and reopen it — closing the window isn't enough. Then ask:

> Who owes us money and how overdue is it?

---

## Checking it works

```bash
uv run qbo-mcp-doctor
```

This runs without Claude and checks the whole chain:

```
[ ok ] Credentials loaded from C:\Users\you\AppData\Local\qbo-mcp\credentials.json
       environment=sandbox  realm=93414576XXXXXXXX
[ ok ] Token refresh succeeded
[ ok ] Refresh token unchanged this time (rotation is periodic); file is current
[ ok ] Refresh token expires in 101 days
[ ok ] Connected to "Sandbox Company US 4768"
[ ok ] Receivables aging: 20 open invoice(s), total outstanding 5281.52
         current      3,756.02
         1-30         1,128.50
         31-60          241.00
         61-90          156.00
         90+              0.00
```

Run this first whenever something seems wrong — it separates "the server is
broken" from "Claude Desktop isn't talking to it".

---

## Troubleshooting

**"No credentials found"** — Setup hasn't been run, or it ran as a different
Windows user. Run `uv run qbo-mcp-setup`.

**The QuickBooks tools don't appear in Claude Desktop** — Almost always the
config file. Check the path uses double backslashes, then confirm the file is
valid JSON (a stray comma breaks it silently). Then fully quit and reopen the
app. If it still fails, check
`%APPDATA%\Claude\logs\mcp-server-quickbooks.log`.

**"Port 8000 is already in use"** during setup — Something else is using that
port. Stop it and retry. The port must match the redirect URI registered with
Intuit, so changing it means changing both.

**Setup opens the browser but fails after sign-in** — The redirect URI doesn't
match. It must be exactly `http://localhost:8000/callback` in **Settings →
Redirect URIs → Development**.

**"Intuit rejected the client credentials (401)"** — Wrong keys, or production
keys used for a sandbox company. Copy them again from the **Development**
section and re-run setup.

**It worked for weeks, now it says "invalid_grant"** — The connection needs
re-authorising. This happens if the token expires (see below), if it was revoked
in QuickBooks, or if two copies of the server fought over a refresh. Fix:
`uv run qbo-mcp-setup`.

---

## Keeping it working

Access tokens last 1 hour and the server refreshes them automatically. The
refresh token rotates roughly daily, and **Intuit invalidates the old one
immediately**, so the new value is written to disk atomically, under a
cross-process lock, before it's used. If two copies of the server start at once,
the second adopts the first's tokens instead of refreshing again — which would
otherwise disconnect both.

The refresh token itself is currently good for **~101 days from setup**, and
that window resets every time it's used. So the server keeps working
indefinitely as long as it's used occasionally. If it goes unused for that long,
re-run `uv run qbo-mcp-setup`. The doctor warns you when fewer than 30 days
remain.

---

## Where things live

| | |
|---|---|
| Credentials | `%LOCALAPPDATA%\qbo-mcp\credentials.json` (Windows)<br>`~/.config/qbo-mcp/credentials.json` (Linux), `~/Library/Application Support/qbo-mcp/` (Mac) |
| Claude Desktop config | `%APPDATA%\Claude\claude_desktop_config.json` |
| Server logs | `%APPDATA%\Claude\logs\mcp-server-quickbooks.log` |

Credentials are stored **outside this repository** so they cannot be committed by
accident. That's the actual protection — `.gitignore` is a backstop. Setup will
refuse outright to write them anywhere inside a git repository. The file is
locked to your user account (`icacls` on Windows, mode `600` elsewhere).

---

## Using it with a real company

This is built and tested against a **sandbox**. It will work against a live
QuickBooks company — answer `production` at the setup prompt and use your
Production keys — but bear in mind:

- Production apps need to go through Intuit's app review before other people can
  connect to them. For your own company's books, that's not required.
- Everything the tools return is sent to Claude to answer your question. Consider
  whether that's appropriate for your real financial data.
- The read-only guarantee holds identically. It's the same code path.

---

## For developers

```bash
uv run --group dev pytest       # 83 tests, no network or credentials needed
```

| Module | Role |
|---|---|
| `client.py` | The only route to the QuickBooks API. GET-only, path-allowlisted. |
| `auth.py` | OAuth setup and refresh. Holds the package's only POST. |
| `config.py` | Credential storage: atomic writes, cross-process lock, git refusal. |
| `tools.py` | QuickBooks operations, callable without an MCP session. |
| `server.py` | MCP tool definitions and descriptions. |
| `qbo_sql.py` | `run_query` validation and result caps. |
| `formatting.py` | Aging arithmetic, report flattening. |
| `schema.py` | Static entity reference for `describe_schema`. |

Notes on design decisions worth knowing before changing things:

- **Tools raise plain exceptions, never `MCPError`.** In this SDK a plain
  exception becomes a tool error whose message the model reads and can act on;
  `MCPError` becomes a protocol error the model never sees. Every failure here is
  one the user needs told about.
- **AR aging is computed from `Invoice` rows, not Intuit's aging report.** Invoice
  rows already embed `CustomerRef.name`, so no join is needed, and it keeps the
  bucket boundaries under our control. Their Reports API is used for the
  financial statements, where the aggregation genuinely has to come from
  QuickBooks.
- **`describe_schema` is a tool, not an MCP resource**, even though resources fit
  static reference data better. Claude Desktop requires resources to be attached
  by hand, so it would never be read — and `run_query` without it is guesswork.
- **The QuickBooks query language is not SQL.** No `JOIN`, `GROUP BY`, `OR`,
  `HAVING`, or `!=`. `qbo_sql.py` catches these and explains the workaround
  rather than passing them through to an opaque Intuit 400.
- **`minorversion` is pinned** (currently 75). Intuit changes response shapes
  between minor versions; an unattended server shouldn't have its output shift
  underneath it.
