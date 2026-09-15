# -*- coding: utf-8 -*-
"""
秦岭云商 自动抢单工具 (Python 版)
直接调用网页接口, 无需浏览器

用法示例:
    python grab_order.py --username 138xxxx --password 你的密码 \
        --order-code SG260826133791-01 --fleet-name 发发发 --date 今日

可选参数:
    --max-vehicle N   最多选取 N 辆车(默认不限, 抢整个车队)
    --sms-code xxx     短信二次验证码(账号开启2FA时需要)
    --dry-run          只走流程不提交(调试用, 测试模式)
"""
import sys
import re
import time
import argparse

import grab_common as gc
from grab_common import Session, log, e, rsa_encrypt, parse_json, login

REF_DISPATCH = gc.REF_DISPATCH
REF_VEHICLE = gc.REF_VEHICLE


def get_page_value(html, eid):
    """从 HTML 解析 <input id="xxx" value="yyy"> 的 value"""
    m = re.search(r'id="' + re.escape(eid) + r'"[^>]*', html)
    if not m:
        return None
    mv = re.search(r'value="([^"]*)"', m.group(0))
    return mv.group(1) if mv else ''


def run(args):
    """抢单主流程, 返回退出码 (0=成功, 1=失败)"""
    log('========== 秦岭云商自动抢单工具启动 ==========')
    log('订单号: %s | 车队: %s | 目标日期: %s | DryRun: %s' %
        (args.order_code, args.fleet_name, args.date, args.dry_run))

    sess = Session()

    # 1. 登录 (或注入共享Cookie跳过登录)
    if args.cookie:
        sess.set_cookie_text(args.cookie)
        log('已注入共享Cookie, 跳过登录')
    elif not login(sess, args.username, args.password, args.sms_code, interactive=True):
        return 1

    # 2. 按订单号查询订单 (支持轮询)
    log('正在查询订单 [%s] ...' % args.order_code)
    q_body = ('quoteCustomerName=&locationId=&quoteCode=' + e(args.order_code) +
              '&pageSize=10&pageNum=1&orderByColumn=&isAsc=asc')
    rows = None
    poll_deadline = time.time() + args.poll_timeout if args.poll_timeout > 0 else 0
    poll_n = 0
    while True:
        q = parse_json(sess.post_form(gc.BASE + '/busi/tms/shipment/listSupplyQuote',
                                     q_body, REF_DISPATCH), '订单查询')
        rows = q.get('rows') or []
        if rows:
            break
        if args.poll_timeout <= 0:
            log('未找到订单号 [%s], 请确认订单号是否正确' % args.order_code)
            return 1
        if time.time() >= poll_deadline:
            log('轮询超时(%ds), 订单 [%s] 仍未出现' % (args.poll_timeout, args.order_code))
            return 1
        poll_n += 1
        # 高频轮询下日志节流: 前3次 + 每5秒一次, 避免刷屏
        if poll_n <= 3 or poll_n % 5 == 0:
            log('订单 [%s] 尚未出现, %ds后重试... (剩余%ds)' %
                (args.order_code, args.poll_interval, int(poll_deadline - time.time())))
        time.sleep(args.poll_interval)
    quote = rows[0]
    quote_id = quote['id']
    loss_str = 'null' if quote.get('lossCoefficient') is None else str(quote['lossCoefficient'])
    # 剩余量直接取订单列表响应 (原单独订单详情请求为冗余, 已移除; 需要权威值时热循环内会重查)
    remaining = _to_float(quote.get('inCompleteWeight'))
    log('找到订单: id=%s, 剩余发货量=%s吨, 货品=%s' %
        (quote_id, remaining, quote.get('productName')))

    # 3. 加载批量派车页面, 解析表单隐藏字段
    burl = (gc.BASE + '/busi/tms/shipment/batchDispatchVehicle?quoteIds=' + str(quote_id) +
            '&vehicleId=&fromSource=add&lossCoefficient=' + e(loss_str))
    bhtml = sess.get(burl, REF_DISPATCH)
    carrier_id = get_page_value(bhtml, 'carrierId')
    carrier_name = get_page_value(bhtml, 'carrierName')
    plan_loading_time = get_page_value(bhtml, 'planLoadingTime')
    plan_arrive_time = get_page_value(bhtml, 'planArriveTime')
    enable_go_station = get_page_value(bhtml, 'enableGoStationName')
    order_location_id = get_page_value(bhtml, 'orderLocationId')
    tram_flag = get_page_value(bhtml, 'tramFlagHidden')
    if carrier_id is None:
        log('解析批量派车页面失败(缺少carrierId), 可能页面结构变化')
        return 1
    log('承运商: %s (id=%s)' % (carrier_name, carrier_id))

    # 计划预约日期: 仅当页面包含该区块时提交 isToday
    has_is_today = 'id="isToday"' in bhtml
    is_today_val = {'今日': '1', '明日': '2', '后日': '3'}.get(args.date, '1')
    if has_is_today:
        tm = re.search(r'今日([\d\-]+) \((\d+)/(\d+)\)', bhtml)
        tw = re.search(r'明日([\d\-]+) \((\d+)/(\d+)\)', bhtml)
        ta = re.search(r'后日([\d\-]+) \((\d+)/(\d+)\)', bhtml)
        log('计划预约日期配额: 今日(剩%s/%s) 明日(剩%s/%s) 后日(剩%s/%s)' % (
            tm.group(2) if tm else '?', tm.group(3) if tm else '?',
            tw.group(2) if tw else '?', tw.group(3) if tw else '?',
            ta.group(2) if ta else '?', ta.group(3) if ta else '?'))
        log('本次选择日期: %s (isToday=%s)' % (args.date, is_today_val))
    else:
        log('该订单不需要计划预约日期')

    # 4. 车队名称 → 车队ID (解析车辆选择窗口页面的下拉选项)
    vurl = (gc.BASE + '/busi/base/vehicle/batchSearchList?carrierId=' + e(carrier_id) +
            '&selectVehicleIds=&locationId=' + e(order_location_id) +
            '&tramFlag=' + e(tram_flag))
    vhtml = sess.get(vurl, REF_DISPATCH)
    fm = re.search(r'<option value="(\d+)">' + re.escape(args.fleet_name) + r'</option>', vhtml)
    if not fm:
        log('未找到车队 [%s]' % args.fleet_name)
        all_fleets = sorted(set(re.findall(r'<option value="\d+">([^<]+)</option>', vhtml)))
        log('当前承运商下车队列表: ' + ', '.join(all_fleets))
        return 1
    md_id = fm.group(1)
    log('车队 [%s] → ID=%s' % (args.fleet_name, md_id))

    # 5. 查询车队全部车辆
    lurl = (gc.BASE + '/busi/base/vehicle/batchList?carrierId=' + e(carrier_id) +
            '&selectVehicle=true&selectVehicleIds=&locationId=' + e(order_location_id))
    l_body = ('mdId=' + e(md_id) +
              '&plateNo=&driverName=&state=&isTram=&issueVerifyStatus='
              '&roadCardVerifyStatus=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc')
    lv = parse_json(sess.post_form(lurl, l_body, REF_VEHICLE), '车队车辆查询')
    vehicles = lv.get('rows') or []
    if not vehicles:
        log('车队 [%s] 没有可选车辆' % args.fleet_name)
        return 1
    log('车队共 %s 辆车 (可选 %d 辆)' % (lv.get('total'), len(vehicles)))

    # 车辆份额拆分: 同账号同订单同车队的多个任务各占一份互不重叠的子集
    # (按下标取模交错选取, 大车/小车在各份额间分布更均匀; 各任务仍各自受容量校验约束)
    if args.share_total > 1:
        share = [v for j, v in enumerate(vehicles) if j % args.share_total == args.share_index]
        if not share:
            log('本任务分到的车辆份额为空 (第 %d/%d 份)' % (args.share_index + 1, args.share_total))
            return 1
        vehicles = share
        log('车辆份额拆分: 本任务为第 %d/%d 份, 分配到 %d 辆 (互不重叠)' %
            (args.share_index + 1, args.share_total, len(vehicles)))

    if args.max_vehicle > 0 and args.max_vehicle < len(vehicles):
        vehicles = vehicles[:args.max_vehicle]
        log('按 --max-vehicle 限制, 仅使用前 %d 辆' % args.max_vehicle)

    candidates = list(vehicles)
    attempt = 0
    empty_retry_start = 0  # 进入"无可派"轮询的起始时间戳(0=尚未进入)
    retry_count = 0  # 轮询总次数计数器(独立于 attempt, 不会被重置)

    def _retry_wait(reason):
        """持续轮询等待: 睡眠 retry_interval 并做超时控制, 返回 True 继续 / False 放弃(超时)。
        纯等待函数, 不修改 candidates, 供快速提交阶段"原参数直接重试"复用"""
        nonlocal empty_retry_start, remaining, retry_count
        # 检查总超时
        if args.retry_timeout > 0 and empty_retry_start > 0:
            elapsed = time.time() - empty_retry_start
            if elapsed >= args.retry_timeout:
                log('>>>>> %s, 已持续重试 %.0fs 超过上限 %ds, 放弃 >>>>>' %
                    (reason, elapsed, args.retry_timeout))
                return False
        if empty_retry_start == 0:
            empty_retry_start = time.time()
            retry_count = 0
            log('>>>>> 进入【%s】持续轮询模式 (间隔 %.3fs, 超时 %ds) >>>>>' %
                (reason, args.retry_interval, args.retry_timeout))
        retry_count += 1
        # 高频轮询模式下不必每次都查询网络, 直接尝试提交
        # 仅每 20 次循环(约 3s)查询一次剩余量, 用于日志显示
        if retry_count % 20 == 0 or retry_count == 1:
            try:
                dj_r = parse_json(sess.post_form(gc.BASE +
                                '/busi/tms/shipment/selectQuoteListByID?selectQuoteIds=' + str(quote_id),
                                '', REF_DISPATCH), '订单详情(轮询)')
                dj_rows = dj_r.get('rows') or []
                if dj_rows:
                    try:
                        remaining = float(dj_rows[0].get('inCompleteWeight') or 0)
                    except (TypeError, ValueError):
                        pass
            except Exception as ex:
                log('轮询查询剩余量异常: %s' % str(ex)[:80])
        elapsed = time.time() - empty_retry_start
        if retry_count % 20 == 0 or retry_count <= 3:
            log('  [%s] 第%d次 当前剩余 %s 吨, %.3fs 后重试... (累计 %.1fs/%ds)' %
                (reason, retry_count, remaining, args.retry_interval,
                 elapsed, args.retry_timeout))
        time.sleep(args.retry_interval)
        return True

    def _do_empty_retry(reason):
        """处理"今日可派车数为0/剩余量不足"情况: 返回 True 表示继续重试, False 表示放弃。
        等待后重置缩减状态, 让容量校验从头重新跑一遍(期间剩余量可能已增长)"""
        if not _retry_wait(reason):
            return False
        nonlocal attempt
        # 重置缩减尝试计数, 让容量缩减逻辑重新跑一遍
        attempt = 0
        # 恢复 candidates 为全部车辆, 避免一直用缩减后的小集合
        candidates[:] = list(vehicles)
        return True

    def _build_payload(rows):
        """由服务器增强后的车辆行组装提交参数, 返回 (ids, md_ids, vehicle_str, weight_sum, payload)"""
        ids = [str(r.get('id')) for r in rows]
        md_ids = [str(r.get('mdId')) for r in rows]
        vs_parts = []
        weight_sum = 0.0
        for r in rows:
            vs_parts.append('%s,%s,%s,%s,%s,%s,%s,' % (
                r.get('id'), r.get('plateNo'), r.get('vehicleType'),
                r.get('loadCapacity'), r.get('driverId'),
                r.get('driverName'), r.get('driverPhone')))
            weight_sum += _to_float(r.get('loadCapacity'))
        vehicle_str = ';'.join(vs_parts)
        # 组装 batchAdd 参数 (与网页提交完全一致)
        pairs = []
        pairs.append(('carrierId', carrier_id))
        pairs.append(('lossCoefficient', loss_str))
        pairs.append(('fromSource', 'add'))
        pairs.append(('carrierName', carrier_name))
        for r in rows:
            pairs.append(('driverName', r.get('driverName') or ''))
            pairs.append(('driverId', r.get('driverId') or ''))
            pairs.append(('platformName', ''))
        pairs.append(('selectVehicleIds', ','.join(ids)))
        pairs.append(('quoteIdHidden', quote_id))
        pairs.append(('planLoadingTime', plan_loading_time))
        pairs.append(('planDepartTime', ''))
        pairs.append(('planArriveTime', plan_arrive_time))
        pairs.append(('remark', ''))
        pairs.append(('customerLineId', ''))
        pairs.append(('locationId', ''))
        pairs.append(('enableGoStationName', enable_go_station))
        pairs.append(('tramFlag', tram_flag))
        pairs.append(('feeStr', ''))
        pairs.append(('vehicleStr', vehicle_str))
        pairs.append(('quoteId', quote_id))
        pairs.append(('weight', weight_sum))
        if has_is_today:
            pairs.append(('isToday', is_today_val))
        payload = '&'.join('%s=%s' % (k, e(v)) for k, v in pairs)
        return ids, md_ids, vehicle_str, weight_sum, payload

    # 6. 校验+提交 主循环 (容量不足自动缩减重试 + 无可派轮询重试; 校验通过后进入快速提交阶段)
    while True:
        attempt += 1
        ids = [str(v.get('id')) for v in candidates]
        md_ids = [str(v.get('mdId')) for v in candidates]
        log('---- 第 %d 次尝试: 拟派 %d 辆车 ----' % (attempt, len(ids)))

        ck_body = ('vehicleIds=' + e(','.join(ids)) +
                   '&mdIds=' + e(','.join(md_ids)) +
                   '&quoteIds=' + str(quote_id))

        # 已取消黑名单校验: 直接进入容量校验 (2026-09 按需优化)
        cap = parse_json(sess.post_form(gc.BASE +
                         '/busi/tms/shipment/checkVehicleCapacity',
                         ck_body, REF_DISPATCH), '容量校验')
        cap_data = cap.get('data')
        if isinstance(cap_data, str) and cap_data == 'false':
            # 重新查询订单当前剩余量 (可能被其他任务消耗了)
            dj2 = parse_json(sess.post_form(gc.BASE +
                            '/busi/tms/shipment/selectQuoteListByID?selectQuoteIds=' + str(quote_id),
                            '', REF_DISPATCH), '订单详情(重查)')
            dj2_rows = dj2.get('rows') or []
            if dj2_rows:
                try:
                    remaining = float(dj2_rows[0].get('inCompleteWeight') or 0)
                except (TypeError, ValueError):
                    pass
            log('容量校验未通过: %s (当前剩余 %s 吨)' % (cap.get('msg'), remaining))
            # 缩减候选: 1) 只保留额定载重 <= 剩余量的车  2) 贪心累计不超过剩余量
            fits = [v for v in vehicles if _to_float(v.get('loadCapacity')) <= remaining]
            if not fits:
                # 剩余量小于任何一辆车的额定载重 → 进入持续轮询
                log('剩余量 %s吨 小于车队任何一辆车的额定载重, 无法派车' % remaining)
                if not _do_empty_retry('剩余量不足'):
                    return 1
                continue
            if len(fits) < len(ids):
                candidates = fits
                continue
            new_cands = []
            total = 0.0
            for v in fits:
                lc = _to_float(v.get('loadCapacity'))
                if total + lc <= remaining:
                    new_cands.append(v)
                    total += lc
            if not new_cands:
                new_cands = [fits[0]]
            if len(new_cands) >= len(ids):
                log('自动缩减后仍无法满足载重约束: %s' % cap.get('msg'))
                if not _do_empty_retry('容量不足缩减失败'):
                    return 1
                continue
            log('自动缩减车辆数量: %d → %d' % (len(ids), len(new_cands)))
            candidates = new_cands
            continue
        if cap.get('code') != 0:
            log('容量校验异常: %s' % cap.get('msg'))
            if not _do_empty_retry('容量校验异常'):
                return 1
            continue

        # 校验通过: 用服务器增强后的车辆数据组装提交参数并缓存
        cap_rows = cap_data if isinstance(cap_data, list) else []
        ids, md_ids, vehicle_str, weight_sum, payload = _build_payload(cap_rows)
        log('容量校验通过, 本次共 %d 辆车, 总载重 %s 吨' % (len(cap_rows), weight_sum))

        if args.dry_run:
            log('[DryRun] 已跳过提交, 模拟请求参数(前400字):')
            log(payload[:400])
            return 0

        # ---- 快速提交阶段: 参数不变则不再重复容量校验, 直接连续提交 (每 150ms 一发) ----
        unrec_n = 0  # 连续无法识别失败次数: 超过阈值说明参数已过时(如剩余量被其他任务消耗), 回退容量校验
        while True:
            log('正在提交派车...')
            bj = parse_json(sess.post_form(gc.BASE +
                            '/busi/tms/shipment/batchAdd',
                            payload, REF_DISPATCH), '派车提交')
            if bj.get('code') == 0:
                log('服务器返回: %s' % bj.get('msg'))
                log('========== 抢单成功 ==========')
                log('账号: %s' % args.username)
                log('订单号: %s' % args.order_code)
                log('车队: %s' % args.fleet_name)
                log('日期: %s' % args.date)
                log('抢到车辆数: %d 辆' % len(cap_rows))
                log('总载重: %s 吨' % weight_sum)
                log('提示: 派车单已创建(初始化状态), 请到【派车单管理】中确认/审核')
                return 0

            fail_msg = str(bj.get('msg'))
            log('派车未成功: %s' % fail_msg)
            # 可承运量不足 → 解析最多可派数量, 就地缩减参数后直接再提交 (省一次容量校验往返)
            mv = re.search(r'最多只[可能]继续派车【(\d+)】', fail_msg)
            if mv:
                unrec_n = 0
                avail = int(mv.group(1))
                log('今日可继续派车余量: %d' % avail)
                if avail <= 0:
                    # 今日已无可派数量 → 进入持续轮询, 等待配额刷新 (跳出快速提交, 重走容量校验)
                    log('今日已无可派数量, 等待配额刷新...')
                    if not _do_empty_retry('今日可派车数为0'):
                        return 1
                    break
                if avail < len(cap_rows):
                    log('调整为最多 %d 辆后重新提交...' % avail)
                    cap_rows = cap_rows[:avail]
                    ids, md_ids, vehicle_str, weight_sum, payload = _build_payload(cap_rows)
                    continue
                log('可派余量 %d 不小于当前车辆数, 但提交仍失败, 进入轮询重试' % avail)
                if not _do_empty_retry('提交失败(余量充足但被拒)'):
                    return 1
                break
            # 无法识别失败原因: 先按原参数快速重试几次(瞬时故障常自愈), 连续失败则回退容量校验,
            # 按实时剩余量重新缩减 — 防止剩余量已被其他任务/账号消耗后旧参数一直撞墙
            unrec_n += 1
            if unrec_n >= 3:
                log('连续 %d 次无法识别失败, 参数可能已过时, 回退容量校验按实时剩余量重新评估...' % unrec_n)
                if not _do_empty_retry('失败原因: ' + fail_msg[:40]):
                    return 1
                break
            log('无法识别失败原因 [%s], 原参数直接重试 (%d/3)...' % (fail_msg, unrec_n))
            if not _retry_wait('失败原因: ' + fail_msg[:40]):
                return 1
            continue


