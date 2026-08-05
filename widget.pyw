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
COMPACT_AFTER_S = 3 * 3600         # pill mode when next event > 3 h away
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
    """True when the foreground window covers its whole monitor."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False  # the desktop / taskbar
    style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
    if style & 0x00C00000:  # WS_CAPTION: normal (maximized) window
        return False

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    m, wk = mi.rcMonitor, mi.rcWork
    covers_monitor = (rect.l <= m.l and rect.t <= m.t
                      and rect.r >= m.r and rect.b >= m.b)
    if not covers_monitor:
        return False
    # A maximized borderless window also covers the monitor, so require it
    # to spill past the work area (i.e. over the taskbar) to count as
    # fullscreen. With an auto-hidden taskbar both rects match and any
    # monitor-covering window is treated as fullscreen.
    if (wk.l, wk.t, wk.r, wk.b) == (m.l, m.t, m.r, m.b):
        return True
    return (rect.l < wk.l or rect.t < wk.t
            or rect.r > wk.r or rect.b > wk.b)

# iOS dark-mode system palette
BG = "#1c1c1e"          # systemGray6 dark — tint under the acrylic blur
FG = "#ffffff"          # label
DIM = "#98989f"         # secondaryLabel over dark
AMBER = "#ff9f0a"       # systemOrange
RED = "#ff453a"         # systemRed
GREEN = "#0a84ff"       # systemBlue — countdown accent


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
        self.error = None
        self.notified = set()       # events already toast-notified
        self._compact = None        # current layout mode (None = unset)
        self._hidden_fs = False     # hidden because a fullscreen app is up
        self._hidden_until = None   # hidden by the user until this time
        self._is_hidden = False     # window currently parked off-screen

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        x, y = self.cfg.get("x", 60), self.cfg.get("y", 60)
        self.root.geometry(f"+{x}+{y}")

        frame = tk.Frame(self.root, bg=BG, padx=20, pady=13,
                         highlightthickness=1,
                         highlightbackground="#8a8a94")
        frame.pack()
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

        for w in (self.root, frame, self.now_row, self.ring, self.now_lbl,
                  self.title_lbl, self.time_lbl, self.count_lbl):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Actualizar ahora", command=self.fetch_async)
        self.menu.add_command(label="Conectar cuenta Google…",
                              command=self._connect_google)
        self.menu.add_command(label="Agregar calendario ICS…",
                              command=self._add_url)
        self.menu.add_command(label="Quitar calendarios ICS",
                              command=self._clear_urls)
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
            self.next_event = min(future, key=lambda e: e[1], default=None)
            self.error = None

    def _schedule_fetch(self):
        self.root.after(FETCH_INTERVAL_MS, lambda: (self.fetch_async(),
                                                    self._schedule_fetch()))

    def _set_compact(self, compact):
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self.title_lbl.pack_forget()
            self.time_lbl.pack_forget()
        else:
            self.title_lbl.pack(anchor="w", before=self.count_lbl)
            self.time_lbl.pack(anchor="w", before=self.count_lbl)

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
        self.root.update_idletasks()
        w = self.root.winfo_width() or 200
        h = self.root.winfo_height() or 100
        max_x = self.root.winfo_screenwidth() - w
        max_y = self.root.winfo_screenheight() - h
        return max(0, min(x, max_x)), max(0, min(y, max_y))

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

        # compact pill when nothing is happening and the next event is far
        far = (self.next_event
               and (self.next_event[1] - now).total_seconds() > COMPACT_AFTER_S)
        self._set_compact(bool(far) and not self.current_event
                          and not self.error)
        # "happening now" line
        if self.current_event and now >= self.current_event[2]:
            self.current_event = None  # just ended
            self.fetch_async()
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
            self.now_lbl.config(text=c_title[:30])
            self.now_row.pack(anchor="w", before=self.title_lbl, pady=(0, 3))
        else:
            self.now_row.pack_forget()

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
            self.title_lbl.config(text=title[:40])
            self.time_lbl.config(text=start.strftime("%a %d %b · %H:%M"))
        elif self.error:
            self.title_lbl.config(text="Error de conexión")
            self.time_lbl.config(text=str(self.error)[:45])
            self.count_lbl.config(text="—", fg=DIM)
        else:
            self.title_lbl.config(text="Sin eventos próximos")
            self.time_lbl.config(text=f"(próximos {LOOKAHEAD_DAYS} días)")
            self.count_lbl.config(text="—", fg=DIM)
        self.root.after(TICK_MS, self._tick)


if __name__ == "__main__":
    # single instance: a second launch just tells the first one to show
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW(None, False, "NextEventWidgetMutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        SHOW_FLAG.touch()
        raise SystemExit
    Widget().root.mainloop()
