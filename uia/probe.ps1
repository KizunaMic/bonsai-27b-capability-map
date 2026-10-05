# UIA 探针：看你的机器上各应用能暴露出多少控件
# 用 PowerShell + .NET UIAutomationClient（Windows 自带，无需安装）
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$root = [System.Windows.Automation.AutomationElement]::RootElement

# 只探测白名单内的应用
$targets = @('mspaint', 'notepad')

Write-Output "=" * 70
Write-Output "UI Automation 控件树探针"
Write-Output "=" * 70

foreach ($proc in $targets) {
    $procs = Get-Process -Name $proc -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Output ""
        Write-Output "[$proc] 未运行，跳过"
        continue
    }
    $p = $procs | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if (-not $p) {
        Write-Output ""
        Write-Output "[$proc] 无主窗口，跳过"
        continue
    }

    $hwnd = $p.MainWindowHandle
    $el = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    Write-Output ""
    Write-Output "=" * 70
    Write-Output "[$proc] 窗口: $($p.MainWindowTitle)  (hwnd=$hwnd)"
    Write-Output "=" * 70

    # 遍历控件树，限深度 4 层
    $count = 0
    $named = 0
    $buttons = @()
    $edits = @()

    function Walk($node, $depth) {
        if ($depth -gt 4 -or $script:count -gt 300) { return }
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $child = $walker.GetFirstChild($node)
        while ($child -ne $null) {
            $script:count++
            try {
                $ct = $child.Current.ControlType.ProgrammaticName -replace 'ControlType\.', ''
                $nm = $child.Current.Name
                $r  = $child.Current.BoundingRectangle
                $x = [int]$r.X; $y = [int]$r.Y
                $w = [int]$r.Width; $h = [int]$r.Height
                if ($nm) { $script:named++ }
                if ($ct -eq 'Button' -and $nm) { $script:buttons += $nm }
                if ($ct -match 'Edit|Document' -and $nm) { $script:edits += $nm }
                if ($depth -le 2 -and $nm) {
                    $pad = '  ' * $depth
                    Write-Output ("{0}[{1}] {2}  ({3},{4} {5}x{6})" -f $pad, $ct, $nm, $x, $y, $w, $h)
                }
            } catch {}
            Walk $child ($depth + 1)
            try { $child = $walker.GetNextSibling($child) } catch { break }
        }
    }

    Walk $el 0

    Write-Output ""
    Write-Output "---- 统计 ----"
    Write-Output "控件总数     : $count"
    Write-Output "有可读名称的 : $named"
    Write-Output "命名按钮     : $($buttons.Count)"
    if ($buttons.Count -gt 0) {
        Write-Output "  按钮列表   : $($buttons -join ' | ')"
    }
    if ($edits.Count -gt 0) {
        Write-Output "编辑区       : $($edits -join ' | ')"
    }
}

Write-Output ""
Write-Output "=" * 70
Write-Output "判读："
Write-Output "  控件总数多 + 有名称的按钮多 => UIA 可用，可做「按名字定位」"
Write-Output "  控件极少或全无名称          => 该应用是自绘界面，UIA 无效，需 OCR"
Write-Output "=" * 70
