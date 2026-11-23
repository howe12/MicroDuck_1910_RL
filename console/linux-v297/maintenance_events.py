"""Authenticated local IMU events, served from the PC's latest-sample cache."""
import json
import secrets
import time


def _serve_events(handler, config, workspace, *, snapshot_name, event_name, owns, key_for,
                  inactive_error, unavailable_error='当前模式不提供 IMU 实时数据', period=.04, heartbeat=1.0):
    """No remote call or hardware action occurs in this HTTP streaming loop."""
    host = handler.headers.get('Host', '')
    allowed = ('127.0.0.1', 'localhost', config.get('bind', '').lower())
    if host.split(':')[0].lower() not in allowed:
        return handler.reply({'error': 'Host 不在允许列表'}, 403)
    origin = handler.headers.get('Origin')
    if ((origin and origin != 'http://' + host)
            or handler.headers.get('Sec-Fetch-Site') == 'cross-site'):
        return handler.reply({'error': '禁止跨站读取维护数据'}, 403)
    token = config.get('control_token', '')
    if not token or not secrets.compare_digest(handler.headers.get('X-Control-Token', ''), token):
        return handler.reply({'error': '页面控制会话已过期，本次操作未执行，请更新页面会话后重新点击',
                              'code': 'control_session_expired', 'action_executed': False,
                              'retry': 'refresh_config_then_user_action'}, 403)
    snapshot = getattr(getattr(workspace, 'core', None), snapshot_name, None)
    if not callable(snapshot):
        return handler.reply({'error':unavailable_error}, 405)

    value = snapshot()
    if not owns(value):
        return handler.reply(inactive_error, 409)
    handler.close_connection = True
    handler.connection.settimeout(2)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.end_headers()
        last_key, last_send = None, 0.0
        while True:
            # Age alone does not manufacture new samples. Status transitions
            # still publish immediately, including stale/stop/ownership loss.
            key = key_for(value)
            now = time.monotonic()
            if key != last_key:
                data = 'event: '+event_name+'\ndata: ' + json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n\n'
                handler.wfile.write(data.encode('utf-8'))
                handler.wfile.flush()
                last_key, last_send = key, now
            elif now - last_send >= heartbeat:
                handler.wfile.write(b': heartbeat\n\n')
                handler.wfile.flush()
                last_send = now
            if not owns(value):
                break
            time.sleep(period)
            value = snapshot()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass


def serve_imu_events(handler, config, workspace, *, period=.04, heartbeat=1.0):
    def owns(value):
        return (value.get('active') is True and value.get('owned') is True
                and value.get('mode') == 'hardware-test' and value.get('imu_running') is True)
    def key(value):
        imu, stream = value.get('imu') or {}, value.get('stream') or {}
        return (value.get('active'),value.get('owned'),value.get('mode'),value.get('imu_running'),
                imu.get('sample_id'),imu.get('sequence'),imu.get('status'),imu.get('reason'),
                stream.get('state'),stream.get('generation'))
    return _serve_events(handler,config,workspace,snapshot_name='imu_stream_snapshot',event_name='imu',
        owns=owns,key_for=key,inactive_error={'error':'先进入板卡测试并开始读取 IMU','code':'imu_stream_inactive'},
        period=period,heartbeat=heartbeat)


def serve_position_events(handler, config, workspace, *, period=.02, heartbeat=1.0):
    def owns(value):
        return value.get('active') is True and value.get('owned') is True and value.get('mode') == 'numbering'
    def key(value):
        stream = value.get('position_stream') or {}
        return (value.get('active'),value.get('owned'),value.get('session_id'),
                stream.get('generation'),stream.get('revision'),stream.get('sequence'),
                stream.get('state'),stream.get('error'))
    return _serve_events(handler,config,workspace,snapshot_name='position_stream_snapshot',event_name='positions',
        owns=owns,key_for=key,inactive_error={'error':'先进入编号并完成扫描','code':'position_stream_inactive'},
        unavailable_error='当前模式不提供编号实时位置',
        period=period,heartbeat=heartbeat)
