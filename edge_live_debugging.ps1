[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("enable", "approve", "watch", "disable")]
    [string]$Command,

    [Parameter(Mandatory = $true)]
    [string]$ProfileDisplayName,

    [int]$TimeoutSeconds = 30,
    [long]$WindowHandle = 0,
    [switch]$CloseWindow
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class EdgeNativeWindow {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern void SwitchToThisWindow(IntPtr hWnd, bool fUnknown);
}
"@

function Write-Result {
    param([hashtable]$Value)
    [Console]::WriteLine(($Value | ConvertTo-Json -Compress))
}

function Get-EdgeWindows {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windows = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $result = @()
    foreach ($window in $windows) {
        if ($window.Current.ClassName -notlike "Chrome_WidgetWin*") {
            continue
        }
        try {
            $process = Get-Process -Id $window.Current.ProcessId -ErrorAction Stop
        }
        catch {
            continue
        }
        if ($process.ProcessName -eq "msedge") {
            $result += $window
        }
    }
    return $result
}

function Find-ProfileWindow {
    param([long]$PreferredHandle = 0)

    $windows = @(Get-EdgeWindows)
    if ($PreferredHandle -gt 0) {
        foreach ($window in $windows) {
            if ($window.Current.NativeWindowHandle -eq $PreferredHandle) {
                return $window
            }
        }
    }

    $matching = @(
        $windows | Where-Object {
            $_.Current.Name -like "* - $ProfileDisplayName - Microsoft*Edge*"
        }
    )
    if ($matching.Count -eq 0) {
        $matching = @(
            $windows | Where-Object {
                $_.Current.Name -like "*Microsoft*Edge*"
            }
        )
    }
    if ($matching.Count -eq 0) {
        return $null
    }

    $inspect = @($matching | Where-Object { $_.Current.Name -like "Inspect with Edge Developer Tools*" })
    if ($inspect.Count -gt 0) {
        return $inspect[0]
    }
    $newTabs = @($matching | Where-Object { $_.Current.Name -like "New tab*" })
    if ($newTabs.Count -gt 0) {
        return $newTabs[-1]
    }
    return $matching[-1]
}

function Find-RemoteDebuggingCheckbox {
    param([System.Windows.Automation.AutomationElement]$Window)
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        "remote-debugging-enabled"
    )
    return $Window.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition
    )
}

function Test-RemoteDebuggingAllowButton {
    param(
        [System.Windows.Automation.AutomationElement]$Button,
        [System.Windows.Automation.AutomationElement]$Window
    )

    $signals = @($Window.Current.Name)
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $Button
    for ($depth = 0; $depth -lt 8 -and $null -ne $current; $depth++) {
        try {
            if ($current.Current.Name) {
                $signals += $current.Current.Name
            }
            if ($current.Current.AutomationId) {
                $signals += $current.Current.AutomationId
            }
            $current = $walker.GetParent($current)
        }
        catch {
            break
        }
    }
    return [bool](
        $signals | Where-Object {
            $_ -match "(?i)(remote[\s-]*debug|developer\s*tools|devtools|inspect)"
        }
    )
}

