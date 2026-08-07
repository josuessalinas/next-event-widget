"""Next-event countdown widget.

Always-on-top Liquid Glass window showing a live countdown to the next
event across all configured sources:
  - Google Calendar API (OAuth, sees every calendar of the account)
  - any number of ICS feed URLs
Stdlib only.
"""

import ctypes
import json
import re
import subprocess
import threading
import tkinter as tk
from tkinter import simpledialog, messagebox
import urllib.request
import urllib.parse
import webbrowser
import http.server
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")
CLIENT_SECRET_PATH = Path(__file__).with_name("client_secret.json")
SHOW_FLAG = Path(__file__).with_name("show.flag")
FETCH_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes
TICK_MS = 5000                     # UI redraw; display has minute granularity
LOOKAHEAD_DAYS = 30
OAUTH_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
NOTIFY_BEFORE_S = 10 * 60          # toast 10 min before an event
MIN_W, MIN_H = 150, 74             # smallest the card can be dragged to
MAX_LIST_ROWS = 6
# measured block heights, padding included — the layout budget depends on
# them, so they must match what the widgets actually request
ROW_H = 23
AGENDA_BASE = 38                   # top margin + separator + section header
STATS_H = 30
PAD = 28                           # frame padding + border
H_NOW, H_TITLE, H_TIME, H_COUNT = 26, 28, 26, 37   # normal-view lines
H_FTITLE, H_NEXT = 35, 24          # focus-view lines
H_RING_MIN, MAX_RING = 36, 150     # the ring grows with the card
MEETING_URL = re.compile(
    r"https?://(?:[\w.-]*\.)?(?:meet\.google\.com|zoom\.us|teams\.microsoft"
    r"\.com|teams\.live\.com)/[^\s\"'<>\\,;]+")


def find_meeting_link(*texts):
    for t in texts:
        if t:
            m = MEETING_URL.search(t)
            if m:
                return m.group(0)
    return None


def toast(title, msg):
    """Native Windows toast notification (with the default sound)."""
    esc_t = title.replace("'", "''")
    esc_m = msg.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::"
        "ToastText02);"
        "$t = $x.GetElementsByTagName('text');"
        f"$t.Item(0).AppendChild($x.CreateTextNode('{esc_t}')) | Out-Null;"
        f"$t.Item(1).AppendChild($x.CreateTextNode('{esc_m}')) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
        "\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n);")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        creationflags=0x08000000)  # CREATE_NO_WINDOW


def fullscreen_app_active():
    """True while Windows itself considers a fullscreen app to be running.

    Uses the same signal Windows uses to suppress notifications, so a
    maximized window never counts (geometry heuristics got that wrong,
    especially with an auto-hidden taskbar, and left the widget hidden
    for good).
    """
    state = ctypes.c_int()
    hr = ctypes.windll.shell32.SHQueryUserNotificationState(
        ctypes.byref(state))
    if hr != 0:
        return False
    # 2 QUNS_BUSY, 3 QUNS_RUNNING_D3D_FULL_SCREEN, 4 QUNS_PRESENTATION_MODE
    return state.value in (2, 3, 4)

# iOS dark-mode system palette
BG = "#1c1c1e"          # systemGray6 dark — tint under the acrylic blur
FG = "#ffffff"          # label
DIM = "#98989f"         # secondaryLabel over dark
AMBER = "#ff9f0a"       # systemOrange
RED = "#ff453a"         # systemRed
GREEN = "#0a84ff"       # systemBlue — countdown accent
CYAN = "#64d2ff"        # systemCyan — the event coming up next
LABEL2 = "#d1d1d6"      # secondaryLabel — agenda titles
TERTIARY = "#6e6e73"    # tertiaryLabel — section header, day summary
SEP = "#38383a"         # separator hairline


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def apply_liquid_glass(root, tint_rgb=(40, 40, 46), tint_alpha=0x66):
    """Windows 11 acrylic blur-behind + rounded corners on a tk window."""
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()

    class AccentPolicy(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int),
                    ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint),
                    ("AnimationId", ctypes.c_int)]

    class WinCompatData(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int),
                    ("Data", ctypes.POINTER(AccentPolicy)),
                    ("SizeOfData", ctypes.c_size_t)]

    r, g, b = tint_rgb
    accent = AccentPolicy()
    accent.AccentState = 4  # ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 2
    accent.GradientColor = (tint_alpha << 24) | (b << 16) | (g << 8) | r
    data = WinCompatData()
    data.Attribute = 19  # WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)
    try:
        ctypes.windll.user32.SetWindowCompositionAttribute(
            hwnd, ctypes.byref(data))
    except Exception:
        pass  # older Windows: solid background remains
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


