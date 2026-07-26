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
- *"How far behind am I?"* — everything past due, ranked by what it actually costs your grade
- *"Can I still get a B in BIO 101?"* — real arithmetic on the course's own group weights:
  what you'd need to average on everything that's left
- *"If I get 30/50 on this, where do I land?"* — hypothetical scores, before you decide how
  much to sweat something
- *"What does the lab report actually want?"* — full instructions and rubric, HTML stripped out
- *"Summarise week 6's slides"* — reads the PDF or PowerPoint, not just its filename
- *"Put my deadlines on my phone"* — exports every due date to a calendar file with reminders
- *"Remind me to start the essay Thursday"* — writes a real note into your Canvas planner

---

## Install

Requires Python ≥ 3.10. Nothing to clone — install straight from GitHub:

```bash
pip install "canvas-mcp[all] @ git+https://github.com/mhadifilms/canvas-mcp"
```

Or with [uv](https://docs.astral.sh/uv/), which keeps it in its own environment:

```bash
uv tool install --with browser-cookie3 --with pypdf --with python-docx --with python-pptx \
  "git+https://github.com/mhadifilms/canvas-mcp"
```

The extras matter:

| Extra | Gives you |
|---|---|
| `browsers` | Reading cookies from Chrome, Edge, Brave, Vivaldi, Opera, Arc, Safari |
| `login` | `canvas-mcp login` — opens a browser window for SSO/2FA logins |
| `documents` | Reading PDF, Word and PowerPoint course materials as text |
| `all` | All three |

None are required for Firefox (and forks), which is read with nothing but the standard library.

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
| `triage` | Missing work ranked by real grade impact — the "I've fallen behind" tool |
| `crunch_check` | Weeks where deadlines pile up, flagged while there's still time |
| `daily_digest` | A short brief: what's imminent, what's missing, where the pressure is |
| `export_calendar` | Write every deadline to a .ics for Google/Apple Calendar |
| `list_courses` | Courses with ids, instructors, current grade |
| `grades` | Current grade in each course |
| `announcements` | Recent instructor announcements |
| `todo_list` | Canvas's to-do list plus your own planner notes |

**Grades, done properly**

| Tool | What it does |
|---|---|
| `grade_forecast` | Current standing, where the weight sits, and what you need on what's left |
| `what_if` | Try a hypothetical score and watch the final grade move |
| `triage` | Rank missing work by percentage of the final grade it costs |

These use the course's real assignment group weights and grading scheme. That matters: a
100-point assignment in a 5%-weighted group moves your grade less than a 20-point one in a
50%-weighted group, and points alone will tell you the opposite.

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
| `read_file` | Read a PDF, Word doc, PowerPoint, CSV or page as text |
| `read_assignment_attachments` | Read the files attached to an assignment brief |
| `discussions` | List discussion topics, or read one with its replies |
| `list_quizzes` | Quizzes and exams with time limits and attempt counts |

**Your planner** (these three refuse to run if `CANVAS_MCP_READ_ONLY=1`)

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

## Getting it in front of you without opening a chat

The student most at risk of missing a deadline is the one who won't think to ask about it.
Two commands run without an assistant at all:

```bash
canvas-mcp digest --days 7      # a short brief, cron-able
canvas-mcp ics --days 120       # every deadline as a calendar file
```

A morning brief on macOS or Linux, every weekday at 7am:

```cron
0 7 * * 1-5 /usr/local/bin/canvas-mcp digest >> ~/canvas-brief.txt
```

Or push it to a desktop notification:

```bash
canvas-mcp digest --days 3 | head -20 | xargs -0 -I{} notify-send "Canvas" "{}"     # Linux
canvas-mcp digest --days 3 | osascript -e "display notification (do shell script \"cat\")"  # macOS
```

The calendar export is the higher-leverage one: import it once and every deadline appears on
your phone with reminders a day and two hours ahead. Events carry stable ids, so re-running the
export and re-importing updates the existing entries rather than duplicating them.

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

### Staying connected

Canvas sessions expire on inactivity, which would otherwise mean reconnecting every morning.
Three things prevent that:

- **Keepalive.** While the server is running it touches a cheap endpoint every ten minutes, so
  the session never idles out.
- **The rotated cookie is saved.** Canvas hands out a fresh session cookie as you use it, and
  the saved session is updated to match — so it ages from your last request, not your first.
- **The remember-me cookie is captured too.** If you ticked "stay signed in", that cookie lives
  for weeks and Canvas will mint a new session from it.

If a session does die mid-conversation, tools reconnect silently and retry once. You'll only
see an error if there's genuinely no login left to find, and then it tells you how to fix it.

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
| `CANVAS_MCP_KEEPALIVE_SECONDS` | How often to keep the session warm (default 600, `0` disables) |

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

**"I can't read .pdf as text."** Install the readers: `pip install 'canvas-mcp[documents]'`.

---

## Development

```bash
git clone https://github.com/mhadifilms/canvas-mcp
cd canvas-mcp
pip install -e ".[dev]"
pytest
```

158 tests, no network access required. They cover the HTTP layer against a fake Canvas
(pagination, CSRF, rate-limit retries, cookie scoping, the several ways Canvas says "logged
out"), cookie extraction against a synthetic Firefox profile, the auth fallback chain, and every
tool's rendered output. `tests/test_integration.py` runs the stack over a real socket against a
local server that mimics Canvas's redirect-to-login behaviour, and the calendar output is
round-tripped through an independent iCalendar parser rather than trusted to our own writer.

The grade arithmetic (`tests/test_gradecalc.py`) is tested hardest, because it is the part a
student would act on: weighted and unweighted courses, excused and omitted work, groups with
nothing graded yet, unreachable targets, and the boundary cases where Canvas's own displayed
grade is misleading.

Layout:

```
src/canvas_mcp/
  server.py           MCP tools and prompts
  client.py           HTTP: pagination, CSRF, error translation, course resolution
  queries.py          Canvas fetches shared by the tools and the CLI
  gradecalc.py        grade arithmetic - pure functions, no HTTP
  digest.py           the morning brief
  documents.py        PDF/Word/PowerPoint text extraction
  ics.py              iCalendar export
  auth.py             the credential chain
  browser_cookies.py  reading Firefox (stdlib) and Chromium/Safari (browser_cookie3)
  login.py            Playwright interactive login
  formatting.py       HTML to text, dates humans read
  config.py           the saved session on disk
  cli.py              canvas-mcp subcommands
```
