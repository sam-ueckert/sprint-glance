#!/usr/bin/env python3
"""Sprint Glance: a small always-on-top window listing your open Jira stories
in the active sprint.

    sprint_glance.py                      show the window
    sprint_glance.py setup                connect to Jira, store the token, pick a schedule
    sprint_glance.py schedule on [MIN]    show it every MIN minutes (default 60)
    sprint_glance.py schedule off         stop showing it automatically
    sprint_glance.py schedule status
    sprint_glance.py check                print the stories as JSON, no window
    sprint_glance.py uninstall            remove schedule, launcher, token and config

Standard library only. Jira Cloud only. Runs on macOS and Windows.
"""
import argparse
import base64
import datetime as dt
import getpass
import json
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from xml.sax.saxutils import escape as xml_escape

try:
    import tkinter as tk
except ImportError:
    tk = None

APP = "sprint-glance"
DISPLAY_NAME = "Sprint Glance"
SCRIPT = os.path.abspath(__file__)
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

if IS_WIN:
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP)
else:
    CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "site": "",                 # https://yourcompany.atlassian.net
    "email": "",                # the email you log in to Jira with
    "extra_assignees": [],      # more Jira account IDs whose stories you also own
    "boards": [],               # [{"id": 12, "name": "..."}]; empty means every board
    "jql": "",                  # replaces the built-in query (and boards) when set
    "sprint_field": "customfield_10020",  # setup looks up the real one
    "auto_close_seconds": 90,
    "schedule_days": ["mon", "tue", "wed", "thu", "fri"],  # days timer launches may open the window
    "schedule_start": "00:00",  # HH:MM local; timer launches before this do nothing
    "schedule_end": "18:00",    # HH:MM local; timer launches at or after this do nothing
    "status_colours": {},       # {"In Review": "#9333ea"} overrides by status name
    "token_command": [],        # argv that prints the token, instead of the OS store
}

TOKEN_RE = re.compile(r'[!#-\[\]-~]+')  # printable ASCII without space, " or \
EMAIL_RE = re.compile(r'[^\s"\\@]+@[^\s"\\@]+')
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console flash under pythonw


class SetupNeeded(Exception):
    pass


class JiraError(Exception):
    pass


# ---------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- token store
# macOS: login Keychain, via the `security` tool.
# Windows: Credential Manager, via advapi32 through ctypes.
# The token never goes on a command line, into the config file, or to stdout.

