# =====================================================================
#  秦岭云商自动抢单工具 - 本地图形界面服务
#  启动方式: 双击 "启动抢单界面.bat" 或执行:
#    powershell -NoProfile -ExecutionPolicy Bypass -File .\grab_server.ps1
#  访问: http://127.0.0.1:8787/   (关闭本窗口即停止服务)
# =====================================================================
[CmdletBinding()]
param(
    [int]$Port = 8787,          # 监听端口
    [switch]$NoBrowser          # 启动时不自动打开浏览器
)

$ErrorActionPreference = 'Stop'
$Root     = $PSScriptRoot
$ToolPath = Join-Path $Root 'grab_order.ps1'
$HtmlPath = Join-Path $Root 'grab_ui.html'
$QueryPath = Join-Path $Root 'grab_query.ps1'

if (-not (Test-Path $ToolPath)) { Write-Output ('错误: 未找到 ' + $ToolPath); Read-Host '按回车退出'; exit 1 }
if (-not (Test-Path $HtmlPath)) { Write-Output ('错误: 未找到 ' + $HtmlPath); Read-Host '按回车退出'; exit 1 }
if (-not (Test-Path $QueryPath)) { Write-Output ('错误: 未找到 ' + $QueryPath); Read-Host '按回车退出'; exit 1 }

# ---------------------------------------------------------------------
# 内嵌 C# 子进程运行器 (线程安全捕获输出, 避免PowerShell事件线程崩溃)
# ---------------------------------------------------------------------
Add-Type -TypeDefinition @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

public class ProcRunner
{
    [DllImport("kernel32.dll")]
    private static extern int GetOEMCP();

    private Process proc;
    private StringBuilder log = new StringBuilder();
    private object sync = new object();
    public volatile bool Done = false;
    public int ExitCode = 1;

    public string Start(string file, string args, string workDir)
    {
        try
        {
            Encoding oem = null;
            try { oem = Encoding.GetEncoding(GetOEMCP()); } catch { oem = Encoding.UTF8; }
            proc = new Process();
            proc.StartInfo.FileName = file;
            proc.StartInfo.Arguments = args;
            proc.StartInfo.WorkingDirectory = workDir;
            proc.StartInfo.UseShellExecute = false;
            proc.StartInfo.RedirectStandardOutput = true;
            proc.StartInfo.RedirectStandardError = true;
            proc.StartInfo.CreateNoWindow = true;
            proc.StartInfo.StandardOutputEncoding = oem;
            proc.StartInfo.StandardErrorEncoding = oem;
            proc.EnableRaisingEvents = true;
            proc.OutputDataReceived += (s, e) => { if (e.Data != null) Append(e.Data); };
            proc.ErrorDataReceived += (s, e) => { if (e.Data != null) Append(e.Data); };
            proc.Exited += (s, e) =>
            {
                lock (sync)
                {
                    Done = true;
                    try { ExitCode = proc.ExitCode; } catch { ExitCode = 1; }
                }
            };
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            return null;
        }
        catch (Exception ex)
        {
            lock (sync) { Done = true; ExitCode = 1; }
            return ex.Message;
        }
    }

    public bool HasExited
    {
        get { try { return proc == null || proc.HasExited; } catch { return true; } }
    }

    private void Append(string line)
    {
        lock (sync) { log.AppendLine(line); }
    }

    public string Drain()
    {
        lock (sync)
        {
            string t = log.ToString();
            log.Clear();
            return t;
        }
    }

    public void Kill()
    {
        try { if (proc != null && !proc.HasExited) proc.Kill(); } catch { }
    }
}
"@

# ---------------------------------------------------------------------
# 运行状态 (单任务)
# ---------------------------------------------------------------------
$script:Runner = $null

function Quote-Ps {
    param([string]$V)
    return "'" + ($V -replace "'", "''") + "'"
}

function Start-Grab {
    param($U, $P, $Oc, $Fn, $Dt, $Mx, $Dry, $Sms)

    # 用 -EncodedCommand 传递完整调用, 规避任何特殊字符/引号问题
    $inner = '& ' + (Quote-Ps $ToolPath) +
             ' -Username ' + (Quote-Ps $U) +
             ' -Password ' + (Quote-Ps $P) +
             ' -OrderCode ' + (Quote-Ps $Oc) +
             ' -FleetName ' + (Quote-Ps $Fn) +
             ' -Date ' + (Quote-Ps $Dt)
    if ($Mx -gt 0) { $inner += ' -MaxVehicle ' + [string]$Mx }
    if ($Sms)      { $inner += ' -SmsCode ' + (Quote-Ps $Sms) }
    if ($Dry)      { $inner += ' -DryRun' }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $b64)

    $runner = New-Object ProcRunner
    $err = $runner.Start('powershell.exe', ($args -join ' '), $Root)
    if ($err) {
        $runner.Drain() | Out-Null
        return ('启动失败: ' + $err)
    }
    $script:Runner = $runner
    return $null
}

function Stop-Grab {
    if ($null -ne $script:Runner) { $script:Runner.Kill() }
}

