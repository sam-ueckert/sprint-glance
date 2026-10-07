# Sprint Glance

A small window in the top-right corner of your screen that lists the Jira stories assigned to you in the active sprint, with each one's status. Click an issue key to open it in the browser. The window closes after 90 seconds, or stays open while the pointer is over it. Esc closes it.

You can have it open on a timer (every hour, say) or only when you open it yourself.

It works with Jira Cloud (`*.atlassian.net`) on macOS and Windows. It's one Python file that uses only the standard library, so there's nothing to `pip install`.

## Install

You need Python 3.9 or newer with Tk. The installer looks for one and tells you what to install if it can't find one.

**macOS**

```sh
git clone https://github.com/sam-ueckert/sprint-glance.git ~/sprint-glance
~/sprint-glance/install.sh
```

If you don't have a Python with Tk, the python.org installer has it. So does `brew install python-tk`, or `uv python install 3.12` if you use uv.

**Windows**

Download the repository as a ZIP from GitHub (Code → Download ZIP), extract it somewhere permanent such as `Documents\sprint-glance`, and double-click `install.cmd`.

If you don't have Python, run `winget install Python.Python.3.12`, or use the python.org installer and leave "tcl/tk and IDLE" ticked.

**What setup asks for**

1. Your Jira site, e.g. `yourcompany.atlassian.net`.
2. The email you sign in to Jira with.
3. An API token. Create one at <https://id.atlassian.com/manage-profile/security/api-tokens>. Setup checks it against Jira before saving it.
4. Whether to show the window on a timer and, if so, how often.

Setup also adds a launcher so you can open the window by hand: **Sprint Glance** in `~/Applications` on macOS, or in the Start menu on Windows.

Setup runs from wherever you put the folder. If you move the folder, run the installer again.

## Turning the timer on or off

Run these from the folder. On Windows, use `py` instead of `python3`.

```sh
python3 sprint_glance.py schedule on 30     # every 30 minutes
python3 sprint_glance.py schedule off
python3 sprint_glance.py schedule status
```

On macOS the timer is a LaunchAgent (`~/Library/LaunchAgents/io.github.sprint-glance.plist`). It also shows the window once at login.

On Windows it's a Task Scheduler task named `SprintGlance`. It runs on battery, and the first window after you log in can take up to one interval to appear.

## Where things are kept

| | macOS | Windows |
|---|---|---|
| API token | login Keychain, service `sprint-glance` | Credential Manager, `sprint-glance:<email>` |
| Settings | `~/.config/sprint-glance/config.json` | `%APPDATA%\sprint-glance\config.json` |
| Errors | `~/Library/Logs/sprint-glance.log` | `%APPDATA%\sprint-glance\error.log` |

The token is never written to the settings file, and never passed on a command line.

## Settings

Setup writes `config.json`. You can also edit it by hand:

| Key | What it does |
|---|---|
| `site`, `email` | Set by setup. |
| `extra_assignees` | Jira account IDs whose stories should also count as yours, for when work is split across two accounts. Find an ID in the URL of that person's Jira profile. |
| `jql` | Replaces the built-in search. The default is: assigned to you, in an open sprint, not in a Done status. |
| `auto_close_seconds` | Seconds before the window closes. Default 90. |
| `status_colours` | Badge colours by status name, e.g. `{"In Review": "#9333ea"}`. Otherwise grey means To Do, blue In Progress, green Done. |
| `token_command` | Gets the token by running a command instead of reading the OS store, e.g. `["op", "read", "op://Private/Jira/token"]` for 1Password. The command must print only the token. |
| `sprint_field` | Set by setup, which looks up the Sprint field's ID on your site. |

The environment variable `SPRINT_GLANCE_TOKEN` overrides both the stored token and `token_command`.

## Behind a TLS-inspecting proxy

Corporate networks that inspect HTTPS (Netskope, Zscaler) re-sign traffic with their own CA. Two things in `tls_context()` make that work without extra setup:

- Python 3.13+ rejects some inspection CAs because their Basic Constraints extension isn't marked critical. Sprint Glance switches off that one strictness check (`VERIFY_X509_STRICT`). It still verifies the certificate chain and the hostname.
- On macOS, python.org Python doesn't read the Keychain, so a company CA installed there by MDM isn't trusted. Sprint Glance loads the certificates from the System and login keychains. It loads every certificate it finds and doesn't read the Keychain's per-certificate trust settings, so a certificate you've marked "Never Trust" is still loaded.

Windows Python already reads the Windows certificate store.

## Troubleshooting

- **HTTP 401**: wrong email or token. Run the installer again.
- **HTTP 403 after several failed attempts**: Atlassian has put a CAPTCHA on the account. Sign in once in a browser to clear it. Sprint Glance never retries a failed login, so it won't make the lockout worse.
- **No stories but you have some**: check that they're in a sprint that has been started and not just planned. If they're assigned to a second account, add that account to `extra_assignees`.
- `python3 sprint_glance.py check` prints what the window would show, as JSON, without opening the window.

## Uninstall

```sh
python3 sprint_glance.py uninstall
```

This removes the timer, the launcher, the stored token and the settings. Delete the folder afterwards.
