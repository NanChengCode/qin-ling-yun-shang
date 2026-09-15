# =====================================================================
#  秦岭云商 自动抢单工具 (直接调用网页接口, 无需浏览器)
#  用法示例:
#    .\grab_order.ps1 -Username 138xxxx -Password 你的密码 `
#        -OrderCode SG260826133791-01 -FleetName 发发发 -Date 今日
#  可选参数:
#    -MaxVehicle N    最多选取 N 辆车(默认不限, 抢整个车队)
#    -SmsCode xxx     短信二次验证码(账号开启2FA时需要)
#    -DryRun          只走流程不提交(调试用)
#  =====================================================================
[CmdletBinding()]
param(
    [string]$Username,          # 登录账号
    [string]$Password,          # 登录密码
    [string]$OrderCode,         # 需要抢单的订单号
    [string]$FleetName,         # 需要抢单的车队名称 (如: 发发发)
    [ValidateSet('今日','明日','后日')]
    [string]$Date = '今日',     # 计划预约日期
    [string]$SmsCode,           # 短信二次验证码 (2FA, 可选)
    [int]$MaxVehicle = 0,       # 最多车辆数, 0=不限制
    [int]$MaxRetry = 6,         # 容量不足时最大重试次数
    [switch]$DryRun             # 演练模式: 不真正提交派车
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # 禁用进度输出(避免重定向时产生CLIXML噪音)
$script:Base = 'https://man.qinlingshuzi.com'

# 强制使用 TLS1.2, 关闭 Expect:100-continue (否则会被站点拦截)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]'Tls12,Tls11,Tls'
[Net.ServicePointManager]::Expect100Continue = $false

# 站点 WAF 会拦截不带浏览器头的 POST (返回404), 必须模拟浏览器请求头
$script:UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

# ---------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------
function Log {
    param([string]$Msg)
    # 直接写标准输出: 控制台/重定向/Web服务 三种场景下都是纯文本
    [Console]::WriteLine(('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss.fff'), $Msg))
}

# ---------------------------------------------------------------------
# HTTP 会话 (Cookie 自动维护)
# ---------------------------------------------------------------------
Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue

$script:Jar  = New-Object System.Net.CookieContainer
$script:Http = $null

function New-Session {
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.CookieContainer = $script:Jar
    $handler.UseCookies = $true
    $handler.AllowAutoRedirect = $true
    $handler.AutomaticDecompression = [Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate
    $script:Http = New-Object System.Net.Http.HttpClient($handler)
    $script:Http.Timeout = [TimeSpan]::FromSeconds(20)
}

function Add-BrowserHeaders {
    param($Req, [string]$Referer)
    $Req.Headers.TryAddWithoutValidation('User-Agent', $script:UA) | Out-Null
    $Req.Headers.TryAddWithoutValidation('Accept', 'application/json, text/javascript, */*; q=0.01') | Out-Null
    $Req.Headers.TryAddWithoutValidation('Accept-Language', 'zh-CN,zh;q=0.9') | Out-Null
    $Req.Headers.TryAddWithoutValidation('X-Requested-With', 'XMLHttpRequest') | Out-Null
    $Req.Headers.TryAddWithoutValidation('Origin', $script:Base) | Out-Null
    $Req.Headers.TryAddWithoutValidation('Referer', ($script:Base + $Referer)) | Out-Null
}

function Invoke-Get {
    param([string]$Url, [string]$Referer = '/index')
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $Url)
    Add-BrowserHeaders $req $Referer
    $resp = $script:Http.SendAsync($req).Result
    $bytes = $resp.Content.ReadAsByteArrayAsync().Result
    return [Text.Encoding]::UTF8.GetString($bytes)
}

function Invoke-PostForm {
    param([string]$Url, [string]$BodyString, [string]$Referer = '/index')
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, $Url)
    $req.Content = New-Object System.Net.Http.StringContent($BodyString, [Text.Encoding]::UTF8, 'application/x-www-form-urlencoded')
    $req.Content.Headers.ContentType.CharSet = 'UTF-8'
    Add-BrowserHeaders $req $Referer
    $resp = $script:Http.SendAsync($req).Result
    $bytes = $resp.Content.ReadAsByteArrayAsync().Result
    return [Text.Encoding]::UTF8.GetString($bytes)
}