# ---------- ICS parsing ----------

def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_dt(value, params):
    """Return an aware datetime in local time, or None."""
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):  # all-day
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=datetime.now().astimezone().tzinfo)
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2) == "Z":
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone()


def _expand_rrule(start, rrule, dur, now, horizon):
    """Yield instances (ongoing or future) for simple DAILY/WEEKLY rules."""
    m = re.search(r"FREQ=(DAILY|WEEKLY)", rrule)
    if not m:
        return
    step = timedelta(days=1 if m.group(1) == "DAILY" else 7)
    iv = re.search(r"INTERVAL=(\d+)", rrule)
    if iv:
        step *= int(iv.group(1))
    until = None
    u = re.search(r"UNTIL=([0-9TZ]+)", rrule)
    if u:
        until = _parse_dt(u.group(1), {})
    count = None
    c = re.search(r"COUNT=(\d+)", rrule)
    if c:
        count = int(c.group(1))
    byday = re.search(r"BYDAY=([^;]+)", rrule)
    if byday and "," in byday.group(1):
        return
    inst = start
    n = 0
    while inst <= horizon:
        if count is not None and n >= count:
            return
        if until and inst > until:
            return
        if inst + dur > now:
            yield inst
        inst += step
        n += 1


def parse_events(ics_text):
    """Return [(title, start, end)] of ongoing/future events in the window."""
    now = datetime.now().astimezone()
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT",
                            _unfold(ics_text), re.S):
        dt_m = re.search(r"^DTSTART([^:]*):(.+)$", block, re.M)
        if not dt_m:
            continue
        start = _parse_dt(dt_m.group(2), dt_m.group(1))
        if start is None:
            continue
        end_m = re.search(r"^DTEND([^:]*):(.+)$", block, re.M)
        end = _parse_dt(end_m.group(2), end_m.group(1)) if end_m else None
        dur = (end - start) if end and end > start else timedelta(hours=1)
        sum_m = re.search(r"^SUMMARY[^:]*:(.+)$", block, re.M)
        title = sum_m.group(1).strip() if sum_m else "(sin título)"
        title = title.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
        loc_m = re.search(r"^LOCATION[^:]*:(.+)$", block, re.M)
        desc_m = re.search(r"^DESCRIPTION[^:]*:(.+)$", block, re.M)
        link = find_meeting_link(loc_m and loc_m.group(1),
                                 desc_m and desc_m.group(1))
        candidates = []
        rr_m = re.search(r"^RRULE:(.+)$", block, re.M)
        if rr_m:
            candidates = list(_expand_rrule(start, rr_m.group(1), dur,
                                            now, horizon))
        elif start <= horizon and start + dur > now:
            candidates = [start]
        for c in candidates:
            events.append((title, c, c + dur, link))
    return events


# ---------- Google Calendar API (OAuth, stdlib only) ----------