function Navigate-ToRemoteDebugging {
    param([System.Windows.Automation.AutomationElement]$Window)

    $handle = [IntPtr]$Window.Current.NativeWindowHandle
    $null = [EdgeNativeWindow]::ShowWindowAsync($handle, 9)
    $null = [EdgeNativeWindow]::SetForegroundWindow($handle)
    [EdgeNativeWindow]::SwitchToThisWindow($handle, $true)
    try {
        $Window.SetFocus()
    }
    catch {
        # A newly created Edge top-level element can reject UIA focus briefly.
    }
    $shell = New-Object -ComObject WScript.Shell
    $activated = $shell.AppActivate($Window.Current.ProcessId)
    if (-not $activated) {
        $activated = $shell.AppActivate($Window.Current.Name)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    do {
        if ([EdgeNativeWindow]::GetForegroundWindow() -eq $handle) {
            break
        }
        $null = [EdgeNativeWindow]::SetForegroundWindow($handle)
        [EdgeNativeWindow]::SwitchToThisWindow($handle, $true)
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    $shell.SendKeys("^l")
    Start-Sleep -Milliseconds 250
    $shell.SendKeys("edge://inspect/#remote-debugging")
    $shell.SendKeys("{ENTER}")
}

function Wait-ForProfileWindow {
    param([long]$PreferredHandle = 0)
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        $window = Find-ProfileWindow -PreferredHandle $PreferredHandle
        if ($null -ne $window) {
            return $window
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Wait-ForRemoteDebuggingCheckbox {
    param([System.Windows.Automation.AutomationElement]$Window)
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        try {
            $checkbox = Find-RemoteDebuggingCheckbox -Window $Window
            if ($null -ne $checkbox) {
                return $checkbox
            }
        }
        catch {
            $Window = Find-ProfileWindow -PreferredHandle $WindowHandle
            if ($null -eq $Window) {
                return $null
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

if ($Command -eq "approve" -or $Command -eq "watch") {
    $deadline = if ($Command -eq "watch") {
        [DateTime]::MaxValue
    }
    else {
        [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    }
    do {
        $candidateWindows = @()
        $preferred = Find-ProfileWindow -PreferredHandle $WindowHandle
        if ($null -ne $preferred) {
            $candidateWindows += $preferred
        }
        foreach ($candidate in @(Get-EdgeWindows)) {
            if (
                $candidate.Current.Name -like "* - $ProfileDisplayName - Microsoft*Edge*" -and
                ($null -eq $preferred -or
                    $candidate.Current.NativeWindowHandle -ne $preferred.Current.NativeWindowHandle)
            ) {
                $candidateWindows += $candidate
            }
        }
        foreach ($window in $candidateWindows) {
            $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                "Allow"
            )
            $buttons = $window.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                $nameCondition
            )
            foreach ($button in $buttons) {
                if (
                    $button.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
                    $button.Current.IsEnabled -and
                    -not $button.Current.IsOffscreen -and
                    (Test-RemoteDebuggingAllowButton -Button $button -Window $window)
                ) {
                    $invoke = $button.GetCurrentPattern(
                        [System.Windows.Automation.InvokePattern]::Pattern
                    )
                    $invoke.Invoke()
                    if ($Command -eq "approve") {
                        Write-Result @{
                            success = $true
                            action = "approved"
                            process_id = $window.Current.ProcessId
                            window_handle = $window.Current.NativeWindowHandle
                        }
                        exit 0
                    }
                }
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($Command -eq "watch") {
        exit 0
    }
    Write-Result @{ success = $false; action = "approval_prompt_not_found" }
    exit 2
}

$window = Wait-ForProfileWindow -PreferredHandle $WindowHandle
if ($null -eq $window) {
    Write-Result @{ success = $false; action = "profile_window_not_found" }
    exit 3
}

$checkbox = Find-RemoteDebuggingCheckbox -Window $window
if ($null -eq $checkbox) {
    Navigate-ToRemoteDebugging -Window $window
    $checkbox = Wait-ForRemoteDebuggingCheckbox -Window $window
}
if ($null -eq $checkbox) {
    Write-Result @{
        success = $false
        action = "remote_debugging_checkbox_not_found"
        process_id = $window.Current.ProcessId
        window_handle = $window.Current.NativeWindowHandle
    }
    exit 4
}

$toggle = $checkbox.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
$before = $toggle.Current.ToggleState.ToString()
$wantOn = $Command -eq "enable"
if (($wantOn -and $before -ne "On") -or (-not $wantOn -and $before -eq "On")) {
    $toggle.Toggle()
    Start-Sleep -Milliseconds 500
}
$after = $toggle.Current.ToggleState.ToString()

$result = @{
    success = ($wantOn -and $after -eq "On") -or (-not $wantOn -and $after -ne "On")
    action = $Command
    before = $before
    after = $after
    process_id = $window.Current.ProcessId
    window_handle = $window.Current.NativeWindowHandle
    window_title = $window.Current.Name
}

if ($Command -eq "disable" -and $CloseWindow) {
    try {
        $windowPattern = $window.GetCurrentPattern(
            [System.Windows.Automation.WindowPattern]::Pattern
        )
        $windowPattern.Close()
        $result["window_closed"] = $true
    }
    catch {
        $result["window_closed"] = $false
        $result["window_close_error"] = $_.Exception.Message
    }
}

Write-Result $result
exit $(if ($result.success) { 0 } else { 5 })
