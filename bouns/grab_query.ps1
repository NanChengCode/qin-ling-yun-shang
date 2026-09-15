# =====================================================================
#  秦岭云商 - 信息查询脚本
#  功能: 候选订单列表 / 车队信息 / 车队车辆统计
#  用法示例:
#    .\grab_query.ps1 -Username 138xxxx -Password 你的密码 -Action Orders -WithQuota -OutFile orders.json
#    .\grab_query.ps1 -Username 138xxxx -Password 你的密码 -Action Fleets -OutFile fleets.json
#    .\grab_query.ps1 -Username 138xxxx -Password 你的密码 -Action FleetVehicles -FleetId 5531 -OutFile v.json
# =====================================================================
[CmdletBinding()]
param(
    [string]$Username,          # 登录账号
    [string]$Password,          # 登录密码
    [string]$SmsCode,           # 短信二次验证码(2FA, 可选)
    [string]$Cookie,            # 调试用: 直接注入登录Cookie(portalToken=..; rzzxToken=..), 跳过登录
    [ValidateSet('Orders', 'Fleets', 'FleetVehicles')]
    [string]$Action = 'Orders', # 查询类型
    [string]$FleetId,           # FleetVehicles 时必填: 车队ID
    [switch]$WithQuota,         # Orders 时: 附带每日预约配额查询
    [string]$OutFile = 'query_result.json'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Base = 'https://man.qinlingshuzi.com'
$UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
$RefDispatch = '/busi/tms/shipment/dispatchPage?menuId=2465'
$RefSite = '/index'

# 强制 TLS1.2, 关闭 Expect:100-continue
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]'Tls12,Tls11,Tls'
[Net.ServicePointManager]::Expect100Continue = $false

function Log {
    param([string]$Msg)
    [Console]::WriteLine(('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss.fff'), $Msg))
}

function E {
    param([AllowEmptyString()][string]$V)
    return [uri]::EscapeDataString($V)
}

# ---------------------------------------------------------------------
# RSA (与网站 login JS 相同)
# ---------------------------------------------------------------------
function New-RsaEncryptor {
    $b64 = 'MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKoR8mX0rGKLqzcWmOzbfj64K8ZIgOdHnzkXSOVOZbFu/TJhZ7rFAN+eaGkl3C4buccQd/EjEsj9ir7ijT7h96MCAwEAAQ=='
    $der = [Convert]::FromBase64String($b64)
    function Read-Len {
        param($bytes, [ref]$pos)
        $b = $bytes[$pos.Value]; $pos.Value++
        if ($b -lt 0x80) { return $b }
        $n = $b -band 0x7F
        $len = 0
        for ($i = 0; $i -lt $n; $i++) { $len = ($len -shl 8) -bor $bytes[$pos.Value]; $pos.Value++ }
        return $len
    }
    $p = 0
    $p++
    $null = Read-Len $der ([ref]$p)
    $p++
    $null = Read-Len $der ([ref]$p)
    $p++
    $oidLen = Read-Len $der ([ref]$p)
    $p += $oidLen
    $p++; $p++
    $p++
    $null = Read-Len $der ([ref]$p)
    $p++
    $p++
    $null = Read-Len $der ([ref]$p)
    $p++
    $nLen = Read-Len $der ([ref]$p)
    if ($der[$p] -eq 0) { $p++; $nLen-- }
    $mod = $der[$p..($p + $nLen - 1)]
    $p = $p + $nLen
    $p++
    $eLen = Read-Len $der ([ref]$p)
    $exp = $der[$p..($p + $eLen - 1)]
    $param = New-Object System.Security.Cryptography.RSAParameters
    $param.Modulus = $mod
    $param.Exponent = $exp
    $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider
    $rsa.ImportParameters($param)
    return $rsa
}

