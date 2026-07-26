# canvas-mcp

An MCP server that gives an AI assistant read access to your Canvas courses — **without an API key**.

Canvas has a perfectly good REST API, but a lot of schools disable the page where students
generate access tokens ("Approved Integrations" simply isn't there). That locks students out
of their own coursework data for no good reason.

This server takes the other route Canvas already supports: **the session your browser is
already using**. Canvas's own web interface authenticates to `/api/v1/...` with a session
cookie, nothing more. The server runs on your computer, borrows that cookie from the browser
you're logged into, and calls the same endpoints the Canvas dashboard calls. Same account,
same permissions, same data, no token page required.

Built for the student who's juggling five classes, two jobs, and a Canvas dashboard that
tells you everything except what actually matters this week.

---

## What you can ask for

Once it's connected, these are ordinary questions:

- *"What's due this week?"* — every class, in one list, sorted by day
- *"How far behind am I?"* — everything past due that hasn't been turned in, with points at stake
- *"What does the lab report actually want?"* — full instructions and rubric, HTML stripped out
- *"Can I still pass BIO 101?"* — current grade plus the assignment-by-assignment breakdown
- *"Did anything change?"* — recent announcements across all courses
- *"Remind me to start the essay Thursday"* — writes a real note into your Canvas planner, so it
  shows up on your phone too

---

## Install

```bash
git clone <this repo>
cd canvas-mcp
pip install -e ".[all]"
```

The extras matter:

| Extra | Gives you |
|---|---|
| `browsers` | Reading cookies from Chrome, Edge, Brave, Vivaldi, Opera, Arc, Safari |
| `login` | `canvas-mcp login` — opens a browser window for SSO/2FA logins |
| `all` | Both |

Neither is required for Firefox (and forks), which is read with nothing but the standard library.

If you install the `login` extra, also run once:

```bash
python -m playwright install chromium
```

---

## Connect

Three ways, easiest first. You only need one, and only once — the session is saved.

### 1. Borrow the session from your browser

Log in to Canvas in your normal browser, leave it logged in, then:

```bash
canvas-mcp import-cookies
```

Or just start the server and ask your assistant to run the `connect` tool — it does the same
scan automatically the first time a tool needs Canvas.

On macOS, Chrome and Safari cookies live behind the system Keychain, so you'll get one
permission prompt. That's the OS doing its job.

### 2. Log in through a browser window

For schools with SSO, Duo, or a browser whose cookie store can't be decrypted:

```bash
canvas-mcp login --base-url https://yourschool.instructure.com
```

A browser window opens on your school's normal login page. Sign in as usual. The window
closes itself once Canvas hands over a session.

### 3. Paste the cookie yourself

Always works, takes about thirty seconds:

```bash
canvas-mcp set-cookie --base-url https://yourschool.instructure.com
```

It'll walk you through finding `canvas_session` in DevTools → Application (or Storage) →
Cookies, and take the value on a hidden prompt.

> The cookie is `HttpOnly`, so it won't appear in the Console tab — you need the
> Application/Storage panel.

### Check it worked

```bash
canvas-mcp status     # is the saved session still good?
canvas-mcp doctor     # what did it find, what's missing, why isn't it connecting?
```

---

## Point your assistant at it

**Claude Code:**

```bash
claude mcp add canvas -- canvas-mcp
```

**Claude Desktop / any client with a JSON config:**

```json
{
  "mcpServers": {
    "canvas": {
      "command": "canvas-mcp",
      "args": []
    }
  }
}
```

If `canvas-mcp` isn't on your PATH, use the full interpreter path and `["-m", "canvas_mcp"]`
as the args.

---

## Tools

**Getting connected**

| Tool | What it does |
|---|---|
| `canvas_status` | Are we connected, and as whom |
| `connect` | Find and use a Canvas session on this computer |
| `browser_login` | Open a browser window to log in through the school's sign-in page |
| `disconnect` | Delete the saved session |

**Across all courses**

| Tool | What it does |
|---|---|
| `upcoming` | Everything due in the next N days, grouped by day |
| `missing_work` | Past due and not submitted, with points at stake and whether it's still open |
| `list_courses` | Courses with ids, instructors, current grade |
| `grades` | Current grade in each course |
| `announcements` | Recent instructor announcements |
| `todo_list` | Canvas's to-do list plus your own planner notes |

**Inside one course**

| Tool | What it does |
|---|---|
| `course_overview` | Syllabus, instructors, modules, what's due soon |
| `list_assignments` | All assignments, filterable by `upcoming` / `overdue` / `unsubmitted` / … |
| `get_assignment` | Full instructions, rubric, dates, your submission status and feedback |
| `grades` | Assignment-by-assignment breakdown with instructor comments |
| `course_modules` | Module structure in teaching order |
| `get_page` | Read a Canvas page as plain text |
| `list_files` / `download_file` | Find and download course materials |
| `discussions` | List discussion topics, or read one with its replies |
| `list_quizzes` | Quizzes and exams with time limits and attempt counts |

**Your planner** (skipped entirely if `CANVAS_MCP_READ_ONLY=1`)

| Tool | What it does |
|---|---|
| `add_todo` | Add a personal to-do to the Canvas planner |
| `delete_todo` | Remove one |
| `mark_done` | Tick an item off in the planner — without submitting anything |

Course arguments are forgiving. `"BIO 101"`, `"bio101"`, `"biology"`, and `"101"` all work; if a
name is genuinely ambiguous the tool says so and lists the candidates instead of guessing.

Two prompts ship with the server: `weekly_plan` (build a schedule) and `catch_up` (triage after
falling behind).

---

## What it won't do

There are no tools for submitting an assignment, posting a discussion reply, or taking a quiz —
not because they'd be hard to write, but because they're the line between helping someone manage
their work and doing their work. A test asserts those tool names stay absent.

Helping you *understand* an assignment, plan around it, and see where you stand is the point.
Handing it in for you isn't.

---

## Privacy and safety

- **Everything is local.** The server runs on your machine and talks only to your school's
  Canvas host. There's no intermediary service.
- **The saved session lives at `~/.canvas-mcp/session.json`**, written `0600` in a `0700`
  directory. Treat it like a password — anyone holding that cookie is logged in as you. Run
  `canvas-mcp logout` on a shared computer.
- **Cookies are scoped to the Canvas host.** If Canvas redirects to your school's SSO domain,
  the session cookie is not sent there. (There's a test for this.)
- **Browser scans only keep Canvas cookies.** When the target host is already known, only that
  host's cookies are read at all.
- **This is your own account.** It sees exactly what you see when you log in — nothing more, no
  privilege escalation, no other students' data.

Sessions expire, usually in a day or so. When that happens tools say so plainly and tell you how
to reconnect, rather than failing with a stack trace.

---

## Configuration

All optional.

| Variable | Effect |
|---|---|
| `CANVAS_BASE_URL` | Your Canvas host, e.g. `https://yourschool.instructure.com` |
| `CANVAS_SESSION_COOKIE` | Supply the session cookie directly instead of saving one |
| `CANVAS_API_TOKEN` | Use a real API token, if your school actually lets you have one |
| `CANVAS_MCP_READ_ONLY` | `1` disables the three planner-write tools |
| `CANVAS_MCP_AUTO_IMPORT` | `0` stops the automatic browser scan; connect explicitly instead |
| `CANVAS_MCP_TZ` | IANA timezone for due dates (defaults to your Canvas profile, then this computer) |
| `CANVAS_MCP_HOME` | Where the session is stored (default `~/.canvas-mcp`) |

---

## Troubleshooting

**"I couldn't find a working Canvas session on this computer."**
Run `canvas-mcp doctor`. It lists every browser profile it can read, what it found in each, and
what stopped it. Most often: not logged in right now, the wrong browser profile, or
`browser-cookie3` isn't installed so Chrome was skipped.

**Chrome cookies fail on Windows.** Chrome 127+ added App-Bound Encryption, which
`browser-cookie3` can't always get past. Use `canvas-mcp login` or `set-cookie` instead.

**"Canvas rejected the saved session."** It expired, or you logged out of the browser it came
from. Log back in to Canvas and run `canvas-mcp import-cookies` again.

**Your school requires a VPN.** So does this — it's making the same requests your browser does.

**Everything is an hour off.** Set `CANVAS_MCP_TZ` to your IANA zone, e.g. `America/Chicago`.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

68 tests, no network access required. They cover the HTTP layer against a fake Canvas
(pagination, CSRF, rate-limit retries, cookie scoping, the several ways Canvas says "logged
out"), cookie extraction against a synthetic Firefox profile, the auth fallback chain, and every
tool's rendered output. `tests/test_integration.py` runs the stack over a real socket against a
local server that mimics Canvas's redirect-to-login behaviour.

Layout:

```
src/canvas_mcp/
  server.py           MCP tools and prompts
  client.py           HTTP: pagination, CSRF, error translation, course resolution
  auth.py             the credential chain
  browser_cookies.py  reading Firefox (stdlib) and Chromium/Safari (browser_cookie3)
  login.py            Playwright interactive login
  formatting.py       HTML to text, dates humans read
  config.py           the saved session on disk
  cli.py              canvas-mcp subcommands
```
