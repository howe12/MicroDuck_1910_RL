"""Page permissions derived from measured service state, never browser routing."""
import math

from telemetry import IDS, NAMES


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def numbered_chain(snapshot):
    rows = snapshot.get('servos') or []
    return (snapshot.get('service_sample_fresh') is True
            and len(rows) == len(IDS) and {r.get('id') for r in rows} == set(IDS)
            and all(r.get('status') == 'live' and type(r.get('position_raw')) is int for r in rows)
            and (snapshot.get('imu') or {}).get('status') == 'live')


def numbered_scan(maintenance):
    """Use the completed user-requested scan; never run another chain check."""
    scan=maintenance.get('scan') or {}
    if (scan.get('completed') is not True or scan.get('running') is True
            or scan.get('requires_rescan') is True or scan.get('conflict_ids')):
        return False
    rows=maintenance.get('devices') or []
    for ident in (*IDS,200):
        found=[row for row in rows if row.get('id')==ident]
        if len(found)!=1:return False
        row=found[0]
        if (row.get('identity_verified') is not True or row.get('error') or row.get('suspected_duplicate')
                or row.get('kind')!=('imu' if ident==200 else 'servo')
                or ident!=200 and row.get('register_id')!=ident):return False
    return True


def calibrated(snapshot):
    report = snapshot.get('calibration') or {}
    data = report.get('calibration') or {}
    rows = data.get('joints') or []
    quat = data.get('imu_mount_quat')
    if (data.get('imu_mount_verified') is not True or not isinstance(quat, list)
            or len(quat) != 4 or not all(finite(v) for v in quat)
            or abs(sum(v*v for v in quat)-1) > 1e-6
            or len(rows) != len(IDS)):
        return False
    for row, sid, name in zip(rows, IDS, NAMES):
        if (row.get('id') != sid or row.get('name') != name or row.get('calibrated') is not True
                or type(row.get('zero_raw')) is not int or not 0 <= row['zero_raw'] <= 4095
                or type(row.get('direction')) is not int or row['direction'] not in (-1, 1)
                or not finite(row.get('min_rad')) or not finite(row.get('max_rad'))
                or row['min_rad'] >= row['max_rad']):
            return False
    return True


def page_permissions(snapshot, maintenance, connected=True, pending=False):
    """A stopped/unknown loop is never evidence that the serial port is free."""
    bus = snapshot.get('bus') or {}
    active = maintenance.get('active') is True
    own = maintenance.get('owned', True) is not False
    stopped = maintenance.get('robotd_running') is False
    serial_free = maintenance.get('serial_available') is True
    maintenance_available = maintenance.get('available') is True
    mode = maintenance.get('mode')
    chain = numbered_chain(snapshot)
    calibration = calibrated(snapshot)
    common = '' if connected else '先连接 ZERO'
    if pending:
        common = '等待当前模式切换完成'
    pages = {}
    for page, wanted in (('numbering', 'numbering'), ('hardware', 'hardware-test')):
        reason = common
        if not reason:
            if active:
                reason = '' if own and mode == wanted else '先退出当前维护测试'
            elif not maintenance_available:
                reason = maintenance.get('error') or '先安装本版配套固件'
            elif not stopped:
                reason = '先停止机器人总线服务'
            elif not serial_free:
                reason = '串口仍被其他程序占用'
        # Navigation always offers these independent bench entry flows. Actual
        # session/packet permissions remain represented by allowed/can_start.
        pages[page] = {'allowed': not bool(reason), 'can_start': not bool(reason),
                       'browsable': True, 'reason': reason}
    reason = common or ('先退出维护模式' if active else '')
    if not reason and bus.get('mode') != 'commissioning':
        reason = '先切换到标定模式'
    pages['calibration'] = {'allowed': not bool(reason), 'browsable': True, 'reason': reason}
    reason = common or ('先退出维护模式' if active else '')
    if not reason and bus.get('mode') != 'motion':
        reason = '先切换到运动模式'
    if not reason and not calibration:
        reason = '先完成关节与 IMU 标定'
    if not reason and not chain:
        reason = '等待整机实时反馈'
    pages['robot'] = {'allowed': not bool(reason), 'browsable': True, 'reason': reason}
    return pages


def maintenance_active(store):
    workspace = getattr(store, 'workspace', None)
    return bool(workspace and workspace.busy_or_active())
