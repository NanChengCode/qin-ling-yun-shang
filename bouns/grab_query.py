# -*- coding: utf-8 -*-
"""
秦岭云商 - 信息查询脚本 (Python 版)
功能: 候选订单列表 / 车队信息 / 车队车辆统计

用法示例:
    python grab_query.py --username 138xxxx --password 你的密码 \
        --action Orders --with-quota --out-file orders.json
    python grab_query.py --username 138xxxx --password 你的密码 \
        --action Fleets --out-file fleets.json
    python grab_query.py --username 138xxxx --password 你的密码 \
        --action FleetVehicles --fleet-id 5531 --out-file v.json
"""
import sys
import re
import json
import argparse

import grab_common as gc
from grab_common import Session, log, e, parse_json, login


def get_quota_info(html):
    """从批量派车页面 HTML 解析每日预约配额"""
    out = {'has': False, 'today': None, 'tomorrow': None, 'after': None}
    if 'id="isToday"' not in html:
        return out
    out['has'] = True
    tm = re.search(r'今日[\d\-]+ \((\d+)/(\d+)\)', html)
    tw = re.search(r'明日[\d\-]+ \((\d+)/(\d+)\)', html)
    ta = re.search(r'后日[\d\-]+ \((\d+)/(\d+)\)', html)
    if tm:
        out['today'] = tm.group(1) + '/' + tm.group(2)
    if tw:
        out['tomorrow'] = tw.group(1) + '/' + tw.group(2)
    if ta:
        out['after'] = ta.group(1) + '/' + ta.group(2)
    return out


def get_orders(sess, with_quota=False):
    """查询候选订单 (调度派车列表)"""
    log('正在查询候选订单列表...')
    q_body = ('quoteCustomerName=&locationId=&quoteCode='
              '&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc')
    lst = parse_json(sess.post_form(gc.BASE + '/busi/tms/shipment/listSupplyQuote',
                                    q_body, gc.REF_DISPATCH), '候选订单')
    rows = lst.get('rows') or []
    log('共查询到 %d 条候选订单' % len(rows))

    out = []
    for i, q in enumerate(rows):
        loss = ('null' if q.get('lossCoefficient') is None
                else str(q.get('lossCoefficient')))
        row = {
            'id': q.get('id'),
            'quoteCode': q.get('quoteCode'),
            'csrOrderCode': q.get('csrOrderCode'),
            'customerName': q.get('customerName'),
            'productName': q.get('productName'),
            'sender': q.get('sender'),
            'consignee': q.get('consignee'),
            'inCompleteWeight': q.get('inCompleteWeight'),
            'weight': q.get('weight'),
            'expectArrivedTime': q.get('expectArrivedTime'),
            'transportType': q.get('transportType'),
            'quotaHas': False,
            'quotaToday': None,
            'quotaTomorrow': None,
            'quotaAfter': None,
        }
        # 配额查询较慢, 仅前 20 条 + 仅在需要时
        if with_quota and i < 20:
            burl = (gc.BASE + '/busi/tms/shipment/batchDispatchVehicle?quoteIds=' +
                    str(q.get('id')) + '&vehicleId=&fromSource=add&lossCoefficient=' + e(loss))
            try:
                quota = get_quota_info(sess.get(burl, gc.REF_DISPATCH))
                row['quotaHas'] = quota['has']
                row['quotaToday'] = quota['today']
                row['quotaTomorrow'] = quota['tomorrow']
                row['quotaAfter'] = quota['after']
            except Exception as ex:
                log('订单 %s 配额查询跳过: %s' % (q.get('quoteCode'), ex))
        out.append(row)
    return out


def get_fleets(sess):
    """查询车队信息 (返回完整字段, 透传给浏览器)"""
    log('正在查询车队信息...')
    m_body = 'name=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    ml = parse_json(sess.post_form(gc.BASE + '/busi/base/motorcade/list',
                                   m_body, '/busi/base/motorcade'), '车队信息')
    rows = ml.get('rows') or []
    log('共查询到 %d 个车队' % len(rows))

    out = []
    for m in rows:
        out.append({
            'id': m.get('id'),
            'name': m.get('name'),
            'carrierId': m.get('carrierId'),
            'carrierName': m.get('carrierName'),
            'carCaptainName': m.get('carCaptainName'),
        })
    return out