def load_client_secret():
    """Return (client_id, client_secret) from client_secret.json, or None."""
    try:
        data = json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8-sig"))
        key = "installed" if "installed" in data else "web"
        return data[key]["client_id"], data[key]["client_secret"]
    except (OSError, ValueError, KeyError):
        return None


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def oauth_authorize(client_id, client_secret):
    """Run the installed-app OAuth flow. Returns the token response dict."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q or "error" in q:
                result["code"] = q.get("code", [None])[0]
                result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Listo ✅</h2>Puedes cerrar esta "
                             "pestaña y volver al widget.".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    auth_url = ("https://accounts.google.com/o/oauth2/v2/auth?"
                + urllib.parse.urlencode({
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": OAUTH_SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",
                }))
    webbrowser.open(auth_url)
    srv.timeout = 10
    deadline = datetime.now() + timedelta(minutes=5)
    while "code" not in result and datetime.now() < deadline:
        srv.handle_request()
    srv.server_close()
    if not result.get("code"):
        raise RuntimeError(result.get("error") or "Autorización cancelada")
    tok = _post_form("https://oauth2.googleapis.com/token", {
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    })
    if "refresh_token" not in tok:
        raise RuntimeError("Google no devolvió refresh_token")
    return tok


def _api_get(url, access_token):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _g_dt(field, local_tz):
    """Google event start/end dict -> aware local datetime, or None."""
    if "dateTime" in field:
        return datetime.fromisoformat(field["dateTime"]).astimezone()
    if "date" in field:
        return datetime.fromisoformat(field["date"]).replace(
            tzinfo=local_tz).astimezone()
    return None


# access token (~1 h) and calendar list (changes rarely) are cached so a
# routine refresh costs one events request per calendar, nothing more
_g_cache = {"access": None, "access_exp": None, "cals": None, "cals_exp": None}


def _get_access(refresh_token, client_id, client_secret):
    now = datetime.now()
    if _g_cache["access"] and _g_cache["access_exp"] > now:
        return _g_cache["access"]
    tok = _post_form("https://oauth2.googleapis.com/token", {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    _g_cache["access"] = tok["access_token"]
    _g_cache["access_exp"] = now + timedelta(
        seconds=int(tok.get("expires_in", 3600)) - 120)
    return _g_cache["access"]


def _get_calendars(access):
    now = datetime.now()
    if _g_cache["cals"] is not None and _g_cache["cals_exp"] > now:
        return _g_cache["cals"]
    cals = _api_get(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList"
        "?fields=items(id,summary,selected)", access).get("items", [])
    _g_cache["cals"] = cals
    _g_cache["cals_exp"] = now + timedelta(hours=6)
    return cals


def google_events(refresh_token, client_id, client_secret):
    """Return [(title, start, end)] of ongoing/future events, all calendars."""
    access = _get_access(refresh_token, client_id, client_secret)
    now = datetime.now().astimezone()
    time_min = urllib.parse.quote(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    time_max = urllib.parse.quote(
        (datetime.now(timezone.utc) + timedelta(days=LOOKAHEAD_DAYS))
        .strftime("%Y-%m-%dT%H:%M:%SZ"))
    events = []
    for cal in _get_calendars(access):
        cal_id = urllib.parse.quote(cal["id"], safe="")
        evs = _api_get(
            f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
            f"?timeMin={time_min}&timeMax={time_max}&maxResults=5"
            "&singleEvents=true&orderBy=startTime"
            "&fields=items(summary,status,start,end,hangoutLink,"
            "location,description)", access)
        for e in evs.get("items", []):
            if e.get("status") == "cancelled":
                continue
            start = _g_dt(e.get("start", {}), now.tzinfo)
            if start is None:
                continue
            end = _g_dt(e.get("end", {}), now.tzinfo)
            if end is None or end <= start:
                end = start + timedelta(hours=1)
            title = e.get("summary", "(sin título)")
            link = e.get("hangoutLink") or find_meeting_link(
                e.get("location"), e.get("description"))
            events.append((title, start, end, link))
    return events


# ---------- widget ----------

class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = load_config()
        urls = self.cfg.get("ics_urls") or []
        if self.cfg.get("ics_url"):  # migrate old single-URL config
            urls.append(self.cfg.pop("ics_url"))
        self.cfg["ics_urls"] = urls
        save_config(self.cfg)

        self.next_event = None      # (title, start, end, link) or None
        self.current_event = None   # event happening right now, or None
        self.upcoming = []          # every future event, soonest first
        self.error = None
        self.notified = set()       # events already toast-notified
        self._stack_key = None      # which blocks are currently packed
        self._focus_text = None     # text lines shown in the focus view
        self._hidden_fs = False     # hidden because a fullscreen app is up
        self._hidden_until = None   # hidden by the user until this time
        self._is_hidden = False     # window currently parked off-screen
        self._focus_view = None     # showing the focus layout (None = unset)
        self.view_mode = tk.StringVar(
            value=self.cfg.get("view_mode", "auto"))

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        x, y = self.cfg.get("x", 60), self.cfg.get("y", 60)
        w = max(MIN_W, self.cfg.get("w", 212))
        h = max(MIN_H, self.cfg.get("h", 119))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        frame = tk.Frame(self.root, bg=BG, padx=20, pady=13,
                         highlightthickness=1,
                         highlightbackground="#8a8a94")
        frame.pack(fill="both", expand=True)
        self.frame = frame
        self.now_row = tk.Frame(frame, bg=BG)
        self.ring = tk.Canvas(self.now_row, width=18, height=18, bg=BG,
                              highlightthickness=0)
        self.ring.pack(side="left", padx=(0, 7))
        self.now_lbl = tk.Label(self.now_row, text="", bg=BG, fg="#30d158",
                                font=("Segoe UI Variable Text Semibold", 10))
        self.now_lbl.pack(side="left")
        self.title_lbl = tk.Label(frame, text="Cargando…", bg=BG, fg=FG,
                                  font=("Segoe UI Variable Text Semibold",
                                        13))
        self.title_lbl.pack(anchor="w")
        self.time_lbl = tk.Label(frame, text="", bg=BG, fg=DIM,
                                 font=("Segoe UI Variable Text", 11))
        self.time_lbl.pack(anchor="w")
        self.count_lbl = tk.Label(frame, text="—", bg=BG, fg=GREEN,
                                  font=("Segoe UI Variable Display Semib",
                                        16, "bold"))
        self.count_lbl.pack(anchor="w", pady=(3, 0))

        # focus view: progress of the current event as the hero, next event
        # demoted to the small line — the normal view inverted
        self.focus_box = tk.Frame(frame, bg=BG)
        self.big_ring = tk.Canvas(self.focus_box, width=78, height=78, bg=BG,
                                  highlightthickness=0)
        self.big_ring.pack()
        self.f_title = tk.Label(self.focus_box, text="", bg=BG, fg=FG,
                                font=("Segoe UI Variable Text Semibold", 13))
        self.f_title.pack(pady=(8, 0))
        self.f_next = tk.Label(self.focus_box, text="", bg=BG, fg=CYAN,
                               font=("Segoe UI Variable Text", 10))
        self.f_next.pack(pady=(2, 0))

        # agenda: shown when the card is tall enough to fit rows
        self.list_box = tk.Frame(frame, bg=BG)
        tk.Frame(self.list_box, bg=SEP, height=1).pack(fill="x", pady=(0, 7))
        self.list_head = tk.Label(self.list_box, text="P R Ó X I M O S",
                                  bg=BG, fg=TERTIARY,
                                  font=("Segoe UI Variable Text", 7, "bold"))
        self.list_head.pack(anchor="w", pady=(0, 3))
        self.list_rows = []
        for _ in range(MAX_LIST_ROWS):
            row = tk.Frame(self.list_box, bg=BG)
            stamp = tk.Label(row, text="", bg=BG, fg=CYAN, anchor="w",
                             font=("Segoe UI Variable Text", 10))
            stamp.pack(side="left")
            name = tk.Label(row, text="", bg=BG, fg=LABEL2, anchor="w",
                            font=("Segoe UI Variable Text", 10))
            name.pack(side="left", padx=(8, 0))
            self.list_rows.append((row, stamp, name))
        self.stats_lbl = tk.Label(frame, text="", bg=BG, fg=TERTIARY,
                                  font=("Segoe UI Variable Text", 9))

        # resize grip, bottom-right corner
        self.grip = tk.Canvas(self.root, width=14, height=14, bg=BG,
                              highlightthickness=0, cursor="size_nw_se")
        for off in (2, 6, 10):
            self.grip.create_line(13 - off, 13, 13, 13 - off, fill="#6e6e73")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<Button-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<ButtonRelease-1>", self._resize_end)

        self._blocks = (self.now_row, self.title_lbl, self.time_lbl,
                        self.count_lbl, self.focus_box, self.list_box,
                        self.stats_lbl)

        # bound on the toplevel only: it sits in every child's bindtags, so
        # one binding covers the whole card. Binding the children as well
        # fired each handler twice (a click opened two tabs).
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Actualizar ahora", command=self.fetch_async)
        self.menu.add_command(label="Conectar cuenta Google…",
                              command=self._connect_google)
        self.menu.add_command(label="Agregar calendario ICS…",
                              command=self._add_url)
        self.menu.add_command(label="Quitar calendarios ICS",
                              command=self._clear_urls)
        view_menu = tk.Menu(self.menu, tearoff=0)
        for label, val in (("Automática", "auto"), ("Normal", "normal"),
                           ("Enfoque", "focus")):
            view_menu.add_radiobutton(label=label, value=val,
                                      variable=self.view_mode,
                                      command=self._save_view_mode)
        self.menu.add_cascade(label="Vista", menu=view_menu)
        hide_menu = tk.Menu(self.menu, tearoff=0)
        for label, mins in (("15 min", 15), ("30 min", 30),
                            ("1 hora", 60), ("2 horas", 120)):
            hide_menu.add_command(label=label,
                                  command=lambda m=mins: self._hide_for(m))
        self.menu.add_cascade(label="Ocultar por…", menu=hide_menu)
        self.menu.add_separator()
        self.menu.add_command(label="Salir", command=self._quit)

        self.root.deiconify()
        self.root.update_idletasks()
        x, y = self._visible_pos()
        self.root.geometry(f"+{x}+{y}")
        try:
            apply_liquid_glass(self.root)
        except Exception:
            pass
        self.fetch_async()
        self._schedule_fetch()
        self._tick()

    # drag (a press that never moves >5 px counts as a click)
    def _drag_start(self, e):
        self._ox = e.x_root - self.root.winfo_x()
        self._oy = e.y_root - self.root.winfo_y()
        self._press = (e.x_root, e.y_root)
        self._moved = False

    def _drag_move(self, e):
        if (abs(e.x_root - self._press[0]) > 5
                or abs(e.y_root - self._press[1]) > 5):
            self._moved = True
        if self._moved:
            self.root.geometry(f"+{e.x_root - self._ox}+{e.y_root - self._oy}")

    def _drag_end(self, _e):
        if self._moved:
            self.cfg["x"], self.cfg["y"] = (self.root.winfo_x(),
                                            self.root.winfo_y())
            save_config(self.cfg)
        else:
            self._open_event()

    def _open_event(self):
        """Open the meeting link (current event first), else the calendar."""
        for ev in (self.current_event, self.next_event):
            if ev and ev[3]:
                webbrowser.open(ev[3])
                return
        webbrowser.open("https://calendar.google.com")

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    # sources
    def _connect_google(self):
        creds = load_client_secret()
        if not creds:
            messagebox.showinfo(
                "Conectar cuenta Google",
                "Falta el archivo client_secret.json en la carpeta del "
                "widget.\n\n1. console.cloud.google.com → crea un proyecto\n"
                "2. Habilita 'Google Calendar API'\n"
                "3. Pantalla de consentimiento OAuth → External → agrega tu "
                "correo como test user\n"
                "4. Credenciales → OAuth client ID → Desktop app → descarga "
                "el JSON\n"
                f"5. Guárdalo como:\n{CLIENT_SECRET_PATH}",
                parent=self.root)
            return

        def run():
            try:
                tok = oauth_authorize(*creds)
                self.cfg["google_refresh_token"] = tok["refresh_token"]
                save_config(self.cfg)
                self._fetch()
            except Exception as exc:
                self.error = f"OAuth: {exc}"

        threading.Thread(target=run, daemon=True).start()

    def _add_url(self):
        url = simpledialog.askstring(
            "Next Event Widget",
            f"URL ICS del calendario a agregar "
            f"(hay {len(self.cfg['ics_urls'])} conectados):",
            parent=self.root)
        if url and url.strip().lower().startswith("http"):
            self.cfg["ics_urls"].append(url.strip())
            save_config(self.cfg)
            self.fetch_async()

    def _clear_urls(self):
        if messagebox.askyesno(
                "Next Event Widget",
                f"¿Quitar los {len(self.cfg['ics_urls'])} calendarios ICS "
                "conectados?", parent=self.root):
            self.cfg["ics_urls"] = []
            save_config(self.cfg)
            self.fetch_async()

    def _quit(self):
        self.root.destroy()

    # data
    def fetch_async(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        events = []
        errors = []
        creds = load_client_secret()
        rtok = self.cfg.get("google_refresh_token")
        if creds and rtok:
            try:
                events += google_events(rtok, *creds)
            except Exception as exc:
                _g_cache["access"] = None  # maybe stale; re-auth next try
                errors.append(f"Google: {exc}")
        for url in self.cfg["ics_urls"]:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "NextEventWidget"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    text = r.read().decode("utf-8", "replace")
                events += parse_events(text)
            except Exception as exc:
                errors.append(str(exc))
        now = datetime.now().astimezone()
        ongoing = [e for e in events if e[1] <= now < e[2]]
        future = [e for e in events if e[1] > now]
        if errors and not events and not self.next_event:
            self.error = errors[0]
        else:
            # ongoing: the one that started most recently
            self.current_event = max(ongoing, key=lambda e: e[1],
                                     default=None)
            self.upcoming = sorted(future, key=lambda e: e[1])
            self.next_event = self.upcoming[0] if self.upcoming else None
            self.error = None

    def _schedule_fetch(self):
        self.root.after(FETCH_INTERVAL_MS, lambda: (self.fetch_async(),
                                                    self._schedule_fetch()))

    def _save_view_mode(self):
        self.cfg["view_mode"] = self.view_mode.get()
        save_config(self.cfg)

    def _show_stack(self, key, items):
        """Show exactly `items`, in order, dropping whatever else was up.

        Re-packing only when the set changes keeps the card from flickering
        every tick.
        """
        if key == self._stack_key:
            return
        self._stack_key = key
        for w in self._blocks:
            w.pack_forget()
        for w, kw in items:
            w.pack(**kw)

    def _draw_big_ring(self, pct, label, px):
        """Draw the ring at `px`, so the focus view fits whatever height."""
        c = self.big_ring
        c.config(width=px, height=px)
        c.delete("all")
        stroke = max(3, px // 13)
        m = stroke // 2 + 2                       # inset so the arc fits
        c.delete("all")
        c.create_oval(m, m, px - m, px - m, outline="#3a3a3c", width=stroke)
        if pct > 0:
            c.create_arc(m, m, px - m, px - m, start=90,
                         extent=-359.9 * pct, style="arc",
                         outline="#30d158", width=stroke)
        fs = max(8, int(px / (4.6 if len(label) <= 5 else 5.4)))
        c.create_text(px / 2, px / 2, text=label, fill=FG,
                      font=("Segoe UI Variable Display Semib", fs, "bold"))

    def _layout_normal(self, inner, ongoing):
        """Pick the lines that fit, most important first. Returns used px."""
        items = []
        used = H_COUNT
        show_title = inner >= H_COUNT + H_TITLE
        show_time = inner >= H_COUNT + H_TITLE + H_TIME
        show_now = ongoing and inner >= H_COUNT + H_TITLE + H_TIME + H_NOW
        if show_now:
            items.append((self.now_row, dict(anchor="w", pady=(0, 3))))
            used += H_NOW
        if show_title:
            items.append((self.title_lbl, dict(anchor="w")))
            used += H_TITLE
        if show_time:
            items.append((self.time_lbl, dict(anchor="w")))
            used += H_TIME
        items.append((self.count_lbl, dict(anchor="w", pady=(3, 0))))
        self._show_stack(("n", show_now, show_title, show_time), items)
        return used

    def _layout_focus(self, inner, width):
        """Give the ring every pixel the text lines don't need."""
        if inner >= H_RING_MIN + H_FTITLE + H_NEXT:
            text_h = H_FTITLE + H_NEXT
        elif inner >= H_RING_MIN + H_FTITLE:
            text_h = H_FTITLE
        else:
            text_h = 0
        ring = max(H_RING_MIN, min(MAX_RING, inner - text_h, width - 46))
        self._show_stack(("f", text_h), [(self.focus_box, {})])
        if text_h != self._focus_text:
            self._focus_text = text_h
            self.f_title.pack_forget()
            self.f_next.pack_forget()
            if text_h >= H_FTITLE:
                self.f_title.pack(pady=(7, 0))
            if text_h > H_FTITLE:
                self.f_next.pack(pady=(1, 0))
        return ring, ring + text_h

    # resize (drag the corner grip)
    def _resize_start(self, e):
        self._rs = (e.x_root, e.y_root,
                    self.root.winfo_width(), self.root.winfo_height())
        return "break"

    def _resize_move(self, e):
        w = max(MIN_W, self._rs[2] + e.x_root - self._rs[0])
        h = max(MIN_H, self._rs[3] + e.y_root - self._rs[1])
        self.root.geometry(f"{w}x{h}")
        return "break"

    def _resize_end(self, _e):
        self.cfg["w"] = self.root.winfo_width()
        self.cfg["h"] = self.root.winfo_height()
        save_config(self.cfg)
        try:
            apply_liquid_glass(self.root)  # recompose the enlarged surface
        except Exception:
            pass
        return "break"

    def _fit_text(self, text, extra=0):
        """Trim to what the current width can show (~7 px per character)."""
        room = max(8, (self.root.winfo_width() - 46 - extra) // 7)
        return text if len(text) <= room else text[:room - 1] + "…"

    def _update_agenda(self, now, used_h, skip=1):
        """Fill the agenda with as many upcoming events as the height allows.

        `skip` drops the events already shown above, so the list continues
        the schedule instead of repeating the next event.
        """
        free = self.root.winfo_height() - used_h
        rows = max(0, min(MAX_LIST_ROWS, (free - AGENDA_BASE) // ROW_H))
        events = [e for e in self.upcoming if e[1] > now][skip:skip + rows]
        if not events:
            self.list_box.pack_forget()
            self.stats_lbl.pack_forget()
            return
        # one stamp column for the whole list, so the titles line up
        same_day = all(e[1].date() == now.date() for e in events)
        col = 5 if same_day else 12
        for i, (row, stamp_lbl, name_lbl) in enumerate(self.list_rows):
            if i < len(events):
                title, start = events[i][0], events[i][1]
                stamp = (f"{start:%H:%M}" if same_day
                         else f"{start:%a %d} {start:%H:%M}")
                stamp_lbl.config(text=stamp, width=col)
                name_lbl.config(text=self._fit_text(title, col * 7 + 20))
                row.pack(anchor="w", fill="x")
            else:
                row.pack_forget()
        self.list_box.pack(anchor="w", fill="x", pady=(9, 0))
        # day summary when there is still room under the list
        if free - AGENDA_BASE - len(events) * ROW_H >= STATS_H:
            today = [e for e in self.upcoming
                     if e[1].date() == now.date() and e[1] > now]
            busy = sum((e[2] - e[1]).total_seconds() for e in today) / 3600
            if today:
                txt = (f"{len(today)} evento{'s' if len(today) > 1 else ''} "
                       f"hoy · {busy:.1f} h")
            else:
                txt = "nada más por hoy"
            self.stats_lbl.config(text=txt)
            self.stats_lbl.pack(anchor="w", pady=(8, 0))
        else:
            self.stats_lbl.pack_forget()

    # Hiding parks the window off-screen instead of withdrawing it:
    # withdraw/deiconify drops the acrylic surface and the topmost z-order,
    # so the card came back half-painted until it was moved by hand.
    def _hide_window(self):
        if not self._is_hidden:
            self._is_hidden = True
            self.root.geometry("+-4000+-4000")

    def _visible_pos(self):
        """Saved position, clamped so the card always lands on screen.

        A monitor change, a resolution change or a DPI change can leave the
        stored coordinates outside the desktop, making the widget invisible
        with no way to drag it back.
        """
        x, y = self.cfg.get("x", 60), self.cfg.get("y", 60)
        return self._clamp(x, y)

    def _clamp(self, x, y):
        self.root.update_idletasks()
        # requested size, not current: the card is still growing to fit its
        # text while the window is being placed
        # the window carries an explicit size, so its real size wins; the
        # requested one only covers the moment before it is mapped
        w = self.root.winfo_width() if self.root.winfo_width() > 1 else \
            max(self.root.winfo_reqwidth(), MIN_W)
        h = self.root.winfo_height() if self.root.winfo_height() > 1 else \
            max(self.root.winfo_reqheight(), MIN_H)
        max_x = self.root.winfo_screenwidth() - w
        max_y = self.root.winfo_screenheight() - h
        return max(0, min(x, max_x)), max(0, min(y, max_y))

    def _keep_on_screen(self):
        """Nudge the card back if it ends up off the desktop.

        Covers the window growing after placement, resolution changes and
        display-scaling changes.
        """
        if self._is_hidden:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        cx, cy = self._clamp(x, y)
        if (cx, cy) != (x, y):
            self.root.geometry(f"+{cx}+{cy}")

    def _show_window(self):
        self._is_hidden = False
        x, y = self._visible_pos()
        self.root.geometry(f"+{x}+{y}")
        self.root.attributes("-topmost", False)
        self.root.attributes("-topmost", True)  # force back to the front
        self.root.lift()

    def _hide_for(self, minutes):
        self._hidden_until = datetime.now() + timedelta(minutes=minutes)
        self._hide_window()

    def _check_fullscreen(self):
        try:
            fs = fullscreen_app_active()
        except Exception:
            fs = False
        if fs and not self._hidden_fs:
            self._hidden_fs = True
            self._hide_window()
        elif not fs and self._hidden_fs:
            self._hidden_fs = False
            if not self._hidden_until:
                self._show_window()

    # ui tick
    def _tick(self):
        now = datetime.now().astimezone()
        # a second launch of widget.pyw drops show.flag: always reappear,
        # whatever the reason it was hidden (this is the user's escape hatch)
        if SHOW_FLAG.exists():
            try:
                SHOW_FLAG.unlink()
            except OSError:
                pass
            self._hidden_until = None
            self._hidden_fs = False
            self._show_window()
        if self._hidden_until and datetime.now() >= self._hidden_until:
            self._hidden_until = None
            if not self._hidden_fs:
                self._show_window()
        self._check_fullscreen()
        self._keep_on_screen()

        # toast 10 min before the next event (once per event)
        if self.next_event:
            key = (self.next_event[0], self.next_event[1].isoformat())
            secs = (self.next_event[1] - now).total_seconds()
            if 0 < secs <= NOTIFY_BEFORE_S and key not in self.notified:
                self.notified.add(key)
                mins = max(1, int(secs // 60))
                try:
                    toast(self.next_event[0],
                          f"Empieza en {mins} min "
                          f"({self.next_event[1]:%H:%M})")
                except Exception:
                    pass

        if self.current_event and now >= self.current_event[2]:
            self.current_event = None  # just ended
            self.fetch_async()
        # focus view whenever something is in progress (or forced), and the
        # normal view otherwise
        mode = self.view_mode.get()
        self._focus_view = (bool(self.current_event)
                            and mode in ("auto", "focus"))
        inner = self.root.winfo_height() - PAD

        if self._focus_view:
            c_title, c_start, c_end = self.current_event[:3]
            total = (c_end - c_start).total_seconds() or 1
            pct = min(1.0, max(0.0, (now - c_start).total_seconds() / total))
            left = max(0, int((c_end - now).total_seconds() // 60))
            left_txt = (f"{left // 60}h {left % 60}m" if left >= 60
                        else f"{left}m")
            ring_px, used = self._layout_focus(inner,
                                               self.root.winfo_width())
            self._draw_big_ring(pct, left_txt, ring_px)
            self.f_title.config(text=self._fit_text(c_title))
            if self.next_event:
                n_title, n_start = self.next_event[0], self.next_event[1]
                stamp = (f"{n_start:%H:%M}" if n_start.date() == now.date()
                         else f"{n_start:%a %H:%M}")
                self.f_next.config(
                    text=f"{self._fit_text(n_title, 60)} · {stamp}")
            else:
                self.f_next.config(text="")
            self._update_agenda(now, used + PAD)
            self.root.after(TICK_MS, self._tick)
            return

        used = self._layout_normal(inner, bool(self.current_event))

        if self.current_event:
            c_title, c_start, c_end = self.current_event[:3]
            total = (c_end - c_start).total_seconds() or 1
            pct = min(1.0, max(0.0, (now - c_start).total_seconds() / total))
            self.ring.delete("all")
            self.ring.create_oval(3, 3, 15, 15, outline="#48484a", width=2)
            if pct > 0:
                self.ring.create_arc(3, 3, 15, 15, start=90,
                                     extent=-359.9 * pct, style="arc",
                                     outline="#30d158", width=2)
            self.now_lbl.config(text=self._fit_text(c_title, 30))

        if self.next_event:
            title, start = self.next_event[0], self.next_event[1]
            delta = start - now
            if delta.total_seconds() <= 0:
                self.count_lbl.config(text="¡Ahora!", fg=RED)
                if delta.total_seconds() < -60:
                    self.fetch_async()  # it started; promote to "now" line
            else:
                s = int(delta.total_seconds())
                h, rem = divmod(s, 3600)
                m = rem // 60
                if h >= 24:
                    txt = f"{h // 24}d {h % 24}h"
                elif h:
                    txt = f"{h}h {m}m"
                elif m:
                    txt = f"{m} min"
                else:
                    txt = "< 1 min"
                color = RED if s < 300 else AMBER if s < 900 else GREEN
                self.count_lbl.config(text=txt, fg=color)
            self.title_lbl.config(text=self._fit_text(title))
            self.time_lbl.config(text=start.strftime("%a %d %b · %H:%M"))
        elif self.error:
            self.title_lbl.config(text="Error de conexión")
            self.time_lbl.config(text=self._fit_text(str(self.error)))
            self.count_lbl.config(text="—", fg=DIM)
        else:
            self.title_lbl.config(text="Sin eventos próximos")
            self.time_lbl.config(text=f"(próximos {LOOKAHEAD_DAYS} días)")
            self.count_lbl.config(text="—", fg=DIM)
        self._update_agenda(now, used + PAD)
        self.root.after(TICK_MS, self._tick)


if __name__ == "__main__":
    # single instance: a second launch just tells the first one to show
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW(None, False, "NextEventWidgetMutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        SHOW_FLAG.touch()
        raise SystemExit
    Widget().root.mainloop()