function ConvertTo-JsonObj {
    param([string]$Text, [string]$Desc)
    try { return ($Text | ConvertFrom-Json) }
    catch { throw "[$Desc] 响应不是JSON, 可能登录已失效: $($Text.Substring(0, [Math]::Min(180, $Text.Length)))" }
}

function E {
    param([AllowEmptyString()][string]$V)
    return [uri]::EscapeDataString($V)
}

# ---------------------------------------------------------------------
# RSA (与网站 login JS 相同: JSEncrypt + PKCS1 v1.5, 512位公钥)
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
    $p++                                  # 外层 SEQUENCE tag
    $null = Read-Len $der ([ref]$p)
    $p++                                  # 内层 SEQUENCE tag
    $null = Read-Len $der ([ref]$p)
    $p++                                  # OID tag
    $oidLen = Read-Len $der ([ref]$p)
    $p += $oidLen
    $p++; $p++                            # NULL tag + len
    $p++                                  # BIT STRING tag
    $null = Read-Len $der ([ref]$p)
    $p++                                  # unused-bits 字节
    $p++                                  # 内层 SEQUENCE tag
    $null = Read-Len $der ([ref]$p)
    $p++                                  # INTEGER tag (modulus)
    $nLen = Read-Len $der ([ref]$p)
    if ($der[$p] -eq 0) { $p++; $nLen-- } # 去掉前导 0x00
    $mod = $der[$p..($p + $nLen - 1)]
    $p = $p + $nLen
    $p++                                  # INTEGER tag (exponent)
    $eLen = Read-Len $der ([ref]$p)
    $exp = $der[$p..($p + $eLen - 1)]
    $param = New-Object System.Security.Cryptography.RSAParameters
    $param.Modulus  = $mod
    $param.Exponent = $exp
    $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider
    $rsa.ImportParameters($param)
    return $rsa
}

function Encrypt-JsText {
    param($rsa, [AllowEmptyString()][string]$Text)
    $bytes = [Text.Encoding]::UTF8.GetBytes(([string]$Text))
    $enc = $rsa.Encrypt($bytes, $false)   # PKCS1 v1.5
    return [Convert]::ToBase64String($enc)
}

# ---------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------
function Invoke-AppLogin {
    param([string]$User, [string]$Pwd)
    Log "正在登录账号 [$User] ..."
    $rsa = New-RsaEncryptor
    $body = 'username=' + (E (Encrypt-JsText $rsa $User)) +
            '&password=' + (E (Encrypt-JsText $rsa $Pwd)) +
            '&validateCode=&type=password&vCode=' + (E (Encrypt-JsText $rsa ''))
    $j = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/login" $body '/login') '登录'
    if ($j.code -eq 0) {
        if ($j.data -and $j.data.type -eq 3) {
            Log "该账号需要短信二次验证 (2FA)"
            $code = $SmsCode
            if (-not $code) {
                try { $code = Read-Host '请输入手机短信验证码' }
                catch {
                    Log '无法交互式输入验证码(非交互模式), 请在界面填写短信验证码后重试'
                    return $false
                }
            }
            $vbody = 'username=' + (E (Encrypt-JsText $rsa $User)) +
                     '&tempToken=' + (E ([string]$j.data.tempToken)) +
                     '&type=vCode&vCode=' + (E (Encrypt-JsText $rsa $code))
            $j2 = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/login/verify" $vbody '/login') '二次验证'
            if ($j2.code -eq 0) { Log '二次验证通过, 登录成功'; return $true }
            Log "二次验证失败: $($j2.msg)"
            return $false
        }
        Log '登录成功'
        return $true
    }
    Log "登录失败: $($j.msg)"
    if ("$($j.msg)" -match '验证码') {
        Log '提示: 若系统要求图形验证码, 请先网页登录确认账号状态或稍后重试'
    }
    return $false
}

# ---------------------------------------------------------------------
# 解析页面隐藏字段
# ---------------------------------------------------------------------
function Get-PageValue {
    param([string]$Html, [string]$Id)
    $m = [regex]::Match($Html, 'id="' + [regex]::Escape($Id) + '"[^>]*')
    if (-not $m.Success) { return $null }
    $mv = [regex]::Match($m.Value, 'value="([^"]*)"')
    if ($mv.Success) { return $mv.Groups[1].Value }
    return ''   # 存在但没有 value 属性 → 视为空串
}

