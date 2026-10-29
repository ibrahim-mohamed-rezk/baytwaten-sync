# Creates the desktop shortcut "بيت الوطن – الحجوزات" that runs start.bat (server window minimized, browser opens
# by itself). Also draws app/static/icon.ico the first time. Run:  powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$ico = Join-Path $root "app\static\icon.ico"

if (-not (Test-Path $ico)) {
    Add-Type -AssemblyName System.Drawing
    $sizes = 256, 48, 32, 16
    $pngs = foreach ($n in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap $n, $n
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = "AntiAlias"
        $g.Clear([System.Drawing.Color]::Transparent)
        # rounded blue tile (the header logo's colour)
        $r = [int]($n * 0.22); $d = 2 * $r
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $path.AddArc(0, 0, $d, $d, 180, 90); $path.AddArc($n - $d - 1, 0, $d, $d, 270, 90)
        $path.AddArc($n - $d - 1, $n - $d - 1, $d, $d, 0, 90); $path.AddArc(0, $n - $d - 1, $d, $d, 90, 90)
        $path.CloseFigure()
        $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(79, 106, 240))), $path)
        # white house: roof + body + door
        $w = [System.Drawing.Brushes]::White
        $roof = [System.Drawing.PointF[]]@(
            (New-Object System.Drawing.PointF ($n * 0.5), ($n * 0.18)),
            (New-Object System.Drawing.PointF ($n * 0.84), ($n * 0.48)),
            (New-Object System.Drawing.PointF ($n * 0.16), ($n * 0.48)))
        $g.FillPolygon($w, $roof)
        $g.FillRectangle($w, [single]($n * 0.26), [single]($n * 0.46), [single]($n * 0.48), [single]($n * 0.34))
        $g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(79, 106, 240))),
            [single]($n * 0.44), [single]($n * 0.6), [single]($n * 0.12), [single]($n * 0.2))
        $g.Dispose()
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
        , $ms.ToArray()
    }
    # .ico container holding PNG images
    $out = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter $out
    $bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $n = $sizes[$i]; $b = if ($n -ge 256) { 0 } else { $n }
        $bw.Write([byte]$b); $bw.Write([byte]$b); $bw.Write([byte]0); $bw.Write([byte]0)
        $bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]$pngs[$i].Length); $bw.Write([uint32]$offset)
        $offset += $pngs[$i].Length
    }
    foreach ($p in $pngs) { $bw.Write($p) }
    [System.IO.File]::WriteAllBytes($ico, $out.ToArray())
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "بيت الوطن – الحجوزات.lnk"
# WScript.Shell can't save to a non-ASCII path: save under an ASCII name, then rename
$tmp = Join-Path $desktop "baytwaten-sync.lnk"
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($tmp)
$sh.TargetPath = Join-Path $root "start.bat"
$sh.WorkingDirectory = $root
$sh.IconLocation = "$ico,0"
$sh.WindowStyle = 7  # minimized: the server window waits in the taskbar; closing it stops the panel
$sh.Description = "Start the baytwaten-sync panel and open it in the browser"
$sh.Save()
Move-Item -LiteralPath $tmp -Destination $lnk -Force
"created: $lnk"