function Drain-Log {
    if ($null -eq $script:Runner) { return $null }
    return $script:Runner.Drain()
}

function Get-RunState {
    if ($null -eq $script:Runner) {
        return @{ running = $false; done = $false; exitCode = $null }
    }
    return @{
        running  = (-not $script:Runner.Done)
        done     = $script:Runner.Done
        exitCode = if ($script:Runner.Done) { $script:Runner.ExitCode } else { $null }
    }
}

# ---------------------------------------------------------------------
# 查询任务: 调用 grab_query.ps1 并同步等待结果
# 返回 rawJson 原样透传 (不经过 ConvertFrom/To-Json 二次转换, 避免字段丢失)
# ---------------------------------------------------------------------
function Invoke-QueryChild {
    param($User, $Pwd, $Sms, [string[]]$ExtraArgs)

    $outPath = Join-Path $env:TEMP ('grab_q_' + [guid]::NewGuid().ToString('N') + '.json')
    $inner = '& ' + (Quote-Ps $QueryPath) +
             ' -Username ' + (Quote-Ps $User) +
             ' -Password ' + (Quote-Ps $Pwd)
    if ($Sms) { $inner += ' -SmsCode ' + (Quote-Ps $Sms) }
    foreach ($a in $ExtraArgs) { $inner += ' ' + $a }
    $inner += ' -OutFile ' + (Quote-Ps $outPath)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

    $runner = New-Object ProcRunner
    $err = $runner.Start('powershell.exe', ('-NoProfile -ExecutionPolicy Bypass -EncodedCommand ' + $b64), $Root)
    if ($err) { return @{ ok = $false; raw = ''; msg = ('启动失败: ' + $err) } }

    $script:Runner = $runner
    $deadline = (Get-Date).AddMinutes(3)
    while (-not $runner.Done) {
        $newText = Drain-Log
        if ($newText) { $script:MasterLog += $newText }
        if ((Get-Date) -gt $deadline) { $runner.Kill(); break }
        Start-Sleep -Milliseconds 200
    }
    $newText = Drain-Log
    if ($newText) { $script:MasterLog += $newText }

    if (-not (Test-Path $outPath)) {
        return @{ ok = $false; raw = ''; msg = '查询进程异常退出, 请查看运行日志' }
    }
    try {
        $bytes = [IO.File]::ReadAllBytes($outPath)
        Remove-Item $outPath -Force -ErrorAction SilentlyContinue
        # 去掉 UTF8 BOM
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            $bytes = $bytes[3..($bytes.Length - 1)]
        }
        return @{ ok = $true; raw = [Text.Encoding]::UTF8.GetString($bytes); msg = '' }
    } catch {
        Remove-Item $outPath -Force -ErrorAction SilentlyContinue
        return @{ ok = $false; raw = ''; msg = ('读取查询结果失败: ' + $_.Exception.Message) }
    }
}

function Handle-Query {
    param($Ctx, [string]$Body, [string[]]$Args)

    if ($null -ne $script:Runner -and (-not $script:Runner.Done) -and (-not $script:Runner.HasExited)) {
        Send-Json $Ctx @{ ok = $false; msg = '已有任务正在运行, 请稍后再试' } 409
        return
    }
    $in = $Body | ConvertFrom-Json
    if (-not $in.username -or -not $in.password) {
        Send-Json $Ctx @{ ok = $false; msg = '请先填写登录账号和密码' } 400
        return
    }
    $script:MasterLog = ''
    $res = Invoke-QueryChild ([string]$in.username) ([string]$in.password) ([string]$in.smsCode) $Args
    if (-not $res.ok) {
        Send-Json $Ctx @{ ok = $false; msg = $res.msg } 500
        return
    }
    # 查询结果 JSON 原样透传给浏览器
    Send-Raw $Ctx $res.raw
}