$script:RefDispatch = '/busi/tms/shipment/dispatchPage?menuId=2465'
$script:RefVehicle  = '/busi/base/vehicle/batchSearchList'

# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------
function Main {
    Log '========== 秦岭云商自动抢单工具启动 =========='
    Log "订单号: $OrderCode | 车队: $FleetName | 目标日期: $Date | DryRun: $DryRun"

    New-Session

    # 1. 登录
    if (-not (Invoke-AppLogin $Username $Password)) { return 1 }

    # 2. 按订单号查询订单
    Log "正在查询订单 [$OrderCode] ..."
    $qBody = 'quoteCustomerName=&locationId=&quoteCode=' + (E $OrderCode) +
             '&pageSize=10&pageNum=1&orderByColumn=&isAsc=asc'
    $q = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/busi/tms/shipment/listSupplyQuote" $qBody $script:RefDispatch) '订单查询'
    if (-not $q.rows -or $q.rows.Count -eq 0) {
        Log "未找到订单号 [$OrderCode], 请确认订单号是否正确"
        return 1
    }
    $quote = $q.rows[0]
    $quoteId = $quote.id
    $lossStr = if ($null -eq $quote.lossCoefficient) { 'null' } else { [string]$quote.lossCoefficient }
    Log "找到订单: id=$quoteId, 剩余发货量=$($quote.inCompleteWeight)吨, 货品=$($quote.productName)"

    # 2.1 取订单当前剩余量 (权威值)
    $dj = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/busi/tms/shipment/selectQuoteListByID?selectQuoteIds=$quoteId" '' $script:RefDispatch) '订单详情'
    $remaining = [double]$dj.rows[0].inCompleteWeight
    Log "订单当前剩余发货量: $remaining 吨"

    # 3. 加载批量派车页面, 解析表单隐藏字段
    $burl = "$script:Base/busi/tms/shipment/batchDispatchVehicle?quoteIds=$quoteId&vehicleId=&fromSource=add&lossCoefficient=" + (E $lossStr)
    $bhtml = Invoke-Get $burl $script:RefDispatch
    $carrierId   = Get-PageValue $bhtml 'carrierId'
    $carrierName = Get-PageValue $bhtml 'carrierName'
    $planLoadingTime = Get-PageValue $bhtml 'planLoadingTime'
    $planArriveTime  = Get-PageValue $bhtml 'planArriveTime'
    $enableGoStation = Get-PageValue $bhtml 'enableGoStationName'
    $orderLocationId = Get-PageValue $bhtml 'orderLocationId'
    $tramFlag = Get-PageValue $bhtml 'tramFlagHidden'
    if ($null -eq $carrierId) { Log '解析批量派车页面失败(缺少carrierId), 可能页面结构变化'; return 1 }
    Log "承运商: $carrierName (id=$carrierId)"

    # 计划预约日期: 仅当页面包含该区块时提交 isToday
    $hasIsToday = $bhtml -match 'id="isToday"'
    $isTodayVal = if ($Date -eq '今日') { '1' } elseif ($Date -eq '明日') { '2' } else { '3' }
    if ($hasIsToday) {
        $tm = [regex]::Match($bhtml, '今日([\d\-]+) \((\d+)/(\d+)\)')
        $tw = [regex]::Match($bhtml, '明日([\d\-]+) \((\d+)/(\d+)\)')
        $ta = [regex]::Match($bhtml, '后日([\d\-]+) \((\d+)/(\d+)\)')
        Log ("计划预约日期配额: 今日(剩{0}/{1}) 明日(剩{2}/{3}) 后日(剩{4}/{5})" -f
            $(if($tm.Success){$tm.Groups[2].Value}else{'?'}), $(if($tm.Success){$tm.Groups[3].Value}else{'?'}),
            $(if($tw.Success){$tw.Groups[2].Value}else{'?'}), $(if($tw.Success){$tw.Groups[3].Value}else{'?'}),
            $(if($ta.Success){$ta.Groups[2].Value}else{'?'}), $(if($ta.Success){$ta.Groups[3].Value}else{'?'}))
        Log "本次选择日期: $Date (isToday=$isTodayVal)"
    } else {
        Log '该订单不需要计划预约日期'
    }

    # 4. 车队名称 → 车队ID (解析车辆选择窗口页面的下拉选项)
    $vurl = "$script:Base/busi/base/vehicle/batchSearchList?carrierId=" + (E $carrierId) +
            '&selectVehicleIds=&locationId=' + (E $orderLocationId) + '&tramFlag=' + (E $tramFlag)
    $vhtml = Invoke-Get $vurl $script:RefDispatch
    $fm = [regex]::Match($vhtml, '<option value="(\d+)">' + [regex]::Escape($FleetName) + '</option>')
    if (-not $fm.Success) {
        Log "未找到车队 [$FleetName]"
        $allFleets = [regex]::Matches($vhtml, '<option value="\d+">([^<]+)</option>') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
        Log ('当前承运商下车队列表: ' + ($allFleets -join ', '))
        return 1
    }
    $mdId = $fm.Groups[1].Value
    Log "车队 [$FleetName] → ID=$mdId"

    # 5. 查询车队全部车辆
    $lurl = "$script:Base/busi/base/vehicle/batchList?carrierId=" + (E $carrierId) + '&selectVehicle=true&selectVehicleIds=&locationId=' + (E $orderLocationId)
    $lBody = 'mdId=' + (E $mdId) + '&plateNo=&driverName=&state=&isTram=&issueVerifyStatus=&roadCardVerifyStatus=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    $lv = ConvertTo-JsonObj (Invoke-PostForm $lurl $lBody $script:RefVehicle) '车队车辆查询'
    $vehicles = @($lv.rows)
    if ($vehicles.Count -eq 0) { Log "车队 [$FleetName] 没有可选车辆"; return 1 }
    Log "车队共 $($lv.total) 辆车 (可选 $($vehicles.Count) 辆)"

    if ($MaxVehicle -gt 0 -and $MaxVehicle -lt $vehicles.Count) {
        $vehicles = @($vehicles | Select-Object -First $MaxVehicle)
        Log "按 -MaxVehicle 限制, 仅使用前 $MaxVehicle 辆"
    }

    $candidates = $vehicles

    # 6. 校验+提交 主循环 (容量不足自动缩减重试)
    for ($attempt = 1; $attempt -le $MaxRetry; $attempt++) {
        $ids   = @($candidates | ForEach-Object { [string]$_.id })
        $mdIds = @($candidates | ForEach-Object { [string]$_.mdId })
        Log "---- 第 $attempt 次尝试: 拟派 $($ids.Count) 辆车 ----"

        $ckBody = 'vehicleIds=' + (E ($ids -join ',')) + '&mdIds=' + (E ($mdIds -join ',')) + "&quoteIds=$quoteId"

        $bk = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/busi/tms/shipment/checkVehicleBacklist" $ckBody $script:RefDispatch) '黑名单校验'
        if ($bk.code -ne 0) {
            Log "车辆黑名单校验失败: $($bk.msg)"
            return 1
        }
        Log "黑名单校验通过"

        $cap = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/busi/tms/shipment/checkVehicleCapacity" $ckBody $script:RefDispatch) '容量校验'
        if (($cap.data -is [string]) -and ($cap.data -eq 'false')) {
            Log "容量校验未通过: $($cap.msg) (剩余 $remaining 吨)"
            # 缩减候选: 1) 只保留额定载重 <= 剩余量的车  2) 贪心累计不超过剩余量
            $fits = @($vehicles | Where-Object { [double]$_.loadCapacity -le $remaining })
            if ($fits.Count -eq 0) {
                Log "剩余量 ${remaining}吨 小于车队任何一辆车的额定载重, 无法派车"
                return 1
            }
            if ($fits.Count -lt $ids.Count) { $candidates = $fits; continue }
            $sum = 0.0; $new = @()
            foreach ($v in $fits) {
                if (($sum + [double]$v.loadCapacity) -le $remaining) { $new += $v; $sum += [double]$v.loadCapacity }
            }
            if ($new.Count -eq 0) { $new = @($fits[0]) }
            if ($new.Count -ge $ids.Count) {
                Log "自动缩减后仍无法满足载重约束: $($cap.msg)"
                return 1
            }
            Log "自动缩减车辆数量: $($ids.Count) → $($new.Count)"
            $candidates = $new
            continue
        }
        if ($cap.code -ne 0) {
            Log "容量校验异常: $($cap.msg)"
            return 1
        }

        # 校验通过: 用服务器增强后的车辆数据组装 vehicleStr
        $rows = @($cap.data)
        $vsParts = @()
        $weightSum = 0.0
        foreach ($r in $rows) {
            $vsParts += ('{0},{1},{2},{3},{4},{5},{6},' -f $r.id, $r.plateNo, $r.vehicleType, $r.loadCapacity, $r.driverId, $r.driverName, $r.driverPhone)
            $weightSum += [double]$r.loadCapacity
        }
        $vehicleStr = $vsParts -join ';'
        Log "容量校验通过, 本次共 $($rows.Count) 辆车, 总载重 $weightSum 吨"

        # 组装 batchAdd 参数 (与网页提交完全一致)
        $parts = New-Object 'System.Collections.Generic.List[string]'
        function Add-Pair { param($k, $v) $parts.Add($k + '=' + (E ([string]$v))) }
        Add-Pair 'carrierId' $carrierId
        Add-Pair 'lossCoefficient' $lossStr
        Add-Pair 'fromSource' 'add'
        Add-Pair 'carrierName' $carrierName
        foreach ($r in $rows) {
            Add-Pair 'driverName' $r.driverName
            Add-Pair 'driverId' $r.driverId
            Add-Pair 'platformName' ''
        }
        Add-Pair 'selectVehicleIds' ($ids -join ',')
        Add-Pair 'quoteIdHidden' $quoteId
        Add-Pair 'planLoadingTime' $planLoadingTime
        Add-Pair 'planDepartTime' ''
        Add-Pair 'planArriveTime' $planArriveTime
        Add-Pair 'remark' ''
        Add-Pair 'customerLineId' ''
        Add-Pair 'locationId' ''
        Add-Pair 'enableGoStationName' $enableGoStation
        Add-Pair 'tramFlag' $tramFlag
        Add-Pair 'feeStr' ''
        Add-Pair 'vehicleStr' $vehicleStr
        Add-Pair 'quoteId' $quoteId
        Add-Pair 'weight' $weightSum
        if ($hasIsToday) { Add-Pair 'isToday' $isTodayVal }
        $payload = $parts -join '&'

        if ($DryRun) {
            Log "[DryRun] 已跳过提交, 模拟请求参数(前400字):"
            Log $payload.Substring(0, [Math]::Min(400, $payload.Length))
            return 0
        }

        Log "正在提交派车..."
        $bj = ConvertTo-JsonObj (Invoke-PostForm "$script:Base/busi/tms/shipment/batchAdd" $payload $script:RefDispatch) '派车提交'
        if ($bj.code -eq 0) {
            Log "服务器返回: $($bj.msg)"
            Log '========== 抢单成功 =========='
            Log "账号: $Username"
            Log "订单号: $OrderCode"
            Log "车队: $FleetName"
            Log "日期: $Date"
            Log "抢到车辆数: $($rows.Count) 辆"
            Log "总载重: $weightSum 吨"
            Log '提示: 派车单已创建(初始化状态), 请到【派车单管理】中确认/审核'
            return 0
        }
        $failMsg = [string]$bj.msg
        Log "派车未成功: $failMsg"
        # 情况二: 可承运量不足 → 解析最多可派数量并重试
        $mv = [regex]::Match($failMsg, '最多只[可能]继续派车【(\d+)】')
        if ($mv.Success) {
            $avail = [int]$mv.Groups[1].Value
            Log "今日可继续派车余量: $avail"
            if ($avail -le 0) {
                Log '今日已无可派数量, 抢单失败'
                return 1
            }
            if ($avail -lt $rows.Count) {
                Log "调整为最多 $avail 辆后重新提交..."
                $candidates = @($rows | Select-Object -First $avail)
                continue
            }
            Log "可派余量 $avail 不小于当前车辆数, 但提交仍失败, 请重试"
            return 1
        }
        Log "无法识别失败原因, 抢单失败"
        return 1
    }

    Log '重试次数用尽, 抢单失败'
    return 1
}

# 参数缺失时交互式输入
if (-not $Username)  { $Username = Read-Host '请输入登录账号' }
if (-not $Password)  { $Password = Read-Host '请输入登录密码' }
if (-not $OrderCode) { $OrderCode = Read-Host '请输入需要抢单的订单号' }
if (-not $FleetName) { $FleetName = Read-Host '请输入抢单的车队名称' }

$code = Main

if ($code -eq 0) {
    Log '========== 最终结果: 抢单成功 =========='
} else {
    Log '========== 最终结果: 抢单失败 =========='
}
exit $code