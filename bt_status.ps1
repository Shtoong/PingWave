# bt_status.ps1 — Bluetooth status polling for PingWave
# Returns JSON: {"bt_on": bool, "connected": bool, "battery": int}
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File bt_status.ps1 -DeviceName "OpenRun Pro 2"

param(
    [string]$DeviceName = ""
)

$r = @{ bt_on = $false; connected = $false; battery = -1 }

# --- BT Radio state via WinRT ---
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType = WindowsRuntime] | Out-Null

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    function AwaitResult($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(5000) | Out-Null
        $netTask.Result
    }

    $radios = AwaitResult ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) `
        ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])

    $btRadio = $radios | Where-Object { $_.Kind -eq 3 }  # 3 = Bluetooth
    $r.bt_on = ($null -ne $btRadio -and $btRadio.State -eq 1)  # 1 = On
} catch {
    $r.bt_on = $true  # can't determine, assume on
}

# --- Device connection via WinRT AEP ---
if ($r.bt_on -and $DeviceName -ne "") {
    try {
        [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime] | Out-Null

        $selector = "System.Devices.Aep.ProtocolId:=""{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"""
        $props = [System.Collections.Generic.List[string]]::new()
        $props.Add("System.Devices.Aep.IsConnected")

        $devices = AwaitResult `
            ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync(
                $selector, $props,
                [Windows.Devices.Enumeration.DeviceInformationKind]::AssociationEndpoint)) `
            ([Windows.Devices.Enumeration.DeviceInformationCollection])

        foreach ($d in $devices) {
            if ($d.Name -like "*$DeviceName*") {
                $isConn = $d.Properties["System.Devices.Aep.IsConnected"]
                if ($isConn -eq $true) {
                    $r.connected = $true
                    break
                }
            }
        }
    } catch {}

    # --- Battery: try PnP property (works on some devices) ---
    if ($r.connected) {
        try {
            $btDev = Get-PnpDevice -Class Bluetooth -EA SilentlyContinue |
                Where-Object { $_.FriendlyName -like "*$DeviceName*" } |
                Select-Object -First 1
            if ($btDev) {
                $p = Get-PnpDeviceProperty -InstanceId $btDev.InstanceId -EA SilentlyContinue
                $b = $p | Where-Object { $_.KeyName -eq '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2' }
                if ($b -and $b.Data -ne $null -and $b.Type -ne 'Empty') {
                    $r.battery = [int]$b.Data
                }
            }
        } catch {}
    }
}

$r | ConvertTo-Json -Compress
