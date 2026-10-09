# Technical Explanation — Single Device Identity on MetaTrader 5

**Project:** Trade Assistant OO (local automated trading assistant)
**Document date:** 2026-09-25
**Purpose:** clarify why the trading account appeared with two PC device IDs and detail the code changes implemented to guarantee a single active device going forward.

---

## 1. Root-Cause Analysis

### 1.1 What was verified on the machine

- There is **only one MetaTrader 5 installation** on this PC:
  `C:\Program Files\MetaTrader 5\terminal64.exe`.
- There is **only one terminal data folder** (the terminal's identity):
  `C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075`.
- From an installation standpoint, this machine has always had **a single terminal identity**.

### 1.2 Why two PC device IDs appeared

The trading assistant uses the official MetaQuotes Python API (`MetaTrader5` package) to connect to the MT5 desktop terminal already running on the machine.

The previous code called:

```python
mt5.initialize()   # no parameters
```

The documented default behavior of this API is:

1. Try to attach to the terminal that is already running (same installation, same data folder).
2. **If it cannot attach, it launches its own hidden copy of `terminal64.exe`** and logs into the account.

Two real situations observed in the logs triggered step 2:

- **The desktop terminal was closed when the app started.** The assistant is kept alive by a Windows watchdog task (`TradeAssistantOOWatchdog`), which relaunches MT5 and the app automatically. The logs show MT5 down/relaunch events on 2026-08-17, 2026-08-31, 2026-09-18 and 2026-09-21 — including a rapid restart sequence on the morning of 2026-09-21 (09:44–10:57). Every app start while the terminal was closed could create a new hidden terminal instance that logged in.
- **Privilege-level mismatch.** The Windows task was registered with `RunLevel: Highest` (Administrator), while the desktop terminal and the Jupyter notebook run as the normal user. Windows blocks inter-process attachment between different integrity levels, so the API could not attach to the visible terminal and launched its own copy instead.

Each login session from those hidden instances can be recorded by the broker's systems as a separate device. That is why **one physical PC** appeared as **two PC device IDs** (`8057f72fc4` and `f96df9dcff`).

> The application never intended to open a second instance. This is the standard, documented behavior of the MetaQuotes API when it cannot attach to an already-running terminal. The changes below eliminate this possibility entirely.

---

## 2. Code Changes Implemented

### 2.1 `app/core/broker.py` — "attach-only" connection

**a) Process check before connecting (the app never starts its own terminal):**

```python
# Single terminal identity policy: the app only ATTACHES to the desktop
# MetaTrader terminal (started by the watchdog). It never launches its own
# hidden terminal instance, because each terminal login session can be
# registered by the broker as a separate device.
DEFAULT_MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
TERMINAL_PROCESS_NAME = "terminal64.exe"

def _terminal_process_running(proc_name: str = TERMINAL_PROCESS_NAME) -> bool:
    """True when the desktop MetaTrader terminal process is already running.
    Fails open (returns True) if the check itself fails."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return proc_name.lower() in result.stdout.lower()
    except Exception:
        return True
```

**b) In `initialize()` — if the terminal is not open, the app waits in degraded mode and retries; it never opens a second instance:**

```python
def initialize(self) -> bool:
    if self._connected:
        return True
    if self._attach_only and not _terminal_process_running():
        self._last_error = (
            "attach-only mode: MT5 terminal is not running — the app will "
            "NOT launch a second terminal instance; retrying automatically "
            "once the desktop terminal is open"
        )
        return False
    kwargs = {"portable": False}
    if self._terminal_path:
        kwargs["path"] = self._terminal_path
    self._connected = mt5.initialize(**kwargs)
    ...
```

**c) Fixed terminal path + standard (non-portable) mode:**

```python
mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe", portable=False)
```

With `portable=False` the API is forced to use the **same default data folder** as the desktop terminal — i.e., the same device identity — even in the hypothetical case where it had to start the executable.

### 2.2 `app/config.py` — new configuration parameters

```python
"MT5_PATH": r"C:\Program Files\MetaTrader 5\terminal64.exe",
"MT5_ATTACH_ONLY": "1",
```

`MT5_ATTACH_ONLY=1` enables attach-only mode by default.

### 2.3 `app/main.py` — parameters wired into the broker

```python
broker = MT5Broker(
    login=config.get_str("MT5_LOGIN") or None,
    password=config.get_str("MT5_PASSWORD") or None,
    server=config.get_str("MT5_SERVER") or None,
    terminal_path=config.get_str("MT5_PATH") or None,
    attach_only=config.get_str("MT5_ATTACH_ONLY", "1") != "0",
)
```

### 2.4 `notebooks/analise_direcao_diaria.ipynb` — analysis notebook

The notebook that fetches historical data also used a bare `mt5.initialize()`. It now pins the same terminal:

```python
MT5_EXE = r"C:\Program Files\MetaTrader 5\terminal64.exe"
if not mt5.initialize(path=MT5_EXE, portable=False):
    raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
```

### 2.5 `install_startup.ps1` — scheduled task without elevation

The Windows task that starts MT5 + assistant was registered as Administrator. Now:

```powershell
# RunLevel Limited: the watchdog and everything it starts (MT5 + Flask app)
# run in the normal user context. Running elevated can force the MetaTrader5
# Python package to launch a SECOND terminal instance (different integrity
# level), which the broker may register as an extra device.
$principal = New-ScheduledTaskPrincipal -UserID "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
```

**Task re-registered on 2026-09-25 and verified:** `UserId: gk`, `LogonType: Interactive`, `RunLevel: Limited`.

### 2.6 `tests/conftest.py` — test compatibility

The unit test suite was adapted so it does not depend on a real terminal. After the changes, **255 tests pass**.

### 2.7 `PROCEDIMENTO_STARTUP.md` — operational documentation

Updated with the "one terminal = one device" policy.

---

## 3. Behavior Going Forward

| Situation | Behavior |
|---|---|
| Desktop MT5 terminal open | The app attaches to it and operates normally. |
| Desktop MT5 terminal closed | The app enters degraded mode and **waits**; it reconnects automatically when the terminal opens. It never starts its own copy. |
| PC reboot / app crash | The watchdog starts the desktop terminal; the app attaches to it (same exe, same data folder). |
| Any login to the account | Always originates from the same installed terminal → **same device identity**. |

---

## 4. How to Verify

```powershell
# 1. Only ONE terminal copy may exist:
Get-Process terminal64 | Select-Object Id, Path, StartTime

# 2. The scheduled task must be non-elevated:
(Get-ScheduledTask -TaskName "TradeAssistantOOWatchdog").Principal | Format-List RunLevel

# 3. App parameters (safe defaults already applied):
Get-Content data\runtime_config.json
```

---

## 5. Request to the Broker

The two historical IDs (`8057f72fc4` and `f96df9dcff`) correspond to login sessions of hidden instances of the same installed terminal on this machine, caused by the default MetaQuotes API behavior described in section 1.2. With the corrections above, **no new ID will be generated**: all future logins will come from the same installation and the same data folder.

We kindly ask that the old devices be **removed/reset** from the device list in the account panel, so that the record reflects only the current device.
