# Windows 无窗口启动：为什么「加个 -WindowStyle Hidden」没用

在 Windows 上跑任何长期驻留的东西 —— 看门狗、中继、计划任务 —— 迟早会收到这条反馈：

> 时不时弹出一个蓝色的命令窗口，闪一下就没了，全屏打游戏的时候特别难受。

这条反馈值得认真对待，不是玻璃心：**抢焦点的窗口会把全屏游戏踢出独占模式**，而且它是定时触发的，看起来像随机发作。

第一反应通常是在 PowerShell 命令行上加 `-WindowStyle Hidden`。**它不管用。** 本文解释为什么，省得你也花一晚上。

---

## 先在自己机器上复现

仓库自带一个探测工具：

```powershell
python tools\check_console_flash.py
```

它拉起三个一次性启动器，每 5 毫秒枚举一次可见的 `ConsoleWindowClass` 窗口。

本机实测（Windows 10 Pro 19045，2026-10-26）：

| 子进程的启动方式 | 首次可见 | 在屏幕上停留 |
|---|---|---|
| 对照组：普通 `powershell.exe`，不隐藏 | +295 ms | **1190 ms** |
| `powershell.exe -WindowStyle Hidden` | +303 ms | **253 ms** |
| `wscript.exe` → `Sh.Run(cmd, 0, False)`（SW_HIDE） | — | **0 ms，从未可见** |

这张表里有两个数才是关键：

1. **`-WindowStyle Hidden` 只是把闪屏从 1190 ms 缩短到 253 ms，并没有消除它。** 253 ms 依然肉眼可见。这甚至比不修更糟——因为你会以为修好了。
2. **两种情况首次可见的时刻几乎一样，+295 ms 对 +303 ms。** 这个参数根本不影响窗口**什么时候出现**。机制就在这句话里，也说明修复点不在这里。

---

## 为什么会这样

`powershell.exe` 是**控制台子系统**程序。当任务宿主或某个 GUI 进程用 `CreateProcess` 创建它，且没有传 `CREATE_NO_WINDOW` 或 `DETACHED_PROCESS` 时，操作系统的动作顺序是：

1. `CreateProcess` 返回，新进程没有控制台。
2. 因为镜像是控制台子系统，系统**分配控制台**，`conhost.exe` 为它建出窗口。**此时窗口已经在屏幕上了。**
3. `powershell.exe` 才开始启动，解析自己的命令行参数 —— 到这一步它才看到 `-WindowStyle Hidden`，然后调 `ShowWindow(SW_HIDE)`。

窗口存在于第 2 步和第 3 步之间。**任何传给 `powershell.exe` 的参数都填不上这个缝**，因为参数要等窗口开完才被读到。上表的 +295 / +303 ms 就是第 2 步，跟这个参数无关。

`-WindowStyle` 是**事后补救**。你需要的是**在创建进程那一刻就做好决定**。

---

## 什么才管用

**在创建时隐藏。** 两条路，效果一样：

### 1. `wscript.exe` + `SW_HIDE` —— 本仓库采用的方式

```vbs
' watch-relay.vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "python ""...\relay.py"" --port 8787", 0, True
```

`WScript.Shell.Run` 的第二个参数是窗口样式，`0` 就是 `SW_HIDE`。wscript 把它放进 `STARTUPINFO`，于是控制台**创建时就是隐藏的，一帧都不会显示**。而 `wscript.exe` 自己是 GUI 子系统程序，没有控制台，它自己也不会闪。

`scripts/install.ps1` 里这句 `wscript.exe "<state>\watch-relay.vbs"` 出现在两处：写进 `HKCU\...\Run` 注册表值，以及安装完立刻启动看门狗时交给 `Win32_Process.Create` 的命令行。两条路都经过 `wscript.exe`，都不会用窗口样式参数去启动 PowerShell。整个诀窍就这一句。

### 2. `CreateNoWindow` / `CREATE_NO_WINDOW`

如果是从 .NET 或 Python 里启动，而不是用 VBS：

```csharp
var psi = new ProcessStartInfo(exe, args) {
    UseShellExecute = false,   // 必须，否则 CreateNoWindow 会被忽略
    CreateNoWindow  = true,    // == CREATE_NO_WINDOW：根本不分配控制台
};
```

用 `CREATE_NO_WINDOW` 就压根没有控制台，也就没有窗口 —— 连隐藏的都没有。这个更精准，但**计划任务没法设这个标志**，所以本项目的后台启动一律走 VBS。

---

## 最容易踩的坑

计划任务通常是大家第一个想到的地方，也正是最容易敲出 `-WindowStyle Hidden` 的地方：

```powershell
# 错的 —— 照样闪
$taskArgs = '-WindowStyle Hidden -NoProfile -File watch.ps1'
```

动作是 `powershell.exe`，所以系统在 PowerShell 读到那个参数之前就已经把控制台建好了。**同样的失败，只是搬进了计划任务里。** 非要用计划任务，就把动作改成 `wscript.exe <某个脚本>.vbs`，别让 PowerShell 出现在动作里。

---

## 一条可以记住的规则

> 在 Windows 上，**凡是**用 `powershell.exe -WindowStyle Hidden` 启动的后台脚本 —— 不管是通过计划任务、Run 注册表项还是服务包装器 —— **都会闪一下**。走 `wscript.exe` + `SW_HIDE`，或者用 `CREATE_NO_WINDOW`。**没有任何命令行参数能绕开这件事。**

本项目里每个长期驻留的辅助进程都适用。以后加新的后台组件，按同样方式启动。

---

## 第二个坑：调用者的 Job Object

窗口藏住了，不代表后台进程就能活下来。如果执行安装器的那个进程持有带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 **Job Object**，那么安装器用 `Start-Process` 起来的每个进程都会加入这个 job，调用者一关闭 job，整棵进程树全灭。打包（MSIX）宿主和部分编辑器就是这么干的。

于是 `Start-Process` 看起来成功了，实际留下一个死端口。解法不是计划任务，而是**离开调用者的进程树**：

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = 'wscript.exe "...\watch-relay.vbs"'
}
```

`Win32_Process.Create` 由服务进程 `WmiPrvSE.exe` 实际执行，在你的进程树之外，所以它创建的看门狗不是 job 成员，能活过调用者退出。它**仍然启动的是 `wscript.exe`**，上面那条不闪窗的规则完全不受影响。在打包宿主上还有第二个好处：这样创建的进程不带包标识，因此读到的是真实的 `%LOCALAPPDATA%`，而不是被虚拟化到 `LocalCache` 的那份。

测试套件里有针对这一点的回归测试（`test_the_relay_outlives_a_caller_that_closes_its_job`）：把真实的 `install.ps1` 放进一个 kill-on-close 的 job 里跑，关掉 job，如果中继不再监听就算失败。

---

## 怎么验证修好了

**不要**用「加上参数然后观察一阵子」来验证。250 毫秒的窗口你大概率抓不到，然后就会得出"修好了"的结论。用 `tools\check_console_flash.py` 前后各跑一次 —— 它每 5 毫秒采样，直接报出真实的在屏时长。

顺带一提：正是这套测量方法在测试机上又抓到一个**另外的、无关的**闪窗来源，这也是为什么探测工具要带一个对照组，而不是只跑一次就下结论。