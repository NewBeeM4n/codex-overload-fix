# No console window — and why the obvious fix doesn't work

If you run anything long-lived on Windows — a watchdog, a relay, a scheduled task — you will eventually get this bug report:

> Every so often a blue console window pops up for a split second and disappears. It's very disruptive when I'm playing a game in fullscreen.

It is worth taking seriously. It is not cosmetic: a window that steals focus can drop a fullscreen game out of exclusive mode, and it happens on a timer, so it looks random.

The instinctive fix is to add `-WindowStyle Hidden` to the PowerShell command line. **It does not work.** This document explains why, so you don't spend an evening on it.

---

## Reproduce it yourself

The repository ships a probe that measures it on your own machine:

```powershell
python tools\check_console_flash.py
```

It launches three throwaway launchers and samples every 5 ms for visible `ConsoleWindowClass` windows.

Measured here, Windows 10 Pro 19045, 2026-10-26:

| how the child was launched | first visible | time on screen |
|---|---|---|
| control — plain `powershell.exe`, normal window | +295 ms | **1190 ms** |
| `powershell.exe -WindowStyle Hidden` | +303 ms | **253 ms** |
| `wscript.exe` → `Sh.Run(cmd, 0, False)` (SW_HIDE) | — | **0 ms, never visible** |

Two things in that table matter:

1. **`-WindowStyle Hidden` shortens the flash from 1190 ms to 253 ms but does not remove it.** 253 ms is still a visible flash. If anything this is worse than no fix at all, because now you believe it is fixed.
2. **Both cases first became visible at essentially the same moment — +295 ms vs +303 ms.** The flag has no effect on *when* the window appears. That is the whole mechanism, and it tells you the fix has to happen somewhere else.

---

## Why it happens

`powershell.exe` is a **console-subsystem** program. When a task host or a GUI process creates it with `CreateProcess` and does not pass `CREATE_NO_WINDOW` or `DETACHED_PROCESS`, the operating system does this, in order:

1. `CreateProcess` returns with a new process that has no console.
2. Because the image is console-subsystem, the OS **allocates a console** and `conhost.exe` builds the window for it. **The window is already on screen.**
3. `powershell.exe` starts up and parses its own arguments — only now does it see `-WindowStyle Hidden`, and calls `ShowWindow(SW_HIDE)`.

The window exists between step 2 and step 3. No argument to `powershell.exe` can close that gap, because the argument is not read until after it has already opened. The timings above (+295 vs +303 ms to first visible) are step 2, and they are independent of the flag.

`-WindowStyle` is a *request made after the fact*. What you need is a decision made *at process creation*.

---

## What works

**Hide it at creation time.** Two ways, both do the same thing:

### 1. `wscript.exe` + `SW_HIDE` — what this repository ships

```vbs
' watch-relay.vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "python ""...\relay.py"" --port 8787", 0, True
```

The second argument to `WScript.Shell.Run` is the window style, and `0` is `SW_HIDE`. wscript passes it in the `STARTUPINFO` block, so the console is created hidden and **never becomes visible, not for a single frame**. `wscript.exe` is itself a GUI-subsystem program, so it has no console of its own and shows nothing either.

`scripts/install.ps1` writes that `wscript.exe "<state>\watch-relay.vbs"` command line in two places: as the `HKCU\...\Run` value, and as the argument to `Win32_Process.Create` when it starts the watchdog right after installing. Both go through `wscript.exe`; neither ever starts PowerShell with a window style. That is the entire trick.

### 2. `CreateNoWindow` / `CREATE_NO_WINDOW`

If you are launching from .NET or Python instead of VBS:

```csharp
var psi = new ProcessStartInfo(exe, args) {
    UseShellExecute = false,   // required, or CreateNoWindow is ignored
    CreateNoWindow  = true,    // == CREATE_NO_WINDOW: no console is allocated at all
};
```

With `CREATE_NO_WINDOW` there is no console and therefore no window at any point — not even a hidden one. It is the more precise fix, but **Task Scheduler cannot set this flag**, which is why scheduled/background launches here go through VBS.

---

## The trap

Task Scheduler is the usual place people reach for first, and it is exactly where `-WindowStyle Hidden` gets typed:

```powershell
# WRONG - still flashes
$taskArgs = '-WindowStyle Hidden -NoProfile -File watch.ps1'
```

The action is `powershell.exe`, so the OS allocates a console before PowerShell reads that argument. **Same failure, just moved into the scheduler.** If you must use Task Scheduler, make the action `wscript.exe <something>.vbs` instead, and keep PowerShell out of the action entirely.

---

## Rule of thumb

> On Windows, **any** background script started as `powershell.exe -WindowStyle Hidden` — by Task Scheduler, by a Run key, by a service wrapper — will flash. Route it through `wscript.exe` with `SW_HIDE`, or use `CREATE_NO_WINDOW`. There is no command-line flag that avoids this.

This applies to every long-lived helper in this project. If you add a new background component, launch it the same way.

---

## The second trap: your caller's Job Object

Hiding the window is not enough to keep a background helper alive. If the process that runs the installer holds a **Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, every process the installer starts with `Start-Process` joins that job, and the whole tree dies the moment the caller closes it. Packaged (MSIX) hosts and some editors do exactly this.

`Start-Process` therefore looks like it worked and leaves a dead port behind. The fix is not Task Scheduler — it is to leave the caller's process tree:

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = 'wscript.exe "...\watch-relay.vbs"'
}
```

`Win32_Process.Create` is carried out by `WmiPrvSE.exe`, a service outside your process tree, so the watchdog it creates is not a job member and survives the caller. It **still starts `wscript.exe`**, so the no-console-window rule above is untouched. A second benefit on packaged hosts: a process created this way carries no package identity, so it resolves the real `%LOCALAPPDATA%` rather than a virtualised `LocalCache` view of it.

The suite has a regression test for this (`test_the_relay_outlives_a_caller_that_closes_its_job`): it runs the real `install.ps1` inside a kill-on-close job, closes the job, and fails if the relay stops listening.

---

## Verifying a fix

Do not verify by adding the flag and watching for a while. You will probably not catch a 250 ms window, and you will conclude it is fixed. Run `tools\check_console_flash.py` before and after — it samples at 5 ms and reports the actual on-screen duration.

Related: the same measurement approach caught a second, unrelated source of window flashes on the test machine, which is why the probe compares against a control case rather than trusting a single run.