def _to_float(v):
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def main():
    parser = argparse.ArgumentParser(description='秦岭云商自动抢单工具')
    parser.add_argument('--username', required=False)
    parser.add_argument('--password', required=False)
    parser.add_argument('--cookie', default='',
                        help='注入共享Cookie跳过登录, 格式: k=v; k2=v2')
    parser.add_argument('--order-code', required=False)
    parser.add_argument('--fleet-name', required=False)
    parser.add_argument('--date', choices=['今日', '明日', '后日'], default='今日')
    parser.add_argument('--sms-code', default='')
    parser.add_argument('--max-vehicle', type=int, default=0)
    parser.add_argument('--max-retry', type=int, default=6)
    parser.add_argument('--share-index', type=int, default=0,
                        help='车辆份额拆分序号(0起, 同账号同订单多任务由服务端传入)')
    parser.add_argument('--share-total', type=int, default=1,
                        help='车辆份额拆分数(>1 时本任务只抢第 share-index 份, 各份互不重叠)')
    parser.add_argument('--poll-interval', type=int, default=1,
                        help='订单轮询间隔(秒), 默认1秒')
    parser.add_argument('--poll-timeout', type=int, default=60,
                        help='订单轮询超时(秒), 默认60秒, 0=不轮询')
    parser.add_argument('--retry-interval', type=float, default=0.15,
                        help='无可派时持续轮询间隔(秒), 默认0.15秒(150ms)')
    parser.add_argument('--retry-timeout', type=int, default=900,
                        help='无可派时持续轮询总超时(秒), 默认900秒(15分钟), 超时自动停止')
    # 测试模式: dry_run 默认开启, 必须显式 --no-dry-run 才会真实下单
    parser.add_argument('--dry-run', dest='dry_run', action='store_true', default=True,
                        help='只走流程不提交(测试模式, 默认开启)')
    parser.add_argument('--no-dry-run', dest='dry_run', action='store_false',
                        help='关闭测试模式, 真实抢单(危险)')
    args = parser.parse_args()

    # 参数缺失时交互式输入 (有Cookie时跳过账号密码)
    if not args.cookie:
        if not args.username:
            args.username = input('请输入登录账号: ').strip()
        if not args.password:
            import getpass
            args.password = getpass.getpass('请输入登录密码: ')
    if not args.order_code:
        args.order_code = input('请输入需要抢单的订单号: ').strip()
    if not args.fleet_name:
        args.fleet_name = input('请输入抢单的车队名称: ').strip()

    code = run(args)

    if code == 0:
        log('========== 最终结果: 抢单成功 ==========')
    else:
        log('========== 最终结果: 抢单失败 ==========')
    sys.exit(code)


if __name__ == '__main__':
    main()