def get_fleet_vehicles(sess, fleet_id):
    """查询车队车辆统计"""
    log('正在查询车队 [%s] 的车辆统计...' % fleet_id)
    m_body = 'name=&pageSize=500&pageNum=1&orderByColumn=&isAsc=asc'
    ml = parse_json(sess.post_form(gc.BASE + '/busi/base/motorcade/list',
                                   m_body, '/busi/base/motorcade'), '车队信息')
    rows = ml.get('rows') or []
    fleet = None
    for m in rows:
        if str(m.get('id')) == str(fleet_id):
            fleet = m
            break
    if not fleet:
        raise RuntimeError('未找到车队ID: ' + str(fleet_id))
    carrier_id = fleet.get('carrierId')

    lurl = (gc.BASE + '/busi/base/vehicle/batchList?carrierId=' + e(str(carrier_id)) +
            '&selectVehicle=true&selectVehicleIds=&locationId=')
    l_body = ('mdId=' + e(str(fleet_id)) +
              '&plateNo=&driverName=&state=&isTram=&issueVerifyStatus='
              '&roadCardVerifyStatus=&pageSize=1000&pageNum=1&orderByColumn=&isAsc=asc')
    vv = parse_json(sess.post_form(lurl, l_body, '/busi/base/motorcade'), '车队车辆')

    s10 = s20 = s30 = s40 = s_other = 0
    for v in (vv.get('rows') or []):
        state = str(v.get('state'))
        if state == '10':
            s10 += 1
        elif state == '20':
            s20 += 1
        elif state == '30':
            s30 += 1
        elif state == '40':
            s40 += 1
        else:
            s_other += 1
    log('车队 [%s] 共 %s 辆 (空闲%d 已派%d 排队%d 已发运%d)' %
        (fleet.get('name'), vv.get('total'), s10, s20, s30, s40))
    return {
        'fleetId': fleet_id,
        'fleetName': fleet.get('name'),
        'carrierId': carrier_id,
        'carrierName': fleet.get('carrierName'),
        'total': vv.get('total'),
        'state10': s10,
        'state20': s20,
        'state30': s30,
        'state40': s40,
        'stateOther': s_other,
    }


def run(args):
    sess = Session()
    if args.cookie:
        sess.set_cookie_text(args.cookie)
        log('已注入调试Cookie, 跳过登录')
    else:
        if not login(sess, args.username, args.password, args.sms_code, interactive=False):
            raise RuntimeError('登录失败')

    if args.action == 'Orders':
        data = get_orders(sess, with_quota=args.with_quota)
        return {'ok': True, 'action': 'Orders', 'rows': data}
    elif args.action == 'Fleets':
        data = get_fleets(sess)
        return {'ok': True, 'action': 'Fleets', 'rows': data}
    elif args.action == 'FleetVehicles':
        if not args.fleet_id:
            raise RuntimeError('FleetVehicles 需要 --fleet-id 参数')
        data = get_fleet_vehicles(sess, args.fleet_id)
        return {'ok': True, 'action': 'FleetVehicles', 'data': data}
    else:
        raise RuntimeError('未知 action: ' + args.action)


def main():
    parser = argparse.ArgumentParser(description='秦岭云商信息查询')
    parser.add_argument('--username', default='')
    parser.add_argument('--password', default='')
    parser.add_argument('--sms-code', default='')
    parser.add_argument('--cookie', default='',
                        help='调试用: 直接注入登录Cookie, 跳过登录')
    parser.add_argument('--action', choices=['Orders', 'Fleets', 'FleetVehicles'],
                        default='Orders')
    parser.add_argument('--fleet-id', default='')
    parser.add_argument('--with-quota', action='store_true')
    parser.add_argument('--out-file', default='query_result.json')
    args = parser.parse_args()

    try:
        payload = run(args)
        with open(args.out_file, 'w', encoding='utf-8') as f:
            # ensure_ascii=False: 中文不转义; 原样透传, 字段不丢失
            json.dump(payload, f, ensure_ascii=False)
        log('结果已写入: ' + args.out_file)
        sys.exit(0)
    except Exception as ex:
        log('查询失败: ' + str(ex))
        try:
            with open(args.out_file, 'w', encoding='utf-8') as f:
                json.dump({'ok': False, 'msg': str(ex)}, f, ensure_ascii=False)
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