function Encrypt-JsText {
    param($rsa, [AllowEmptyString()][string]$Text)
    $bytes = [Text.Encoding]::UTF8.GetBytes(([string]$Text))
    $enc = $rsa.Encrypt($bytes, $false)
    return [Convert]::ToBase64String($enc)
}

# ---------------------------------------------------------------------
# HTTP 会话
# ---------------------------------------------------------------------
Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue

function New-Session {
    $jar = New-Object System.Net.CookieContainer
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.CookieContainer = $jar
    $handler.UseCookies = $true
    $handler.AllowAutoRedirect = $true
    $handler.AutomaticDecompression = [Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate
    $http = New-Object System.Net.Http.HttpClient($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(30)
    return @{ Jar = $jar; Http = $http }
}

function Set-SessionCookie {
    param($Sess, [string]$CookieText)
    foreach ($part in ($CookieText -split ';')) {
        $kv = $part.Trim() -split '=', 2
        if ($kv.Count -eq 2 -and $kv[0]) {
            try { $Sess.Jar.SetCookies([uri]$Base, $kv[0] + '=' + $kv[1]) } catch { }
        }
    }
    Log '已注入调试Cookie, 跳过登录'
}

function Add-BrowserHeaders {
    param($Req, [string]$Referer)
    $Req.Headers.TryAddWithoutValidation('User-Agent', $UA) | Out-Null
    $Req.Headers.TryAddWithoutValidation('Accept', 'application/json, text/javascript, */*; q=0.01') | Out-Null
    $Req.Headers.TryAddWithoutValidation('Accept-Language', 'zh-CN,zh;q=0.9') | Out-Null
    $Req.Headers.TryAddWithoutValidation('X-Requested-With', 'XMLHttpRequest') | Out-Null
    $Req.Headers.TryAddWithoutValidation('Origin', $Base) | Out-Null
    $Req.Headers.TryAddWithoutValidation('Referer', ($Base + $Referer)) | Out-Null
}

function Invoke-AppGet {
    param($Http, [string]$Url, [string]$Referer = '/index')
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $Url)
    Add-BrowserHeaders $req $Referer
    $resp = $Http.SendAsync($req).Result
    return [Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

function Invoke-AppPost {
    param($Http, [string]$Url, [string]$BodyString, [string]$Referer = '/index')
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, $Url)
    $req.Content = New-Object System.Net.Http.StringContent($BodyString, [Text.Encoding]::UTF8, 'application/x-www-form-urlencoded')
    $req.Content.Headers.ContentType.CharSet = 'UTF-8'
    Add-BrowserHeaders $req $Referer
    $resp = $Http.SendAsync($req).Result
    return [Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

function ConvertTo-JsonObj {
    param([string]$Text, [string]$Desc)
    try { return ($Text | ConvertFrom-Json) }
    catch { throw "[$Desc] 响应不是JSON: $($Text.Substring(0, [Math]::Min(120, $Text.Length)))" }
}

# ---------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------
function Invoke-QueryLogin {
    param($Http, $User, $Pwd, $Sms)
    Log "正在登录账号 [$User] ..."
    $rsa = New-RsaEncryptor
    $body = 'username=' + (E (Encrypt-JsText $rsa $User)) +
            '&password=' + (E (Encrypt-JsText $rsa $Pwd)) +
            '&validateCode=&type=password&vCode=' + (E (Encrypt-JsText $rsa ''))
    $j = ConvertTo-JsonObj (Invoke-AppPost $Http "$Base/login" $body '/login') '登录'
    if ($j.code -eq 0) {
        if ($j.data -and $j.data.type -eq 3) {
            Log '该账号需要短信二次验证 (2FA)'
            if (-not $Sms) { throw '账号需要短信二次验证, 请提供 -SmsCode' }
            $vbody = 'username=' + (E (Encrypt-JsText $rsa $User)) +
                     '&tempToken=' + (E ([string]$j.data.tempToken)) +
                     '&type=vCode&vCode=' + (E (Encrypt-JsText $rsa $Sms))
            $j2 = ConvertTo-JsonObj (Invoke-AppPost $Http "$Base/login/verify" $vbody '/login') '二次验证'
            if ($j2.code -ne 0) { throw ('二次验证失败: ' + $j2.msg) }
        }
        Log '登录成功'
        return
    }
    throw ('登录失败: ' + $j.msg)
}

# ---------------------------------------------------------------------
# 查询: 候选订单 (调度派车列表)
# ---------------------------------------------------------------------
function Get-QuotaInfo {
    param([string]$Html)
    $out = [PSCustomObject]@{ has = $false; today = $null; tomorrow = $null; after = $null }
    if ($Html -notmatch 'id="isToday"') { return $out }
    $out.has = $true
    $tm = [regex]::Match($Html, '今日[\d\-]+ \((\d+)/(\d+)\)')
    $tw = [regex]::Match($Html, '明日[\d\-]+ \((\d+)/(\d+)\)')
    $ta = [regex]::Match($Html, '后日[\d\-]+ \((\d+)/(\d+)\)')
    if ($tm.Success) { $out.today = $tm.Groups[1].Value + '/' + $tm.Groups[2].Value }
    if ($tw.Success) { $out.tomorrow = $tw.Groups[1].Value + '/' + $tw.Groups[2].Value }
    if ($ta.Success) { $out.after = $ta.Groups[1].Value + '/' + $ta.Groups[2].Value }
    return $out
}

function Get-Orders {
    param($Http)
    Log '正在查询候选订单列表...'
    $qBody = 'quoteCustomerName=&locationId=&quoteCode=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    $list = ConvertTo-JsonObj (Invoke-AppPost $Http "$Base/busi/tms/shipment/listSupplyQuote" $qBody $RefDispatch) '候选订单'
    Log ('共查询到 ' + @($list.rows).Count + ' 条候选订单')
    $rows = @()
    $i = 0
    foreach ($q in @($list.rows)) {
        $row = [PSCustomObject]@{
            id                = $q.id
            quoteCode         = $q.quoteCode
            csrOrderCode      = $q.csrOrderCode
            customerName      = $q.customerName
            productName       = $q.productName
            sender            = $q.sender
            consignee         = $q.consignee
            inCompleteWeight  = $q.inCompleteWeight
            weight            = $q.weight
            expectArrivedTime = $q.expectArrivedTime
            transportType     = $q.transportType
            quotaHas          = $false
            quotaToday        = $null
            quotaTomorrow     = $null
            quotaAfter        = $null
        }
        if ($WithQuota -and $i -lt 20) {
            $loss = if ($null -eq $q.lossCoefficient) { 'null' } else { [string]$q.lossCoefficient }
            $burl = "$Base/busi/tms/shipment/batchDispatchVehicle?quoteIds=$($q.id)&vehicleId=&fromSource=add&lossCoefficient=" + (E $loss)
            try {
                $quota = Get-QuotaInfo (Invoke-AppGet $Http $burl $RefDispatch)
                $row.quotaHas = $quota.has
                $row.quotaToday = $quota.today
                $row.quotaTomorrow = $quota.tomorrow
                $row.quotaAfter = $quota.after
            } catch {
                Log ("订单 $($q.quoteCode) 配额查询跳过: " + $_.Exception.Message)
            }
        }
        $rows += $row
        $i = $i + 1
    }
    return ,$rows
}

# ---------------------------------------------------------------------
# 查询: 车队信息
# ---------------------------------------------------------------------
function Get-Fleets {
    param($Http)
    Log '正在查询车队信息...'
    $mBody = 'name=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    $ml = ConvertTo-JsonObj (Invoke-AppPost $Http "$Base/busi/base/motorcade/list" $mBody '/busi/base/motorcade') '车队信息'
    Log ('共查询到 ' + @($ml.rows).Count + ' 个车队')
    $rows = @()
    foreach ($m in @($ml.rows)) {
        $rows += [PSCustomObject]@{
            id             = $m.id
            name           = $m.name
            carrierId      = $m.carrierId
            carrierName    = $m.carrierName
            carCaptainName = $m.carCaptainName
        }
    }
    return ,$rows
}

# ---------------------------------------------------------------------
# 查询: 车队车辆统计
# ---------------------------------------------------------------------
function Get-FleetVehicles {
    param($Http, [string]$FId)
    Log "正在查询车队 [$FId] 的车辆统计..."
    $mBody = 'name=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    $ml = ConvertTo-JsonObj (Invoke-AppPost $Http "$Base/busi/base/motorcade/list" $mBody '/busi/base/motorcade') '车队信息'
    $fleet = $null
    foreach ($m in @($ml.rows)) { if ([string]$m.id -eq $FId) { $fleet = $m; break } }
    if ($null -eq $fleet) { throw ('未找到车队ID: ' + $FId) }
    $carrierId = $fleet.carrierId

    $lurl = "$Base/busi/base/vehicle/batchList?carrierId=" + (E ([string]$carrierId)) + '&selectVehicle=true&selectVehicleIds=&locationId='
    $lBody = 'mdId=' + (E $FId) + '&plateNo=&driverName=&state=&isTram=&issueVerifyStatus=&roadCardVerifyStatus=&pageSize=1000&pageNum=1&orderByColumn=&isAsc=asc'
    $vv = ConvertTo-JsonObj (Invoke-AppPost $Http $lurl $lBody '/busi/base/motorcade') '车队车辆'
    $s10 = 0; $s20 = 0; $s30 = 0; $s40 = 0; $sOther = 0
    foreach ($v in @($vv.rows)) {
        switch ([string]$v.state) {
            '10' { $s10 = $s10 + 1 }
            '20' { $s20 = $s20 + 1 }
            '30' { $s30 = $s30 + 1 }
            '40' { $s40 = $s40 + 1 }
            default { $sOther = $sOther + 1 }
        }
    }
    Log "车队 [$($fleet.name)] 共 $($vv.total) 辆 (空闲$s10 已派$s20 排队$s30 已发运$s40)"
    return [PSCustomObject]@{
        fleetId     = $FId
        fleetName   = $fleet.name
        carrierId   = $carrierId
        carrierName = $fleet.carrierName
        total       = $vv.total
        state10     = $s10
        state20     = $s20
        state30     = $s30
        state40     = $s40
        stateOther  = $sOther
    }
}

# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------
$outFull = $OutFile
if (-not [IO.Path]::IsPathRooted($outFull)) {
    $outFull = Join-Path (Get-Location).Path $OutFile
}
$payload = $null
try {
    $sess = New-Session
    if ($Cookie) {
        Set-SessionCookie $sess $Cookie
    } else {
        Invoke-QueryLogin $sess.Http $Username $Password $SmsCode
    }
    switch ($Action) {
        'Orders' { $data = Get-Orders $sess.Http; $payload = @{ ok = $true; action = 'Orders'; rows = $data } }
        'Fleets' { $data = Get-Fleets $sess.Http; $payload = @{ ok = $true; action = 'Fleets'; rows = $data } }
        'FleetVehicles' {
            if (-not $FleetId) { throw 'FleetVehicles 需要 -FleetId 参数' }
            $data = Get-FleetVehicles $sess.Http $FleetId
            $payload = @{ ok = $true; action = 'FleetVehicles'; data = $data }
        }
    }
    $json = $payload | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($outFull, $json, [Text.Encoding]::UTF8)
    Log ('结果已写入: ' + $outFull)
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log ('查询失败: ' + $msg)
    try {
        $err = @{ ok = $false; msg = $msg } | ConvertTo-Json
        [IO.File]::WriteAllText($outFull, $err, [Text.Encoding]::UTF8)
    } catch { }
    exit 1
}