# ---------------------------------------------------------------------
# 响应辅助
# ---------------------------------------------------------------------
function Send-Json {
    param($Ctx, $Obj, [int]$Code = 200)
    $json = ConvertTo-Json $Obj -Compress -Depth 5
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $Ctx.Response.StatusCode = $Code
    $Ctx.Response.ContentType = 'application/json; charset=utf-8'
    $Ctx.Response.ContentLength64 = $bytes.Length
    $Ctx.Response.Headers.Add('Cache-Control', 'no-store')
    $Ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Send-Raw {
    param($Ctx, [string]$Text)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $Ctx.Response.StatusCode = 200
    $Ctx.Response.ContentType = 'application/json; charset=utf-8'
    $Ctx.Response.ContentLength64 = $bytes.Length
    $Ctx.Response.Headers.Add('Cache-Control', 'no-store')
    $Ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

# ---------------------------------------------------------------------
# HTTP 服务 (仅监听 127.0.0.1)
# ---------------------------------------------------------------------
$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
try {
    $listener.Start()
} catch {
    $startErrMsg = $_.Exception.Message
    # 端口被占用: 找到正在运行的旧 grab_server 实例并结束, 重试一次
    $retried = $false
    try {
        $myself = $PID
        $targets = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $myself -and $_.CommandLine -match 'grab_server\.ps1' }
        foreach ($t in $targets) {
            Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($targets) {
            Start-Sleep -Milliseconds 800
            $listener = New-Object System.Net.HttpListener
            $listener.Prefixes.Add("http://127.0.0.1:$Port/")
            $listener.Start()
            $retried = $true
            Write-Output '(已自动接管端口: 旧服务实例被关闭)'
        }
    } catch { }
    if (-not $retried) {
        Write-Output ('启动失败: ' + $startErrMsg)
        Write-Output '解决办法(二选一):'
        Write-Output '  1. 以管理员身份重新运行本脚本'
        Write-Output ('  2. 管理员命令行执行一次: netsh http add urlacl url=http://127.0.0.1:' + $Port + '/ user=Everyone')
        Read-Host '按回车退出'
        exit 1
    }
}

Write-Output ('图形界面服务已启动: http://127.0.0.1:' + $Port + '/')
Write-Output '关闭本窗口即可停止服务'
if (-not $NoBrowser) {
    try { Start-Process ("http://127.0.0.1:" + $Port + "/") } catch { }
}

$script:MasterLog = ''
$script:MasterOffset = 0

while ($true) {
    $ctx = $listener.GetContext()
    try {
        $path = $ctx.Request.Url.AbsolutePath

        # 取出子进程新增日志
        $newText = Drain-Log
        if ($newText) {
            $script:MasterLog += $newText
        }

        switch ($path) {
            '/' {
                $html = [IO.File]::ReadAllText($HtmlPath, [Text.Encoding]::UTF8)
                $bytes = [Text.Encoding]::UTF8.GetBytes($html)
                $ctx.Response.ContentType = 'text/html; charset=utf-8'
                $ctx.Response.ContentLength64 = $bytes.Length
                $ctx.Response.Headers.Add('Cache-Control', 'no-store')
                $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
            }
            '/api/run' {
                $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
                $body = $reader.ReadToEnd()
                $in = $body | ConvertFrom-Json
                if ($null -ne $script:Runner -and (-not $script:Runner.Done) -and $script:Runner.HasExited -eq $false) {
                    Send-Json $ctx @{ ok = $false; msg = '已有任务正在运行, 请先停止或等待结束' } 409
                    break
                }
                if (-not $in.username -or -not $in.password -or -not $in.orderCode -or -not $in.fleetName) {
                    Send-Json $ctx @{ ok = $false; msg = '账号/密码/订单号/车队名称 不能为空' } 400
                    break
                }
                $date = [string]$in.date
                if ($date -notin @('今日','明日','后日')) { $date = '今日' }
                $mx = if ($in.maxVehicle) { [int]$in.maxVehicle } else { 0 }
                $script:MasterLog = ''
                $err = Start-Grab $in.username $in.password $in.orderCode $in.fleetName $date $mx $in.dryRun $in.smsCode
                if ($err) {
                    $script:MasterLog += ('[SERVER] ' + $err + "`r`n")
                    $script:Runner = $null
                    Send-Json $ctx @{ ok = $false; msg = $err } 500
                    break
                }
                Send-Json $ctx @{ ok = $true; msg = '任务已启动' }
            }
            '/api/log' {
                $since = 0
                $qs = $ctx.Request.QueryString['since']
                if ($qs) {
                    $tmp = 0
                    if ([int]::TryParse([string]$qs, [ref]$tmp)) { $since = $tmp }
                }
                $total = $script:MasterLog.Length
                $text = ''
                if ($since -lt $total) { $text = $script:MasterLog.Substring($since, $total - $since) }
                $st = Get-RunState
                Send-Json $ctx @{
                    ok       = $true
                    text     = $text
                    offset   = $total
                    running  = $st.running
                    done     = $st.done
                    exitCode = $st.exitCode
                }
            }
            '/api/stop' {
                Stop-Grab
                Send-Json $ctx @{ ok = $true; msg = '停止指令已发送' }
            }
            '/api/query/orders' {
                $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
                $qBody = $reader.ReadToEnd()
                $in = $qBody | ConvertFrom-Json
                $extra = @('-Action', 'Orders')
                if ($in.withQuota) { $extra += '-WithQuota' }
                Handle-Query $ctx $qBody $extra
            }
            '/api/query/fleets' {
                $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
                $qBody = $reader.ReadToEnd()
                Handle-Query $ctx $qBody @('-Action', 'Fleets')
            }
            '/api/query/fleetVehicles' {
                $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
                $qBody = $reader.ReadToEnd()
                $in = $qBody | ConvertFrom-Json
                if (-not $in.fleetId) {
                    Send-Json $ctx @{ ok = $false; msg = '缺少车队ID' } 400
                    break
                }
                Handle-Query $ctx $qBody @('-Action', 'FleetVehicles', '-FleetId', ([string]$in.fleetId))
            }
            default {
                Send-Json $ctx @{ ok = $false; msg = 'not found' } 404
            }
        }
    } catch {
        try { Send-Json $ctx @{ ok = $false; msg = ('服务内部错误: ' + $_.Exception.Message) } 500 } catch { }
    } finally {
        try { $ctx.Response.Close() } catch { }
    }
}