# Next Event Widget

A tiny always-on-top **Liquid Glass countdown widget for Windows 11** that shows how long until your next Google Calendar event — always visible, beautiful, and written in pure Python stdlib (no dependencies to install).

```
 ◌  Clase en curso            ← progress ring of the event happening now
 Matricula 2026-2             ← next event
 Tue 04 Aug · 10:00
 1h 39m                       ← live countdown (systemBlue → orange → red)
```

## Features

- **Liquid Glass look** — real Windows 11 acrylic blur-behind (DWM), rounded corners, iOS dark-mode system palette. Frameless, draggable, remembers its position.
- **All your calendars** — connects to the Google Calendar API with a one-time OAuth login and watches *every* calendar of your account. Extra ICS feed URLs can be added too.
- **"Happening now" ring** — during an event, an Apple-style progress ring fills as the event advances.
- **Live countdown** — to the soonest upcoming event; turns orange under 15 min, red under 5 min.
- **Toast notifications** — native Windows toast (with sound) 10 minutes before each event.
- **Click to join** — click the widget to open the event's Meet/Zoom/Teams link, or Google Calendar if there is none. Drag to move (a click that moves is a drag).
- **Compact pill** — when the next event is more than 3 h away, the widget collapses to just the countdown.
- **Fullscreen aware** — hides automatically while a fullscreen app (video, game, presentation) is in front.
- **Cheap** — 5-second UI ticks, 5-minute refreshes, OAuth token and calendar list cached. Single ~30 MB `pythonw` process.

## Requirements

- Windows 11 (Windows 10 works, minus the rounded corners)
- Python 3.10+ (stdlib only — nothing to `pip install`)

## Setup

### 1. Get Google API credentials (one time, ~10 min, free)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. Search **"Google Calendar API"** → Enable.
3. **OAuth consent screen** → External (or Internal on Workspace) → fill the app name and your email. If External/Testing, add your account under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.
5. Save it next to `widget.pyw` as **`client_secret.json`**.

### 2. Run and connect

```
pythonw widget.pyw
```

Right-click the widget → **"Conectar cuenta Google…"** → log in in the browser → done. The widget stores a refresh token in `config.json` and syncs silently from then on.

Alternatively (or additionally), right-click → **"Agregar calendario ICS…"** and paste any iCal/ICS URL (e.g. Google Calendar's *Secret address in iCal format*).

### 3. Start with Windows (optional)

Create a shortcut to `widget.pyw` in your Startup folder (`Win+R` → `shell:startup`), with `pythonw.exe` as the target:

```
pythonw.exe "C:\path\to\widget.pyw"
```

## Privacy

Everything runs and stays on your machine. `config.json` (your tokens and feed URLs) and `client_secret.json` are **git-ignored** — never commit them. Access is read-only (`calendar.readonly` scope).

## Right-click menu

| Item | Action |
|---|---|
| Actualizar ahora | Refresh immediately |
| Conectar cuenta Google… | Run the OAuth login |
| Agregar calendario ICS… | Add an ICS feed URL |
| Quitar calendarios ICS | Remove all ICS feeds |
| Salir | Quit |

---

Built with Claude Code.