def _mac_get(account):
    r = subprocess.run(["security", "find-generic-password", "-s", APP, "-a", account, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _mac_set(account, token):
    # `security -i` reads commands from stdin, which keeps the token out of argv.
    # TOKEN_RE and EMAIL_RE exclude the quote and backslash that would break this line.
    line = f'add-generic-password -U -s {APP} -a "{account}" -w "{token}"\n'
    subprocess.run(["security", "-i"], input=line, capture_output=True, text=True)


def _mac_delete(account):
    subprocess.run(["security", "delete-generic-password", "-s", APP, "-a", account],
                   capture_output=True)


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE = 1, 2


def _win_target(account):
    return f"{APP}:{account}"


def _win_get(account):
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(_win_target(account), _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        return ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize).decode()
    finally:
        _advapi.CredFree(p)


def _win_set(account, token):
    blob = token.encode()
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _win_target(account)
    cred.UserName = account
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW failed")


def _win_delete(account):
    _advapi.CredDeleteW(_win_target(account), _CRED_TYPE_GENERIC, 0)


def store_get(account):
    if IS_MAC:
        return _mac_get(account)
    if IS_WIN:
        return _win_get(account)
    return None


def store_set(account, token):
    if IS_MAC:
        _mac_set(account, token)
    elif IS_WIN:
        _win_set(account, token)
    else:
        raise SetupNeeded("No credential store on this platform; set SPRINT_GLANCE_TOKEN "
                          "or token_command instead.")
    if store_get(account) != token:
        raise SetupNeeded("The token could not be saved to the credential store.")


def store_delete(account):
    if IS_MAC:
        _mac_delete(account)
    elif IS_WIN:
        _win_delete(account)


def get_token(cfg):
    env = os.environ.get("SPRINT_GLANCE_TOKEN", "").strip()
    if env:
        return env
    if cfg.get("token_command"):
        try:
            r = subprocess.run(cfg["token_command"], capture_output=True, text=True,
                               timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SetupNeeded(f"token_command failed: {e}") from None
        token = r.stdout.strip()
        if r.returncode != 0 or not TOKEN_RE.fullmatch(token):
            raise SetupNeeded("token_command did not print a token.")
        return token
    if not cfg.get("email"):
        raise SetupNeeded("Sprint Glance isn't set up yet.")
    token = store_get(cfg["email"])
    if not token:
        raise SetupNeeded(f"No Jira token stored for {cfg['email']}.")
    return token


# ---------------------------------------------------------------- Jira

_tls = None


def tls_context():
    """Default verification, plus two fixes for corporate TLS inspection.

    1. Python 3.13+ sets VERIFY_X509_STRICT, which rejects inspection CAs
       (Netskope among them) whose Basic Constraints aren't marked critical.
       Clearing it leaves chain and hostname verification on.
    2. python.org Python on macOS ignores the Keychain, so a company CA pushed
       there by MDM isn't trusted. Load the Keychain certificates as well.
       Windows Python already reads the Windows certificate store.
    """
    global _tls
    if _tls:
        return _tls
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if IS_MAC:
        keychains = [k for k in ("/Library/Keychains/System.keychain",
                                 "/System/Library/Keychains/SystemRootCertificates.keychain",
                                 os.path.expanduser("~/Library/Keychains/login.keychain-db"))
                     if os.path.exists(k)]
        r = subprocess.run(["security", "find-certificate", "-a", "-p", *keychains],
                           capture_output=True, text=True)
        for pem in re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                              r.stdout, re.S):
            try:
                ctx.load_verify_locations(cadata=pem)
            except ssl.SSLError:
                pass  # one unparseable cert shouldn't block the rest
    _tls = ctx
    return ctx


HTTP_HINTS = {
    401: "Jira rejected the email or API token (HTTP 401). Run setup again.",
    403: "Jira refused the request (HTTP 403). If you've had several failed logins, "
         "sign in once in a browser to clear the CAPTCHA.",
    404: "Jira returned HTTP 404 (not found).",
}


def jira(cfg, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cfg["site"] + path, data=data, method=method)
    auth = base64.b64encode(f"{cfg['email']}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # No retries: repeated 401s get the account a CAPTCHA.
    try:
        with urllib.request.urlopen(req, context=tls_context(), timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in HTTP_HINTS:
            raise JiraError(HTTP_HINTS[e.code]) from None
        try:
            detail = "; ".join(json.load(e).get("errorMessages", []))
        except ValueError:
            detail = ""
        raise JiraError(f"Jira returned HTTP {e.code}" + (f": {detail}" if detail else "")) from None
    except (urllib.error.URLError, OSError) as e:
        raise JiraError(f"Can't reach {cfg['site']}: {getattr(e, 'reason', e)}") from None


def build_jql(cfg, sprint_ids=None):
    """sprint_ids limits the search to those sprints; None means every open sprint."""
    if cfg.get("jql"):
        return cfg["jql"]
    who = ", ".join(["currentUser()"] + [json.dumps(a) for a in cfg.get("extra_assignees") or []])
    sprints = "openSprints()" if sprint_ids is None else "(" + ", ".join(map(str, sprint_ids)) + ")"
    return (f"assignee in ({who}) AND sprint in {sprints} AND statusCategory != Done "
            "ORDER BY status DESC, priority DESC, updated DESC")


def find_sprint_field(cfg, token):
    for field in jira(cfg, token, "GET", "/rest/api/2/field"):
        if (field.get("schema") or {}).get("custom") == "com.pyxis.greenhopper.jira:gh-sprint":
            return field["id"]
    return None


def search_issues(cfg, token, jql, fields):
    issues, page = [], None
    while True:
        body = {"jql": jql, "maxResults": 100, "fields": fields}
        if page:
            body["nextPageToken"] = page
        data = jira(cfg, token, "POST", "/rest/api/2/search/jql", body)
        issues += data.get("issues", [])
        page = data.get("nextPageToken")
        if not page or len(issues) >= 300:
            return issues


def active_sprint_ids(cfg, token):
    """IDs of the active sprints on the configured boards, looked up on every run
    so a new sprint is picked up without re-running setup."""
    ids, errors = [], []
    for board in cfg["boards"]:
        try:
            data = jira(cfg, token, "GET",
                        f"/rest/agile/1.0/board/{int(board['id'])}/sprint?state=active")
        except JiraError as e:  # deleted board, or a Kanban board with no sprints
            errors.append(f"{board.get('name') or board['id']}: {e}")
            continue
        ids += [s["id"] for s in data.get("values", [])]
    if errors and len(errors) == len(cfg["boards"]):
        raise JiraError("Couldn't read the chosen boards. " + " ".join(errors))
    return ids


def fetch_stories(cfg):
    """Return {"sprints": [{"name", "end", "stories": [...]}, ...]}, soonest end first,
    plus "note" when there is nothing to show for a reason other than no stories."""
    token = get_token(cfg)
    sprint_field = cfg["sprint_field"]
    sprint_ids = None
    if cfg.get("boards") and not cfg.get("jql"):
        sprint_ids = active_sprint_ids(cfg, token)
        if not sprint_ids:
            names = ", ".join(b.get("name") or str(b["id"]) for b in cfg["boards"])
            return {"sprints": [], "note": f"No active sprint on {names}."}
    issues = search_issues(cfg, token, build_jql(cfg, sprint_ids),
                           ["summary", "status", "priority", "issuetype", sprint_field])

    # One person can have stories on several boards, so group by sprint.
    groups = {}
    for issue in issues:
        f = issue["fields"]
        active = [s for s in f.get(sprint_field) or []
                  if isinstance(s, dict) and s.get("state") == "active"]
        sprint = active[0] if active else {}
        group = groups.setdefault(sprint.get("id"), {
            "name": sprint.get("name") or "Open sprint",
            "end": sprint.get("endDate"),
            "stories": [],
        })
        status = f.get("status") or {}
        group["stories"].append({
            "key": issue["key"],
            "summary": f.get("summary") or "",
            "status": status.get("name", ""),
            "category": (status.get("statusCategory") or {}).get("key", ""),
            "url": f"{cfg['site']}/browse/{issue['key']}",
        })
    return {"sprints": sorted(groups.values(), key=lambda g: g["end"] or "9999")}


# ---------------------------------------------------------------- window

FONT = "Helvetica" if IS_MAC else "Segoe UI" if IS_WIN else "DejaVu Sans"
INK, MUTED, FAINT, LINK, BG = "#111827", "#6b7280", "#9ca3af", "#1d4ed8", "white"
CATEGORY_COLOURS = {"new": "#6b7280", "indeterminate": "#2563eb", "done": "#15803d"}
WIDTH_CHARS = 72


def ends_text(end_iso):
    if not end_iso:
        return ""
    end = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone()
    now = dt.datetime.now().astimezone()
    if end < now:
        return "ended"
    days = (end.date() - now.date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return f"ends {end:%a %b} {end.day}, {when}"


class Popup:
    def __init__(self, cfg):
        self.cfg = cfg
        self.auto_close = int(cfg.get("auto_close_seconds") or 90)
        self.remaining = self.auto_close
        self.hovering = False
        self.loading = False

        self.root = tk.Tk()
        self.root.withdraw()  # stay hidden until the first result is drawn
        self.root.title(DISPLAY_NAME)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<Enter>", lambda _e: setattr(self, "hovering", True))
        self.root.bind("<Leave>", lambda _e: setattr(self, "hovering", False))

        body = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        body.pack(fill="both", expand=True)
        bar = tk.Frame(body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.status = tk.Label(bar, fg=FAINT, bg=BG, font=(FONT, 10))
        self.status.pack(side="left")
        tk.Button(bar, text="Close", command=self.root.destroy, highlightbackground=BG).pack(side="right")
        tk.Button(bar, text="Refresh", command=self.refresh, highlightbackground=BG).pack(side="right", padx=6)

        frame = tk.Frame(body, bg=BG)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, width=WIDTH_CHARS, wrap="word", relief="flat", bg=BG, fg=INK,
                            font=(FONT, 12), padx=2, pady=2, cursor="arrow",
                            highlightthickness=0, spacing1=1, spacing3=1)
        self.scroll = tk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.configure_tags()

        self.refresh()
        self.tick()

    def configure_tags(self):
        t = self.text
        t.tag_configure("sprint", font=(FONT, 15, "bold"), foreground=INK)
        t.tag_configure("ends", font=(FONT, 11), foreground=MUTED, spacing3=6)
        t.tag_configure("key", foreground=LINK, underline=True, font=(FONT, 13, "bold"))
        t.tag_configure("summary", foreground=INK, font=(FONT, 13, "bold"), spacing3=4)
        t.tag_configure("error", foreground="#b91c1c")
        t.tag_configure("hint", foreground=MUTED)
        t.tag_configure("gap", font=(FONT, 4))
        for cat, colour in CATEGORY_COLOURS.items():
            t.tag_configure(f"cat:{cat}", foreground="white", background=colour,
                            font=(FONT, 10, "bold"))
        t.tag_configure("cat:other", foreground="white", background="#374151",
                        font=(FONT, 10, "bold"))
        for i, colour in enumerate((self.cfg.get("status_colours") or {}).values()):
            t.tag_configure(f"status:{i}", foreground="white", background=colour,
                            font=(FONT, 10, "bold"))

    def badge_tag(self, story):
        overrides = list((self.cfg.get("status_colours") or {}).keys())
        if story["status"] in overrides:
            return f"status:{overrides.index(story['status'])}"
        cat = story["category"]
        return f"cat:{cat}" if cat in CATEGORY_COLOURS else "cat:other"

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.status.config(text="loading…")
        result = {}

        def work():
            try:
                result["data"] = fetch_stories(self.cfg)
            except SetupNeeded as e:
                result["error"], result["setup"] = str(e), True
            except JiraError as e:
                result["error"] = str(e)
            except Exception as e:  # show it rather than vanish under pythonw
                result["error"] = f"Unexpected error: {e!r}"

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.wait_for(thread, result)

    def wait_for(self, thread, result):
        if thread.is_alive():
            self.root.after(100, self.wait_for, thread, result)
            return
        self.loading = False
        self.remaining = self.auto_close
        self.render(result)
        self.place_top_right()
        self.root.deiconify()
        self.root.lift()

    def render(self, result):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if "error" in result:
            t.insert("end", result["error"], "error")
            if result.get("setup"):
                python = os.path.basename(sys.executable).replace("pythonw", "python")
                t.insert("end", f"\n\nRun setup:\n{python} \"{SCRIPT}\" setup", "hint")
        else:
            sprints = result["data"]["sprints"]
            if not sprints:
                t.insert("end", result["data"].get("note")
                         or "No open stories assigned to you in an active sprint.")
            n = 0
            for g, sprint in enumerate(sprints):
                if g:
                    t.insert("end", "\n\n", "gap")
                t.insert("end", sprint["name"] + "\n", "sprint")
                t.insert("end", (ends_text(sprint["end"]) or " ") + "\n", "ends")
                for i, s in enumerate(sprint["stories"]):
                    if i:
                        t.insert("end", "\n\n", "gap")
                    link = f"link{n}"
                    n += 1
                    t.insert("end", s["key"], ("key", link))
                    t.tag_bind(link, "<Button-1>", lambda _e, u=s["url"]: webbrowser.open(u))
                    t.tag_bind(link, "<Enter>", lambda _e: t.config(cursor="hand2"))
                    t.tag_bind(link, "<Leave>", lambda _e: t.config(cursor="arrow"))
                    t.insert("end", "  ")
                    t.insert("end", f" {s['status']} ", self.badge_tag(s))
                    t.insert("end", "\n" + s["summary"], "summary")
        t.configure(state="disabled")

        # Fit the height to the content, capped near 70% of the screen. Display
        # lines can only be counted once the widget has been laid out.
        self.root.update_idletasks()
        lines = t.count("1.0", "end", "displaylines")
        lines = (lines[0] if isinstance(lines, tuple) else lines) or 1
        max_lines = int(self.root.winfo_screenheight() * 0.7 / 18)
        t.configure(height=max(4, min(lines + 1, max_lines)))
        if lines + 1 > max_lines:
            self.scroll.pack(side="right", fill="y", before=t)
        else:
            self.scroll.pack_forget()
        t.yview_moveto(0)

    def place_top_right(self):
        self.root.update_idletasks()
        w, sw = self.root.winfo_width(), self.root.winfo_screenwidth()
        self.root.geometry(f"+{sw - w - 24}+48")

    def tick(self):
        if not self.loading:
            if not self.hovering:
                self.remaining -= 1
            if self.remaining <= 0:
                self.root.destroy()
                return
            self.status.config(text="paused while the pointer is over it" if self.hovering
                               else f"closes in {self.remaining}s")
        self.root.after(1000, self.tick)


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def within_schedule_window(cfg, now=None):
    """Whether a timer launch should open the window, per schedule_* in config.json."""
    now = now or dt.datetime.now()
    try:
        days = {str(d).strip().lower()[:3] for d in cfg["schedule_days"]}
        start, end = _minutes(cfg["schedule_start"]), _minutes(cfg["schedule_end"])
    except (TypeError, ValueError, KeyError):
        raise RuntimeError("Bad schedule_days / schedule_start / schedule_end in "
                           f"{CONFIG_PATH}; times are HH:MM, days are mon..sun.")
    return DAY_NAMES[now.weekday()] in days and start <= now.hour * 60 + now.minute < end


def cmd_show(args):
    cfg = load_config()
    if getattr(args, "scheduled", False) and not within_schedule_window(cfg):
        return  # timer fired outside the configured window; a manual launch still opens
    if tk is None:
        sys.exit("This Python has no tkinter. Run the installer again to pick one that does.")
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        except (AttributeError, OSError):
            pass
    Popup(cfg).root.mainloop()


def cmd_check(_args):
    try:
        print(json.dumps(fetch_stories(load_config()), indent=2))
    except (SetupNeeded, JiraError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


# ---------------------------------------------------------------- schedule

MAC_LABEL = "io.github.sprint-glance"
MAC_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{MAC_LABEL}.plist")
MAC_LOG = os.path.expanduser("~/Library/Logs/sprint-glance.log")
WIN_TASK = "SprintGlance"


def gui_python():
    """pythonw.exe on Windows, so no console window opens; sys.executable elsewhere."""
    if IS_WIN:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    return sys.executable


def schedule_on(minutes):
    if IS_MAC:
        plist = {
            "Label": MAC_LABEL,
            "ProgramArguments": [gui_python(), SCRIPT, "show", "--scheduled"],
            "StartInterval": minutes * 60,
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
            "ProcessType": "Interactive",
            "StandardOutPath": MAC_LOG,
            "StandardErrorPath": MAC_LOG,
        }
        os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
        with open(MAC_PLIST, "wb") as f:
            plistlib.dump(plist, f)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", domain, MAC_PLIST],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    elif IS_WIN:
        _win_schedule(minutes)
    else:
        raise RuntimeError("Scheduling is only built for macOS and Windows; use cron.")


def _win_schedule(minutes):
    # An XML definition rather than schtasks flags: the flag form leaves the
    # task's "don't start on battery" default on, so laptops would skip runs.
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    user_id = f"{domain}\\{user}" if domain else user
    start = dt.datetime.now().replace(microsecond=0) + dt.timedelta(minutes=1)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Shows your open Jira sprint stories.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT{minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{xml_escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{xml_escape(gui_python())}</Command>
      <Arguments>{xml_escape(f'"{SCRIPT}" show --scheduled')}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, "task.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", WIN_TASK, "/XML", path],
                       capture_output=True, text=True)
    os.remove(path)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks failed: {(r.stderr or r.stdout).strip()}")


def schedule_off():
    if IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{MAC_LABEL}"],
                       capture_output=True)
        if os.path.exists(MAC_PLIST):
            os.remove(MAC_PLIST)
    elif IS_WIN:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK], capture_output=True)


def schedule_status():
    """Interval in minutes if scheduled, else None."""
    if IS_MAC:
        if not os.path.exists(MAC_PLIST):
            return None
        with open(MAC_PLIST, "rb") as f:
            return plistlib.load(f).get("StartInterval", 0) // 60
    if IS_WIN:
        r = subprocess.run(["schtasks", "/Query", "/TN", WIN_TASK, "/XML"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        # Task Scheduler normalises the interval it stores: PT60M comes back as PT1H.
        m = re.search(r"<Interval>P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?</Interval>", r.stdout)
        if not m:
            return 0
        d, h, mins = (int(x or 0) for x in m.groups())
        return d * 1440 + h * 60 + mins
    return None


def cmd_schedule(args):
    if args.action == "on":
        if not 5 <= args.minutes <= 1440:
            sys.exit("Pick an interval between 5 and 1440 minutes.")
        schedule_on(args.minutes)
        print(f"Showing every {args.minutes} minutes.")
    elif args.action == "off":
        schedule_off()
        print("Automatic showing is off. Open it from the launcher whenever you like.")
    else:
        mins = schedule_status()
        print(f"Scheduled every {mins} minutes." if mins else "Not scheduled.")


# ---------------------------------------------------------------- launcher

def launcher_path():
    if IS_MAC:
        return os.path.expanduser(f"~/Applications/{DISPLAY_NAME}.app")
    if IS_WIN:
        return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
                            "Programs", f"{DISPLAY_NAME}.lnk")
    return None


def make_launcher():
    path = launcher_path()
    if IS_MAC:
        def q(s):
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        script = (f"do shell script quoted form of {q(gui_python())} & \" \" & "
                  f"quoted form of {q(SCRIPT)} & \" > /dev/null 2>&1 &\"")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            shutil.rmtree(path)
        subprocess.run(["osacompile", "-o", path, "-e", script], check=True, capture_output=True)
    elif IS_WIN:
        ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SG_LNK); "
              "$s.TargetPath = $env:SG_EXE; $s.Arguments = $env:SG_ARGS; "
              "$s.WorkingDirectory = $env:SG_DIR; $s.Save()")
        env = dict(os.environ, SG_LNK=path, SG_EXE=gui_python(), SG_ARGS=f'"{SCRIPT}"',
                   SG_DIR=os.path.dirname(SCRIPT))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       env=env, check=True, capture_output=True)
    return path


def remove_launcher():
    path = launcher_path()
    if path and os.path.isdir(path):
        shutil.rmtree(path)
    elif path and os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------- setup

def ask(prompt, default=""):
    reply = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return reply or default


def yes(prompt, default=True):
    reply = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not reply else reply.startswith("y")


def normalise_site(s):
    s = s.strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + (s if "." in s else s + ".atlassian.net")
    return "https://" + urllib.parse.urlparse(s).netloc


TOKEN_PAGE = "https://id.atlassian.com/manage-profile/security/api-tokens"


def token_walkthrough():
    print(f"""
Sprint Glance signs in to Jira with an API token. To make one:
  1. Go to {TOKEN_PAGE}
     (sign in with the same Atlassian account as your Jira email).
  2. Click "Create API token". Use the plain one, not "Create API token with scopes".
  3. Name it "Sprint Glance", choose an expiry date, and click Create.
  4. Click Copy, come back here, and paste it below.
When the token expires, run setup again and paste a new one.
""")
    if yes("Open that page in your browser now?"):
        webbrowser.open(TOKEN_PAGE)


def pick_numbers(reply, count):
    """'1, 3' -> [0, 2]; None if reply isn't a list of numbers from 1 to count."""
    try:
        picks = [int(p) for p in reply.replace(",", " ").split()]
    except ValueError:
        return None
    return [p - 1 for p in picks] if picks and all(1 <= p <= count for p in picks) else None


def choose_accounts(cfg, token, me):
    print(f"\nStories assigned to {me.get('displayName') or 'you'} are always included.")
    current = cfg.get("extra_assignees") or []
    if current:
        names = []
        for account_id in current:
            try:
                user = jira(cfg, token, "GET", "/rest/api/3/user?"
                            + urllib.parse.urlencode({"accountId": account_id}))
                names.append(user.get("displayName") or account_id)
            except JiraError:
                names.append(account_id)
        print("Also included: " + ", ".join(names))
        if yes("Keep these?"):
            return
    chosen = []
    if not yes("Are some of your stories assigned to another Jira account, such as a second login?",
               default=False):
        cfg["extra_assignees"] = []
        return
    while True:
        query = ask("Search by name or email (Enter when done)")
        if not query:
            break
        users = jira(cfg, token, "GET", "/rest/api/3/user/search?"
                     + urllib.parse.urlencode({"query": query, "maxResults": 10}))
        users = [u for u in users if u.get("accountType") == "atlassian" and u.get("active", True)
                 and u["accountId"] != me.get("accountId") and u["accountId"] not in chosen]
        if not users:
            print("No matching accounts.")
            continue
        for i, u in enumerate(users, 1):
            email = f" <{u['emailAddress']}>" if u.get("emailAddress") else ""
            print(f"  {i}. {u.get('displayName')}{email}")
        picks = pick_numbers(ask("Number(s) to add (Enter to skip)"), len(users))
        for p in picks or []:
            chosen.append(users[p]["accountId"])
            print(f"Added {users[p].get('displayName')}.")
    cfg["extra_assignees"] = chosen


def board_name(cfg, token, board_id):
    try:
        return jira(cfg, token, "GET", f"/rest/agile/1.0/board/{int(board_id)}").get("name")
    except JiraError:
        return None


def choose_boards(cfg, token):
    if cfg.get("jql"):
        print("\nA custom jql is set in the config file, so board choice is skipped.")
        return
    # Suggest the boards whose active sprints already hold your stories.
    found = {}
    issues = search_issues(cfg, token, build_jql(cfg), [cfg["sprint_field"]])
    for issue in issues:
        for s in issue["fields"].get(cfg["sprint_field"]) or []:
            if isinstance(s, dict) and s.get("state") == "active" and s.get("boardId"):
                found.setdefault(s["boardId"], None)
    options = [{"id": b, "name": board_name(cfg, token, b) or f"Board {b}"} for b in found]
    for b in cfg.get("boards") or []:
        if b["id"] not in found:
            options.append(b)
    current = {b["id"] for b in cfg.get("boards") or []}

    print("\nWhich boards should it show?")
    while True:
        print("  0. All boards: every active sprint you have stories in")
        for i, b in enumerate(options, 1):
            print(f"  {i}. {b['name']}" + ("  (current)" if b["id"] in current else ""))
        default = ", ".join(str(i) for i, b in enumerate(options, 1) if b["id"] in current) or "0"
        reply = ask("Numbers separated by commas, 0 for all, or part of a board name to search",
                    default)
        if reply.strip() == "0":
            cfg["boards"] = []
            print("Showing all boards.")
            return
        picks = pick_numbers(reply, len(options))
        if picks is not None:
            usable = []
            for b in (options[p] for p in picks):
                try:  # Kanban boards answer 400 "The board does not support sprints"
                    jira(cfg, token, "GET",
                         f"/rest/agile/1.0/board/{int(b['id'])}/sprint?state=active&maxResults=1")
                    usable.append(b)
                except JiraError as e:
                    print(f"Can't use {b['name']}: {e}")
            if usable:
                cfg["boards"] = usable
                print("Showing " + ", ".join(b["name"] for b in usable) + ".")
                return
            continue
        if reply.strip().isdigit() or "," in reply:
            print("Pick from the numbers listed.")
            continue
        results = jira(cfg, token, "GET", "/rest/agile/1.0/board?"
                       + urllib.parse.urlencode({"name": reply.strip(), "maxResults": 20}))
        new = [{"id": b["id"], "name": b["name"]} for b in results.get("values", [])
               if b["id"] not in {o["id"] for o in options}]
        if not new:
            print(f"No other boards match {reply.strip()!r}.")
        options += new


def cmd_setup(_args):
    cfg = load_config()
    print(f"{DISPLAY_NAME} setup. Press Enter to keep a value in brackets.\n")
    while True:
        cfg["site"] = normalise_site(ask("Jira site, e.g. yourcompany.atlassian.net",
                                         cfg["site"].replace("https://", "")))
        cfg["email"] = ask("Email you log in to Jira with", cfg["email"])
        if not cfg["site"] or not EMAIL_RE.fullmatch(cfg["email"]):
            print("Need a site and a valid email.\n")
            continue

        stored = None if cfg.get("token_command") else store_get(cfg["email"])
        if cfg.get("token_command"):
            print("Using token_command from the config file.")
            token = get_token(cfg)
        elif stored and yes("A token is already stored for this email. Keep it?"):
            token = stored
        else:
            token_walkthrough()
            token = getpass.getpass("Paste the token (nothing shows as you paste): ").strip()
            if not TOKEN_RE.fullmatch(token):
                print("That doesn't look like an API token.\n")
                continue

        try:
            me = jira(cfg, token, "GET", "/rest/api/2/myself")
        except JiraError as e:
            print(f"\n{e}\n")
            if not yes("Try again?"):
                sys.exit(1)
            continue
        break

    print(f"\nConnected as {me.get('displayName') or cfg['email']}.")
    if not cfg.get("token_command") and token != stored:
        store_set(cfg["email"], token)
        print("Token saved to the " + ("macOS Keychain." if IS_MAC else "Windows Credential Manager."))
    cfg["sprint_field"] = find_sprint_field(cfg, token) or cfg["sprint_field"]
    before = (list(cfg["extra_assignees"]), list(cfg["boards"]))
    try:
        choose_accounts(cfg, token, me)
        choose_boards(cfg, token)
    except JiraError as e:
        cfg["extra_assignees"], cfg["boards"] = before
        print(f"\n{e}\nKeeping the previous account and board choices.")
    save_config(cfg)
    print(f"Settings saved to {CONFIG_PATH}")

    try:
        sprints = fetch_stories(cfg)["sprints"]
        count = sum(len(g["stories"]) for g in sprints)
        names = ", ".join(g["name"] for g in sprints) or "no active sprint"
        print(f"Found {count} open stor{'y' if count == 1 else 'ies'} ({names}).")
    except JiraError as e:
        print(f"Connected, but the story search failed: {e}")

    if tk is None:
        print("\nWarning: this Python has no tkinter, so the window can't open.")
        return
    try:
        print(f"Launcher created: {make_launcher()}")
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Couldn't create a launcher ({e}); you can still run the script directly.")

    print()
    current = schedule_status()
    if yes("Show it automatically on a timer?", default=bool(current) or current is None):
        while True:
            try:
                minutes = int(ask("Every how many minutes", str(current or 60)))
                if 5 <= minutes <= 1440:
                    break
            except ValueError:
                pass
            print("Enter a number from 5 to 1440.")
        schedule_on(minutes)
        print(f"Scheduled every {minutes} minutes."
              + (" It will also open now." if IS_MAC else " The first one opens in a minute."))
    else:
        schedule_off()
        print("Not scheduled. Open it from the launcher when you want it.")
        if yes("Show it now?"):
            subprocess.Popen([gui_python(), SCRIPT], creationflags=NO_WINDOW)


def cmd_uninstall(_args):
    cfg = load_config()
    if not yes(f"Remove the schedule, launcher, stored token and {CONFIG_DIR}?", default=False):
        return
    schedule_off()
    remove_launcher()
    if cfg.get("email"):
        store_delete(cfg["email"])
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    print(f"Removed. Delete {os.path.dirname(SCRIPT)} yourself to finish.")


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(prog="sprint_glance.py", description=DISPLAY_NAME)
    sub = parser.add_subparsers(dest="command")
    show = sub.add_parser("show", help="show the window (the default)")
    show.add_argument("--scheduled", action="store_true",
                      help="launched by the timer: do nothing outside the schedule_* window in config.json")
    sub.add_parser("setup", help="connect to Jira and choose a schedule")
    sub.add_parser("check", help="print stories as JSON")
    sub.add_parser("uninstall", help="remove everything setup created")
    sched = sub.add_parser("schedule", help="turn the timer on or off")
    sched.add_argument("action", choices=["on", "off", "status"])
    sched.add_argument("minutes", nargs="?", type=int, default=60)
    args = parser.parse_args()
    commands = {"setup": cmd_setup, "check": cmd_check, "schedule": cmd_schedule,
                "uninstall": cmd_uninstall}
    commands.get(args.command, cmd_show)(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (SetupNeeded, RuntimeError) as e:
        sys.exit(str(e))
    except Exception:
        if sys.stderr is None:  # pythonw has nowhere to print; leave a log instead
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "error.log"), "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now().isoformat()}\n{traceback.format_exc()}\n")
        raise
