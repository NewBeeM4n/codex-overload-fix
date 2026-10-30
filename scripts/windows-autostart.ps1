# Access the real user's Run key through the WMI registry provider. In a
# packaged process, direct HKCU writes may be private to the package.
function Set-RealRelayAutostart([string]$Name, [string]$Value, [bool]$Remove = $false) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $key = "$sid\Software\Microsoft\Windows\CurrentVersion\Run"
    $base = @{ hDefKey = [uint32]2147483651; sSubKeyName = $key; sValueName = $Name }
    if ($Remove) {
        $result = Invoke-CimMethod -Namespace root/default -ClassName StdRegProv -MethodName DeleteValue -Arguments $base -ErrorAction Stop
        if ($result.ReturnValue -notin @(0, 2)) { throw "Cannot remove autostart (registry error $($result.ReturnValue))" }
        return
    }
    $write = @{} + $base
    $write.sValue = $Value
    $result = Invoke-CimMethod -Namespace root/default -ClassName StdRegProv -MethodName SetStringValue -Arguments $write -ErrorAction Stop
    if ($result.ReturnValue -ne 0) { throw "Cannot register autostart (registry error $($result.ReturnValue))" }
    $read = Invoke-CimMethod -Namespace root/default -ClassName StdRegProv -MethodName GetStringValue -Arguments $base -ErrorAction Stop
    if ($read.ReturnValue -ne 0 -or $read.sValue -cne $Value) { throw 'Could not verify the real autostart entry' }
}
