#!/usr/bin/env python3
"""Baltic Host: 常规每 4 天检查，最后 4 天或失败时按定时兜底重查。

can_renew 为 true 就提交续期；实际到期时间必须变晚才算成功。
无论是否能续，都检查电源状态，未运行则尝试启动并复查。
不会自派发；站点风控、无法续期或状态不明均报失败。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import blinky_probe as bp

SHORT = os.environ.get('BLINKY_SHORT_ID', 'f766593e')
FULL = os.environ.get('BLINKY_SERVER_UUID', 'f766593e-30c4-4f34-9f53-5337f0e3db33')
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'blinky_state.json')
FOUR_DAYS = 4 * 24 * 60 * 60


def log(message):
    print(message, flush=True)


def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_check_due(state, now=None, scheduled=True):
    if not scheduled:
        return True, '手动检查'
    now = time.time() if now is None else now
    try:
        last = float(state['last_checked'])
        lower_exp = float(state['expiry_lower_bound'])
        if last > now + 300 or last <= 0 or state.get('requires_retry') is True:
            return True, '状态异常或上轮失败，兜底检查'
        if now - last >= FOUR_DAYS:
            return True, '距上次检查已满 4 天'
        if lower_exp - now <= FOUR_DAYS:
            return True, '接近最后 4 天，兜底检查'
        return False, '未满 4 天且未接近最后 4 天，跳过站点请求'
    except (KeyError, TypeError, ValueError):
        return True, '无可靠历史状态，立即检查'


def save_state(days, failed):
    try:
        n = float(days)
        if n < -365 or n > 36500:
            raise ValueError
    except (ValueError, TypeError):
        n = 0
    now = time.time()
    # 不知 days 的取整方式，按少一天处理，提前进入兜底，不拖到过期。
    state = {'last_checked': now,
             'expiry_lower_bound': now + max(0, n - 1) * 86400,
             'requires_retry': bool(failed)}
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def xhdr(s, referer):
    token = ''
    page = s.get(f'{bp.BASE}{referer}', timeout=30)
    if page.status_code != 200:
        raise RuntimeError(f'CSRF 页面 HTTP {page.status_code}')
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', page.text)
    if m:
        token = m.group(1)
    return {'X-CSRF-Token': token, 'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json', 'Referer': f'{bp.BASE}{referer}'}


def read_renewal(s):
    h = xhdr(s, f'/manage/server/{SHORT}/')
    r = s.get(f'{bp.BASE}/manage/server/api/{SHORT}/renewal', headers=h, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'续期状态 HTTP {r.status_code}')
    data = r.json().get('data')
    if not isinstance(data, dict):
        raise RuntimeError('续期状态格式异常')
    return data


def state_of(s):
    r = s.get(f'{bp.BASE}/api/dashboard-data?all=true', timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'电源状态 HTTP {r.status_code}')
    for srv in r.json().get('all_servers') or []:
        if (srv.get('uuid_short') or '').startswith(SHORT):
            return srv.get('status') or '?', bool(srv.get('is_suspended'))
    raise RuntimeError('服务器不在面板列表中')


def ensure_running(s):
    try:
        status, suspended = state_of(s)
        log(f'运行状态: {status} suspended={suspended}')
        if status == 'running' and not suspended:
            log('✅ 已在运行')
            return True
    except Exception as exc:
        status = '?'
        log(f'⚠️ 运行状态读取失败: {type(exc).__name__}')
    if status != 'starting':
        try:
            h = xhdr(s, f'/manage/server/{SHORT}/')
            r = s.post(f'{bp.BASE}/manage/server/api/{FULL}/power',
                       json={'action': 'start'}, headers=h, timeout=60)
            log(f'启动请求: HTTP {r.status_code}')
        except Exception as exc:
            log(f'❌ 启动请求异常: {type(exc).__name__}')
    for i in range(12):
        time.sleep(10)
        try:
            status, suspended = state_of(s)
            log(f'启动复查 {i+1}: {status} suspended={suspended}')
            if status == 'running' and not suspended:
                log('✅ 已确认服务器 running')
                return True
        except Exception as exc:
            log(f'⚠️ 启动复查异常: {type(exc).__name__}')
    log('❌ 未确认服务器达到 running')
    return False


def parse_expiry(value):
    if not isinstance(value, str):
        return None
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def main():
    due, reason = is_check_due(load_state(), scheduled=os.environ.get('BLINKY_SCHEDULED') == '1')
    log(f'检查安排: {reason}')
    if not due:
        return 0
    try:
        s = bp.session()
        ok, msg = bp.login(s)
        if not ok and bp.blocked(msg):
            proxies = bp.start_xray()
            if proxies:
                s = bp.session(proxies)
                ok, msg = bp.login(s)
        log(f"登录: {'✅' if ok else '❌'} {msg[:120]}")
    except Exception as exc:
        log(f'❌ 登录异常: {type(exc).__name__}')
        return 2
    if not ok:
        return 2

    failed = False
    before = None
    current = None
    try:
        before = read_renewal(s)
        current = before
        can = before.get('can_renew')
        days = before.get('days_until_expiration')
        old_exp = before.get('expiration_date')
        log(f'续期状态: can_renew={can} 剩余 {days} 天 到期 {old_exp}')
        if can is True:
            h = xhdr(s, f'/manage/server/{SHORT}/renewal')
            if not h['X-CSRF-Token']:
                raise RuntimeError('没有续期 CSRF token')
            r = s.post(f'{bp.BASE}/manage/server/api/{SHORT}/renewal/renew',
                       data={'csrf_token': h['X-CSRF-Token']}, headers=h, timeout=60,
                       allow_redirects=False)
            log(f'续期请求: HTTP {r.status_code}（以到期时间复查为准）')
            time.sleep(3)
            current = read_renewal(s)
            new_exp = current.get('expiration_date')
            old_dt, new_dt = parse_expiry(old_exp), parse_expiry(new_exp)
            log(f'续期后到期: {old_exp} → {new_exp}')
            if old_dt is None or new_dt is None or new_dt <= old_dt:
                log('❌ 无法确认到期时间向后推移')
                failed = True
            else:
                log('✅ 到期时间已核实推迟')
        elif can is False:
            try:
                close = float(days) <= 5
            except (TypeError, ValueError):
                close = True
            if close:
                log('❌ 接近最后 4 天但站点不允许续期，将定时重试')
                failed = True
            else:
                log('⏳ 站点暂不允许续期')
        else:
            log('❌ 续期状态未知，不能当成不需要续期')
            failed = True
    except Exception as exc:
        log(f'❌ 续期检查或核验失败: {type(exc).__name__}: {str(exc)[:120]}')
        failed = True

    # 登录成功后，不管续期能否完成，都检查并尝试启动。
    if not ensure_running(s):
        failed = True
    if current is not None:
        try:
            save_state(current.get('days_until_expiration'), failed)
        except Exception as exc:
            log(f'❌ 检查状态保存失败: {type(exc).__name__}')
            failed = True
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
