# -*- coding: utf-8 -*-
"""
الخادم الخلفي (Flask) لتطبيق إدارة أجهزة البصمة.
تصميم API على شكل JSON بحيث تتفاعل الواجهة (index.html) معه عبر AJAX
بدون إعادة تحميل الصفحة بالكامل في كل عملية.
"""

from flask import Flask, jsonify, request, Response, send_from_directory
from datetime import datetime, timedelta
from zk import ZK
import os
import io
import csv
import json
import re
import socket
import uuid
import threading
import time
from urllib.parse import quote

app = Flask(__name__, static_folder=None)

BASE_DIR = os.path.expanduser("~/biometric_project")

DEVICES_FILE = "devices.json"
STATE_FILE = "state.json"
HISTORY_FILE = "history.txt"
SCHEDULE_FILE = "schedule.json"
NOTIFICATIONS_FILE = "notifications.json"
SETTINGS_FILE = "settings.json"

MAX_HISTORY_LINES = 200
MAX_NOTIFICATIONS = 100
CONNECT_TIMEOUT = 1.5  # فحص اتصال سريع (ثوانٍ)
ZK_TIMEOUT = 5

# ---------------------------------------------------------------------------
# قفل مستقل لكل جهاز (بعنوان IP) - يمنع تعارض أكثر من اتصال بروتوكول واحد
# في نفس اللحظة على نفس الجهاز (مثلاً: مزامنة مجدولة + مزامنة يدوية في نفس
# الثانية)، وهو خطر حقيقي أصبح ممكنًا بعد تفعيل threaded=True في الخادم.
# أجهزة ZK تدعم جلسة اتصال واحدة فعّالة فقط في نفس الوقت.
# ---------------------------------------------------------------------------
_device_locks = {}
_device_locks_guard = threading.Lock()


def get_device_lock(ip):
    with _device_locks_guard:
        if ip not in _device_locks:
            _device_locks[ip] = threading.Lock()
        return _device_locks[ip]


# ---------------------------------------------------------------------------
# أدوات عامة لتخزين JSON
# ---------------------------------------------------------------------------

def _path(name):
    return os.path.join(BASE_DIR, name)


def _load_json(name, default):
    path = _path(name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(name, data):
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# الأجهزة
# ---------------------------------------------------------------------------

def load_devices():
    devices = _load_json(DEVICES_FILE, None)
    if devices is None:
        devices = []
        _save_json(DEVICES_FILE, devices)
    return devices


def save_devices(devices):
    _save_json(DEVICES_FILE, devices)


def load_state():
    return _load_json(STATE_FILE, {"active_device_id": None})


def save_state(state):
    _save_json(STATE_FILE, state)


def get_device_by_id(device_id):
    for d in load_devices():
        if d["id"] == device_id:
            return d
    return None


def get_active_device():
    devices = load_devices()
    state = load_state()
    active_id = state.get("active_device_id")
    for d in devices:
        if d["id"] == active_id:
            return d
    if devices:
        state["active_device_id"] = devices[0]["id"]
        save_state(state)
        return devices[0]
    return None


def set_device_field(device_id, **fields):
    devices = load_devices()
    changed = False
    for d in devices:
        if d["id"] == device_id:
            d.update(fields)
            changed = True
            break
    if changed:
        save_devices(devices)


# ---------------------------------------------------------------------------
# فحص الاتصال بالجهاز (سريع) وقراءة وقته
# ---------------------------------------------------------------------------

def check_connectivity(ip, timeout=CONNECT_TIMEOUT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip.strip(), 4370))
        s.close()
        return True
    except Exception:
        return False


def fetch_device_time(ip, comm_key=0):
    """يرجع وقت الجهاز الحالي، أو None لو تعذر الاتصال."""
    with get_device_lock(ip):
        conn = None
        try:
            zk = ZK(ip, port=4370, timeout=ZK_TIMEOUT, password=int(comm_key or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            return conn.get_time()
        except Exception:
            return None
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# كشف تغيير الوقت اليدوي على الجهاز (ميزة مستقلة عن الجدولة)
# ---------------------------------------------------------------------------

DRIFT_THRESHOLD_SECONDS = 30      # فرق أكبر من 30 ثانية يُعتبر مشبوهًا
DRIFT_REMINDER_HOURS = 1          # أثناء استمرار الانحراف، تذكير كل ساعة


def check_drift_for_device(d, device_time_obj):
    """يقارن وقت الجهاز بوقت الهاتف بمنطق ذكي يتتبّع حالة التطابق:
    - أول لحظة يظهر فيها انحراف جديد (بعد ما كان متطابقًا) → تنبيه فوري.
    - أثناء استمرار الانحراف (لم يُصحَّح بعد) → تذكير كل ساعة فقط.
    - بمجرد ما يرجع الوقت متطابقًا → تُصفَّر الحالة، وأي انحراف جديد
      بعد كده يُعامَل كأول مرة (تنبيه فوري تاني).
    d: قاموس الجهاز (سيُعدَّل في مكانه أيضًا لضمان اتساقه مع الحفظ اللاحق
    الذي يقوم به المستدعي). device_time_obj: datetime لوقت الجهاز.
    يرجّع نص رسالة التنبيه لو تم إرسال شيء، أو None.

    ملاحظة أمان تزامن مهمة: هذه الدالة قد تُستدعى من أكثر من مسار في نفس
    اللحظة تقريبًا (استطلاع الواجهة الأمامية كل 30 ثانية + نبضة وضع المراقبة
    المستمرة كل دقيقة) - فلو اعتمدنا فقط على القيمة الممرَّرة في d (التي
    رُبما قُرئت من القرص قبل لحظات من مسار آخر)، قد يقرر المساران معًا أن
    هذا "انحراف جديد" ويرسل كل منهما تنبيهًا منفصلاً لنفس الحدث. لتفادي هذا
    تمامًا، القرار والكتابة يحصلان معًا تحت قفل الجهاز نفسه، وبالاعتماد على
    قراءة طازجة من القرص مباشرة تحت هذا القفل - وليس القيمة القديمة المحتملة
    في d."""
    now = datetime.now()
    diff_seconds = abs((device_time_obj - now).total_seconds())

    with get_device_lock(d["ip"]):
        fresh_devices = load_devices()
        fresh = next((x for x in fresh_devices if x["id"] == d["id"]), d)
        was_drifting = fresh.get("drift_active", False)

        if diff_seconds <= DRIFT_THRESHOLD_SECONDS:
            if was_drifting:
                fresh["drift_active"] = False  # رجع الوقت متطابقًا - تصفير الحالة بصمت
                save_devices(fresh_devices)
                d["drift_active"] = False
            return None

        minutes = int(diff_seconds // 60)

        if not was_drifting:
            # انحراف جديد ظهر بعد ما كان متطابقًا - تنبيه فوري
            message = (
                f"⚠️ رُصد فرق كبير (~{minutes} دقيقة) بين وقت جهاز ({d['name']}) ووقت الهاتف — "
                f"قد يكون أحدهم غيّر الوقت يدويًا من الجهاز مباشرة."
            )
            add_notification(d["name"], "تغيير وقت غير متوقع", message)
            fresh["drift_active"] = True
            fresh["last_drift_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_devices(fresh_devices)
            d["drift_active"] = True
            d["last_drift_alert"] = fresh["last_drift_alert"]
            return message

        # الانحراف مستمر - تذكير كل ساعة فقط
        last_alert_str = fresh.get("last_drift_alert")
        if last_alert_str:
            try:
                last_alert = datetime.strptime(last_alert_str, "%Y-%m-%d %H:%M:%S")
                if now - last_alert < timedelta(hours=DRIFT_REMINDER_HOURS):
                    d["drift_active"] = True
                    d["last_drift_alert"] = last_alert_str
                    return None
            except Exception:
                pass

        message = (
            f"⚠️ ما زال وقت جهاز ({d['name']}) غير مطابق (~{minutes} دقيقة) — "
            f"لم يُصحَّح بعد."
        )
        add_notification(d["name"], "تذكير: تغيير وقت مستمر", message)
        fresh["last_drift_alert"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_devices(fresh_devices)
        d["drift_active"] = True
        d["last_drift_alert"] = fresh["last_drift_alert"]
        return message


def get_devices_with_live_status(focus_device_id=None, full_all=False):
    """يفحص كل الأجهزة، لكن يجلب الوقت الكامل (اتصال ببروتوكول الجهاز)
    فقط للجهاز المُركَّز عليه حاليًا في الكاروسيل (focus_device_id) أو
    الجهاز النشط كاحتياطي - أما باقي الأجهزة فيُكتفى بفحص اتصال خفيف
    (فتح منفذ فقط) لتقليل الحمل على الشبكة والبطارية.
    full_all=True: يجلب الوقت الكامل لكل الأجهزة دفعة واحدة (يُستخدم في
    "وضع المراقبة المستمرة" الذي يشغّل Foreground Service مستمرًا، حيث الدقة أهم من
    توفير البطارية لأن ثمن الإشعار الثابت مدفوع بالفعل).
    يرجّع (قائمة الأجهزة، قائمة رسائل تنبيه جديدة) - الرسائل الجديدة تُستخدم
    لعرض إشعار نظام حقيقي حتى أثناء بقاء التطبيق مفتوحًا في الواجهة الأمامية
    (وليس فقط من مهمة الخلفية)."""
    devices = load_devices()
    state = load_state()
    active_id = state.get("active_device_id")
    target_id = focus_device_id or active_id
    result = []
    alerts = []
    changed = False

    for d in devices:
        online = check_connectivity(d["ip"])
        device_time = None
        prev_status = d.get("last_status")

        if online and (full_all or d["id"] == target_id):
            t = fetch_device_time(d["ip"], d.get("comm_key", 0))
            if t:
                device_time = t.strftime("%Y-%m-%d %H:%M:%S")
                d["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                changed = True
                drift_msg = check_drift_for_device(d, t)
                if drift_msg:
                    changed = True
                    alerts.append(drift_msg)
            else:
                online = False  # اتصال TCP نجح لكن بروتوكول الجهاز لم يستجب
        elif online:
            d["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True

        new_status = "online" if online else "offline"
        if prev_status == "online" and new_status == "offline":
            msg = f"⚠️ انقطع الاتصال بالجهاز ({d['name']})"
            add_notification(d["name"], "انقطاع اتصال", msg)
            alerts.append(msg)
        if d.get("last_status") != new_status:
            d["last_status"] = new_status
            changed = True

        result.append({
            "id": d["id"],
            "name": d["name"],
            "ip": d["ip"],
            "active": d["id"] == active_id,
            "status": new_status,
            "device_time": device_time,
            "last_seen": d.get("last_seen"),
        })

    if changed:
        save_devices(devices)

    return result, alerts


# ---------------------------------------------------------------------------
# سجل العمليات
# ---------------------------------------------------------------------------

def log_action(device_name, action_label, message):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {device_name} | {action_label} | {message}\n"
    path = _path(HISTORY_FILE)
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    lines.append(line)
    lines = lines[-MAX_HISTORY_LINES:]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def read_history(limit=50):
    path = _path(HISTORY_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    return list(reversed(lines[-limit:]))


# ---------------------------------------------------------------------------
# الإشعارات
# ---------------------------------------------------------------------------

def add_notification(device_name, ntype, message):
    notifications = _load_json(NOTIFICATIONS_FILE, [])
    notifications.append({
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "device_name": device_name,
        "type": ntype,
        "message": message,
        "read": False,
    })
    notifications = notifications[-MAX_NOTIFICATIONS:]
    _save_json(NOTIFICATIONS_FILE, notifications)


def get_notifications():
    return list(reversed(_load_json(NOTIFICATIONS_FILE, [])))


def mark_notifications_read():
    notifications = _load_json(NOTIFICATIONS_FILE, [])
    for n in notifications:
        n["read"] = True
    _save_json(NOTIFICATIONS_FILE, notifications)


def clear_notifications():
    _save_json(NOTIFICATIONS_FILE, [])


# ---------------------------------------------------------------------------
# الجدولة (لكل جهاز/مجموعة أجهزة على حدة) - الإعدادات فقط، التنفيذ الفعلي
# من WorkManager في Kotlin
# ---------------------------------------------------------------------------
# كل عنصر: {id, name, device_ids: [...], enabled, use_times, times: [...],
#           use_interval, interval_minutes}

def load_schedules():
    return _load_json(SCHEDULE_FILE, [])


def save_schedules(data):
    _save_json(SCHEDULE_FILE, data)


def get_schedule_by_id(schedule_id):
    for s in load_schedules():
        if s["id"] == schedule_id:
            return s
    return None


# ---------------------------------------------------------------------------
# الإعدادات العامة (قفل التطبيق، الوضع الليلي)
# ---------------------------------------------------------------------------

def default_settings():
    return {
        "app_lock_enabled": False,
        "dark_mode": False,
        "continuous_monitoring_enabled": False,
    }


def load_settings():
    s = default_settings()
    s.update(_load_json(SETTINGS_FILE, {}))
    return s


def save_settings(data):
    _save_json(SETTINGS_FILE, data)


# ---------------------------------------------------------------------------
# مساعد: اسم ملف تصدير آمن يدعم العربية (RFC 5987)
# ---------------------------------------------------------------------------

def build_content_disposition(filename):
    # الاحتياط: بعض متصفحات/أدوات تنزيل أندرويد لا تقرأ filename* (UTF-8)
    # وتكتفي بـ filename= العادي، لذلك نجعل الاسم الاحتياطي بنفس الامتداد الصحيح
    # حتى لا يفقد الملف صيغته حتى لو تجاهلت الأداة الاسم العربي الكامل.
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'dat'
    ascii_fallback = f"export.{ext}"
    encoded = quote(filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


# ===========================================================================
# الصفحة الرئيسية (تصميم صفحة واحدة SPA)
# ===========================================================================

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.route('/static/<path:filename>')
def static_files(filename):
    static_dir = os.path.join(os.path.dirname(__file__), "web")
    return send_from_directory(static_dir, filename)


# ===========================================================================
# API: الأجهزة
# ===========================================================================

@app.route('/api/devices', methods=['GET'])
def api_devices():
    focus_id = request.args.get('focus')
    devices, alerts = get_devices_with_live_status(focus_id)
    return jsonify({"devices": devices, "new_alerts": alerts})


@app.route('/api/devices/test', methods=['POST'])
def api_devices_test():
    ip = (request.json or {}).get('ip', '').strip()
    reachable = check_connectivity(ip)
    return jsonify({"reachable": reachable})


@app.route('/api/devices', methods=['POST'])
def api_devices_add():
    data = request.json or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    if not name or not ip:
        return jsonify({"success": False, "message": "الاسم وعنوان IP مطلوبان"}), 400

    try:
        comm_key = int(data.get('comm_key', 0) or 0)
    except (TypeError, ValueError):
        comm_key = 0

    devices = load_devices()
    new_device = {"id": str(uuid.uuid4())[:8], "name": name, "ip": ip, "comm_key": comm_key, "last_status": None, "last_seen": None}
    devices.append(new_device)
    save_devices(devices)

    state = load_state()
    if not state.get("active_device_id"):
        state["active_device_id"] = new_device["id"]
        save_state(state)

    return jsonify({"success": True, "device": new_device})


@app.route('/api/devices/<device_id>', methods=['DELETE'])
def api_devices_delete(device_id):
    devices = load_devices()
    remaining = [d for d in devices if d["id"] != device_id]

    if len(remaining) == len(devices):
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404

    save_devices(remaining)

    state = load_state()
    if state.get("active_device_id") == device_id:
        state["active_device_id"] = remaining[0]["id"] if remaining else None
        save_state(state)

    return jsonify({"success": True})


@app.route('/api/devices/<device_id>/activate', methods=['POST'])
def api_devices_activate(device_id):
    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404
    state = load_state()
    state["active_device_id"] = device_id
    save_state(state)
    return jsonify({"success": True})


# ===========================================================================
# API: الموظفون (إضافة + قائمة + بحث + تصدير/استيراد + مطابقة إكسل)
# ===========================================================================
# ملاحظة مهمة عن الرقمين في أجهزة ZK:
#   uid     : رقم داخلي (رقم خانة التخزين في ذاكرة الجهاز) - يُحدَّد تلقائيًا
#             كأول رقم بعد أكبر رقم مستخدم، ولا يُدخله المستخدم ولا يراه.
#   user_id : "رقم الموظف" الحقيقي الذي يُدخله المستخدم ويظهر في سجلات الحضور.
# كل البحث وفحص التكرار والمطابقة يتم على user_id فقط، أما uid فيبقى
# داخليًا لتحديد السجل نفسه عند التحديث.

IMPORT_TIMEOUT = 20           # الكتابة أبطأ من القراءة، خصوصًا مع القوالب
MAX_EMPLOYEE_NUMBER_BYTES = 24   # سعة حقل user_id في سجل المستخدم (72 بايت)
MAX_DEVICE_UID = 65535           # أكبر رقم داخلي ممكن في بروتوكول ZK

EMPLOYEES_FILE_FORMAT = "zkcontrol-employees"
EMPLOYEES_FILE_VERSION = 1


def _zk_for(device, force_udp=True, timeout=None):
    return ZK(device["ip"], port=4370, timeout=timeout or ZK_TIMEOUT,
              password=int(device.get("comm_key", 0) or 0), force_udp=force_udp, ommit_ping=True)


def connect_for_writing(device):
    """اتصال مخصص لعمليات الكتابة (الاستيراد): TCP أولًا ثم UDP احتياطًا.

    قالب البصمة حزمة كبيرة نسبيًا؛ وUDP لا يضمن وصولها ولا يُبلّغ عن ضياعها،
    فتنتهي العملية "بنجاح" بينما الجهاز لم يستلم القالب (أو استلم جزءًا منه
    فيُسجَّل قالب لا يطابق أحدًا). TCP يضمن الوصول كاملًا أو يعطي خطأ صريحًا.
    يرجع (الاتصال، اسم البروتوكول المستخدم)."""
    last_error = None
    for use_udp in (False, True):
        try:
            conn = _zk_for(device, force_udp=use_udp, timeout=IMPORT_TIMEOUT).connect()
            return conn, ("UDP" if use_udp else "TCP")
        except Exception as e:
            last_error = e
    raise last_error


def normalize_employee_number(value):
    """يوحّد شكل رقم الموظف للمقارنة فقط (لا يُستخدم للحفظ أبدًا): يحذف كل
    المسافات، ويحوّل الحروف اللاتينية لحالة كبيرة، ويحذف الأصفار البادئة من
    الأرقام الخالصة (لأن إكسل يحوّل 066 إلى 66 تلقائيًا)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = re.sub(r"\s+", "", str(value)).upper()
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def clean_employee_number(raw):
    """يرجع (الرقم كما كُتب بعد حذف المسافات الطرفية، رسالة خطأ أو None).
    رقم الموظف نص وليس عددًا - يقبل أرقامًا مثل NN-749397."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None, "أدخل رقم الموظف"
    if re.search(r"\s", s):
        return None, "رقم الموظف يجب ألا يحتوي على مسافات"
    if len(s.encode("utf-8")) > MAX_EMPLOYEE_NUMBER_BYTES:
        return None, "رقم الموظف أطول من المسموح به في الجهاز"
    return s, None


def next_free_uid(users):
    """أول رقم داخلي بعد أكبر رقم مستخدم (نفس سلوك الجهاز ومكتبة pyzk)."""
    return max((u.uid for u in users), default=0) + 1


def employee_label(number, name):
    return f"({name}) رقم {number}" if name else f"رقم {number}"


def find_by_employee_number(users, number):
    key = normalize_employee_number(number)
    return next((u for u in users if u.user_id and normalize_employee_number(u.user_id) == key), None)


def unique_employee_numbers(numbers):
    """ينظّف قائمة أرقام مكتوبة يدويًا: يحذف الفارغ والمكرر (بالمطابقة
    الموحّدة، فـ 66 و066 رقم واحد) مع الحفاظ على ترتيب الكتابة."""
    seen, out = set(), []
    for n in numbers if isinstance(numbers, list) else []:
        shown = str(n if n is not None else "").strip()
        key = normalize_employee_number(shown)
        if key and key not in seen:
            seen.add(key)
            out.append(shown)
    return out


def index_by_employee_number(users):
    """فهرس {رقم موظف موحّد: المستخدم} لتسريع المطابقة الجماعية."""
    return {normalize_employee_number(u.user_id): u for u in users if u.user_id}


def _store_prepared_file(filename, mimetype, content):
    """يحفظ ملفًا جاهزًا في نفس ذاكرة التنزيل المؤقتة المستخدمة في تصدير
    الحضور، ويرجع رمز التنزيل (يُنزَّل عبر نفس مسار التنزيل الموجود)."""
    token = str(uuid.uuid4())
    _export_cache[token] = {
        "filename": filename, "mimetype": mimetype, "content": content,
        "created": datetime.now(),
    }
    now = datetime.now()
    expired = [t for t, v in _export_cache.items() if (now - v["created"]).total_seconds() > _EXPORT_TOKEN_TTL_SECONDS]
    for t in expired:
        _export_cache.pop(t, None)
    return token


def read_device_hardware(conn):
    """يقرأ معلومات الجهاز المهمة للتوافق. كل قراءة مستقلة داخل try لأن بعض
    الطرازات لا تدعم بعض الاستعلامات، وفشل واحدة يجب ألا يُفشل الباقي."""
    info = {}
    for key, getter in (
        ("fp_version", "get_fp_version"),
        ("device_name", "get_device_name"),
        ("platform", "get_platform"),
        ("firmware", "get_firmware_version"),
        ("serial", "get_serialnumber"),
    ):
        try:
            value = getattr(conn, getter)()
            info[key] = str(value).strip() if value is not None else None
        except Exception:
            info[key] = None
    return info


def fingerprints_compatible(source_fp, target_fp):
    """None = غير معروف (لا نستطيع الجزم، فلا نحذّر ولا نطمئن)."""
    if not source_fp or not target_fp:
        return None
    return str(source_fp).strip() == str(target_fp).strip()


@app.route('/api/devices/<device_id>/hardware', methods=['GET'])
def api_device_hardware(device_id):
    device, err = _resolve_online_device(device_id)
    if err:
        return err
    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                info = read_device_hardware(conn)
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    info["success"] = True
    info["device_name_saved"] = device["name"]
    return jsonify(info)


def _resolve_online_device(device_id):
    """يرجع (الجهاز، رد خطأ جاهز أو None)."""
    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return None, (jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400)
    if not check_connectivity(device["ip"]):
        return None, (jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا", "offline": True}), 400)
    return device, None


@app.route('/api/employees', methods=['GET'])
def api_employees_list():
    device_id = request.args.get('device_id')
    query = request.args.get('q', '').strip().lower()

    device, err = _resolve_online_device(device_id)
    if err:
        return err

    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                users = conn.get_users()
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    result = []
    for u in users:
        number = str(u.user_id or "")
        if query and query not in number.lower() and query not in (u.name or "").lower():
            continue
        result.append({"user_id": number, "uid": u.uid, "name": u.name or "", "privilege": u.privilege})

    return jsonify({"success": True, "employees": result, "count": len(result)})


@app.route('/api/employees', methods=['POST'])
def api_employees_add():
    data = request.json or {}
    device, err = _resolve_online_device(data.get('device_id'))
    if err:
        return err

    number, number_err = clean_employee_number(data.get('user_id'))
    if number_err:
        return jsonify({"success": False, "message": number_err}), 400

    try:
        name = (data.get('name') or '').strip()          # الاسم اختياري
        password = str(data.get('password') or '')
        privilege = int(data.get('privilege', 0))
        confirm_overwrite = bool(data.get('confirm_overwrite', False))

        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                users = conn.get_users()
                existing = find_by_employee_number(users, number)

                if existing and not confirm_overwrite:
                    return jsonify({
                        "success": False,
                        "number_exists": True,
                        "existing_name": existing.name or "",
                        "message": f"رقم الموظف ({number}) مسجّل بالفعل على الجهاز — الحفظ سيحدّث بياناته.",
                    }), 409

                if existing:
                    # تحديث نفس السجل (نفس الرقم الداخلي) بدل إنشاء سجل ثانٍ بنفس
                    # رقم الموظف. الحقول التي تُركت فارغة تبقى كما هي على الجهاز،
                    # ورقم البطاقة والمجموعة لا يُمسّان.
                    conn.set_user(
                        uid=existing.uid,
                        name=name or (existing.name or ''),
                        privilege=privilege,
                        password=password or (existing.password or ''),
                        group_id=existing.group_id or '',
                        user_id=existing.user_id,
                        card=existing.card or 0,
                    )
                    action_word = "تم تحديث"
                else:
                    uid = next_free_uid(users)
                    if uid > MAX_DEVICE_UID:
                        return jsonify({"success": False, "message": "امتلأت سعة الجهاز من الموظفين"}), 400
                    conn.set_user(uid=uid, name=name, privilege=privilege, password=password, user_id=number)
                    action_word = "تمت إضافة"
            finally:
                if conn:
                    conn.disconnect()

        log_action(device["name"], "إضافة موظف", f"✓ {action_word} الموظف {employee_label(number, name)}")
        return jsonify({"success": True, "message": f"{action_word} الموظف {employee_label(number, name)} بنجاح"})
    except Exception as e:
        log_action(device["name"], "إضافة موظف", f"✕ فشل (رقم {number}): {e}")
        return jsonify({"success": False, "message": f"فشل: {e}"}), 500


# ---------------------------------------------------------------------------
# تصدير الموظفين مع بصماتهم (من الجهاز → ملف JSON)
# ---------------------------------------------------------------------------

@app.route('/api/employees/export/prepare', methods=['POST'])
def api_employees_export_prepare():
    """قراءة فقط من الجهاز (get_users + get_templates) - لا يكتب عليه شيئًا.
    الوجوه لا تُصدَّر لأن المكتبة لا تدعم قراءة قوالب الوجه.

    numbers (اختياري): قائمة أرقام موظفين محددة. غيابها = كل الموظفين.
    الأرقام غير الموجودة على الجهاز تُرجع في missing، ويُصدَّر الموجود فقط."""
    data = request.json or {}
    device, err = _resolve_online_device(data.get('device_id'))
    if err:
        return err

    wanted = None
    if data.get('numbers') is not None:
        wanted = unique_employee_numbers(data.get('numbers'))
        if not wanted:
            return jsonify({"success": False, "message": "اكتب رقم موظف واحدًا على الأقل"}), 400

    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                users = conn.get_users()
                templates = conn.get_templates()
                hardware = read_device_hardware(conn)
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        log_action(device["name"], "تصدير الموظفين", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    missing = []
    if wanted is not None:
        index = index_by_employee_number(users)
        selected = []
        for n in wanted:
            u = index.get(normalize_employee_number(n))
            if u:
                selected.append(u)
            else:
                missing.append(n)
        if not selected:
            return jsonify({
                "success": False, "missing": missing,
                "message": "لا يوجد أي من الأرقام المكتوبة على الجهاز",
            }), 400
        users = selected

    fingers_by_uid = {}
    for f in templates:
        fingers_by_uid.setdefault(f.uid, []).append({
            "fid": f.fid,
            "valid": f.valid,
            "template": bytes(f.template).hex(),
        })

    employees = []
    fingers_count = 0
    for u in users:
        fingers = fingers_by_uid.get(u.uid, [])
        fingers_count += len(fingers)
        employees.append({
            "user_id": str(u.user_id or ""),
            "name": u.name or "",
            "privilege": u.privilege,
            "password": u.password or "",
            "card": u.card or 0,
            "group_id": str(u.group_id or ""),
            "fingers": fingers,
        })

    now = datetime.now()
    payload = {
        "format": EMPLOYEES_FILE_FORMAT,
        "version": EMPLOYEES_FILE_VERSION,
        "exported_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source_device": device["name"],
        "source_hardware": hardware,
        "scope": "selected" if wanted is not None else "all",
        "employees_count": len(employees),
        "fingers_count": fingers_count,
        "employees": employees,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    scope_part = "محددون_" if wanted is not None else ""
    filename = f"موظفين_{scope_part}{device['name'].replace(' ', '_')}_{now.strftime('%Y-%m-%d_%H-%M')}.json"
    token = _store_prepared_file(filename, "application/json", content)

    scope_note = f" (محددون، غير موجود {len(missing)})" if wanted is not None else ""
    log_action(device["name"], "تصدير الموظفين", f"✓ {len(employees)} موظف و{fingers_count} بصمة{scope_note}")
    return jsonify({
        "success": True, "token": token, "filename": filename,
        "employees_count": len(employees), "fingers_count": fingers_count,
        "missing": missing, "hardware": hardware,
    })


# ---------------------------------------------------------------------------
# استيراد الموظفين مع بصماتهم (من ملف → الجهاز)
# ---------------------------------------------------------------------------

_import_jobs = {}
_IMPORT_JOB_TTL_SECONDS = 1800


@app.route('/api/employees/import/check', methods=['POST'])
def api_employees_import_check():
    """فحص مسبق (قراءة فقط): أي أرقام الموظفين في الملف موجودة بالفعل على
    الجهاز الهدف - حتى يرى المستخدم ما سيحدث قبل أي كتابة."""
    data = request.json or {}
    device, err = _resolve_online_device(data.get('device_id'))
    if err:
        return err

    numbers = data.get('numbers') or []
    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                users = conn.get_users()
                hardware = read_device_hardware(conn)
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    index = index_by_employee_number(users)
    existing = []
    for n in numbers:
        u = index.get(normalize_employee_number(n))
        if u:
            existing.append({"user_id": str(n), "name": u.name or ""})

    return jsonify({"success": True, "existing": existing, "device_name": device["name"], "hardware": hardware})


def _fingers_from_export(uid, exported):
    """يبني كائنات البصمات من الملف المُصدَّر (القالب محفوظ نصًا ست عشريًا)."""
    from zk.finger import Finger
    fingers = []
    for f in exported or []:
        tpl = f.get("template")
        if not tpl:
            continue
        fingers.append(Finger(uid, int(f.get("fid", 0)), int(f.get("valid", 1)), bytes.fromhex(tpl)))
    return fingers


def _templates_on_device(conn, uid):
    """{fid: bytes} للقوالب الموجودة فعليًا على الجهاز لهذا الرقم الداخلي."""
    result = {}
    for t in conn.get_templates():
        if t.uid == uid:
            result[t.fid] = bytes(t.template)
    return result


def _write_fingers_verified(conn, user_obj, fingers):
    """يكتب القوالب ثم يقرأها من الجهاز ويقارنها بايتًا ببايت.

    هذا التحقق ضروري: الكتابة قد "تنجح" من طرف التطبيق بينما لا يصل للجهاز
    شيء، أو يصل قالب ناقص يبدو مسجّلًا لكنه لا يطابق صاحبه عند البصم.
    يعيد المحاولة مرة واحدة، ويرجع (عدد المؤكَّد، رسالة الخطأ أو None)."""
    attempts = 2
    last_missing = []
    for attempt in range(attempts):
        conn.save_user_template(user_obj, fingers)
        try:
            conn.refresh_data()
        except Exception:
            pass

        on_device = _templates_on_device(conn, user_obj.uid)
        last_missing = [f.fid for f in fingers
                        if on_device.get(f.fid) != bytes(f.template)]
        if not last_missing:
            return len(fingers), None
        if attempt + 1 < attempts:
            time.sleep(0.5)

    confirmed = len(fingers) - len(last_missing)
    return confirmed, f"لم تُحفظ {len(last_missing)} بصمة على الجهاز بشكل صحيح"


def _run_import_job(job_id, device, employees, overwrite, skip_fingers=False, restart_device=False):
    from zk.user import User

    job = _import_jobs[job_id]
    try:
        with get_device_lock(device["ip"]):
            conn = None
            device_disabled = False
            try:
                conn, protocol = connect_for_writing(device)
                job["protocol"] = protocol

                # أجهزة ZK قد تتجاهل الكتابة بصمت أثناء انشغالها باستقبال
                # البصم؛ تعطيلها مؤقتًا يجعل الكتابة موثوقة
                try:
                    conn.disable_device()
                    device_disabled = True
                except Exception:
                    pass

                users = conn.get_users()
                index = index_by_employee_number(users)
                next_uid = next_free_uid(users)

                for emp in employees:
                    shown_number = str(emp.get("user_id") or "—")
                    try:
                        number, number_err = clean_employee_number(emp.get("user_id"))
                        if number_err:
                            raise ValueError(number_err)

                        existing = index.get(normalize_employee_number(number))
                        if existing and not overwrite:
                            job["skipped"] += 1
                            continue

                        name = str(emp.get("name") or "")
                        privilege = int(emp.get("privilege") or 0)
                        password = str(emp.get("password") or "")
                        group_id = str(emp.get("group_id") or "")
                        card = int(emp.get("card") or 0)

                        if existing:
                            # نفس السجل ونفس الرقم الداخلي - لا يُنشأ موظف مكرر
                            uid = existing.uid
                            number = existing.user_id
                        else:
                            if next_uid > MAX_DEVICE_UID:
                                raise ValueError("امتلأت سعة الجهاز من الموظفين")
                            uid = next_uid
                            next_uid += 1

                        conn.set_user(uid=uid, name=name, privilege=privilege, password=password,
                                      group_id=group_id, user_id=number, card=card)

                        fingers = [] if skip_fingers else _fingers_from_export(uid, emp.get("fingers"))
                        if fingers:
                            user_obj = User(uid, name, privilege, password, group_id, number, card)
                            confirmed, finger_error = _write_fingers_verified(conn, user_obj, fingers)
                            job["fingers"] += confirmed
                            job["fingers_sent"] += len(fingers)
                            if finger_error:
                                job["finger_failed"].append({"user_id": number, "reason": finger_error})

                        if existing:
                            job["updated"] += 1
                        else:
                            job["added"] += 1
                            # يمنع تكرار نفس الرقم لو ورد مرتين داخل الملف نفسه
                            index[normalize_employee_number(number)] = User(uid, name, privilege, password, group_id, number, card)
                    except Exception as e:
                        job["failed"].append({"user_id": shown_number, "reason": str(e)})
                    finally:
                        job["done"] += 1

                try:
                    conn.refresh_data()
                except Exception:
                    pass  # تحديث بيانات الجهاز تحسين إضافي فقط

                # بعض الطرازات لا تُحمّل القوالب الجديدة في ذاكرة المطابقة
                # إلا بعد إعادة التشغيل، فتبدو البصمة مسجّلة ولا تعمل
                if restart_device and job["fingers"] > 0:
                    try:
                        if device_disabled:
                            try:
                                conn.enable_device()
                            except Exception:
                                pass
                            device_disabled = False
                        conn.restart()
                        job["restarted"] = True
                        conn = None  # الجهاز يقطع الجلسة فور إعادة التشغيل
                    except Exception as e:
                        job["restart_error"] = str(e)
            finally:
                if conn:
                    if device_disabled:
                        try:
                            conn.enable_device()
                        except Exception:
                            pass
                    conn.disconnect()
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"تعذّر الاتصال: {e}"

    summary = (f"إضافة {job['added']}، تحديث {job['updated']}، تخطي {job['skipped']}، "
               f"فشل {len(job['failed'])}، بصمات مؤكَّدة {job['fingers']} من {job['fingers_sent']}"
               f" ({job.get('protocol') or '—'})")
    mark = "✓" if job["status"] == "done" and not job["failed"] and not job["finger_failed"] else "✕"
    log_action(device["name"], "استيراد الموظفين", f"{mark} {summary}" + (f" — {job['message']}" if job.get("message") else ""))


@app.route('/api/employees/import/start', methods=['POST'])
def api_employees_import_start():
    data = request.json or {}
    device, err = _resolve_online_device(data.get('device_id'))
    if err:
        return err

    employees = data.get('employees')
    if not isinstance(employees, list) or not employees:
        return jsonify({"success": False, "message": "الملف لا يحتوي على موظفين"}), 400

    now = datetime.now()
    for jid in [j for j, v in _import_jobs.items() if (now - v["created"]).total_seconds() > _IMPORT_JOB_TTL_SECONDS]:
        _import_jobs.pop(jid, None)

    job_id = str(uuid.uuid4())
    _import_jobs[job_id] = {
        "status": "running", "total": len(employees), "done": 0,
        "added": 0, "updated": 0, "skipped": 0, "fingers": 0, "fingers_sent": 0,
        "failed": [], "finger_failed": [], "message": "", "protocol": None,
        "restarted": False, "restart_error": "",
        "created": now, "device_name": device["name"],
    }
    threading.Thread(target=_run_import_job,
                     args=(job_id, device, employees, bool(data.get('overwrite', False)),
                           bool(data.get('skip_fingers', False)),
                           bool(data.get('restart_device', False))),
                     daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route('/api/employees/import/status/<job_id>', methods=['GET'])
def api_employees_import_status(job_id):
    job = _import_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "message": "عملية الاستيراد غير موجودة"}), 404
    out = {k: v for k, v in job.items() if k != "created"}
    out["success"] = True
    return jsonify(out)


# ---------------------------------------------------------------------------
# مطابقة أرقام الموظفين بملف إكسل (قراءة فقط من الجهاز)
# ---------------------------------------------------------------------------

STATUS_HEADER = "الحالة على الجهاز"
_NOT_NUMBER_HEADERS = ("هاتف", "موبايل", "جوال", "phone", "mobile", STATUS_HEADER)
_ARABIC_LETTER_RE = re.compile(r"[\u0600-\u06FF]")
_NUMBER_LIKE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-_/]*$")
MAX_SCANNED_ROWS = 5000


def looks_like_employee_number(value):
    """قيمة تصلح أن تكون رقم موظف: بلا مسافات ولا حروف عربية، وفيها رقم
    واحد على الأقل، وبطول معقول."""
    if value is None:
        return False
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).strip()
    if not s or re.search(r"\s", s) or _ARABIC_LETTER_RE.search(s):
        return False
    if len(s.encode("utf-8")) > MAX_EMPLOYEE_NUMBER_BYTES:
        return False
    s = s.upper()
    return bool(_NUMBER_LIKE_RE.match(s)) and any(ch.isdigit() for ch in s)


def detect_number_column(ws, device_numbers):
    """يختار عمود أرقام الموظفين بالمقارنة الفعلية مع أرقام الجهاز، لا
    بالتخمين من العناوين: العمود الذي تطابق أكبر عدد من قيمه أرقام الجهاز هو
    المقصود قطعًا، مهما كان ترتيب الأعمدة ووجود العناوين من عدمه.
    عند عدم تطابق أي عمود (كأن يكون الجهاز فارغًا أو الأرقام كلها جديدة)
    نرجع لأكثر عمود تشبه قيمه أرقام الموظفين.
    يرجع (رقم العمود، رقم صف العنوان أو None، وصف العمود، عدد المطابقات)."""
    from openpyxl.utils import get_column_letter

    max_row = min(ws.max_row or 0, MAX_SCANNED_ROWS)
    max_col = ws.max_column or 0
    best = None

    for col in range(1, max_col + 1):
        header = ws.cell(row=1, column=col).value
        header_text = str(header).strip() if header is not None else ""
        if header_text and any(x in header_text.lower() for x in _NOT_NUMBER_HEADERS):
            continue  # عمود هاتف أو عمود نتيجة مطابقة سابقة

        matches = candidates = 0
        for row in range(1, max_row + 1):
            value = ws.cell(row=row, column=col).value
            if not looks_like_employee_number(value):
                continue
            candidates += 1
            if normalize_employee_number(value) in device_numbers:
                matches += 1

        if candidates == 0:
            continue
        score = (matches, candidates, -col)
        if best is None or score > best[0]:
            best = (score, col, matches, candidates)

    if best is None:
        return None, None, "", 0

    _, col, matches, _ = best
    # الصف الأول عنوان فقط إن لم تكن خليته في هذا العمود رقم موظف - وإلا
    # فهو بيانات (ملفات بلا عناوين) ولا يجوز تخطّيه
    first_value = ws.cell(row=1, column=col).value
    if looks_like_employee_number(first_value):
        header_row = None
        label = f"العمود {get_column_letter(col)}"
    else:
        header_row = 1
        text = str(first_value).strip() if first_value is not None else ""
        label = text or f"العمود {get_column_letter(col)}"
    return col, header_row, label, matches


@app.route('/api/employees/match/prepare', methods=['POST'])
def api_employees_match_prepare():
    import base64
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill, Font

    data = request.json or {}
    device, err = _resolve_online_device(data.get('device_id'))
    if err:
        return err

    try:
        raw = base64.b64decode(data.get('file_b64') or '')
        wb = load_workbook(io.BytesIO(raw))
        ws = wb.worksheets[0]
    except Exception:
        return jsonify({"success": False, "message": "تعذّر قراءة الملف — تأكد أنه ملف إكسل بصيغة xlsx"}), 400

    try:
        with get_device_lock(device["ip"]):
            conn = None
            try:
                conn = _zk_for(device).connect()
                users = conn.get_users()
            finally:
                if conn:
                    conn.disconnect()
    except Exception as e:
        log_action(device["name"], "مطابقة إكسل", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    device_numbers = {normalize_employee_number(u.user_id) for u in users if u.user_id}

    col, header_row, header_text, matched = detect_number_column(ws, device_numbers)
    if col is None:
        return jsonify({"success": False, "message": "لم أجد أي عمود فيه أرقام موظفين في الملف"}), 400

    # لو الملف نتيجة مطابقة سابقة، نحدّث عمود الحالة نفسه بدل إضافة عمود جديد
    status_col = None
    if header_row:
        for c in range(1, (ws.max_column or 1) + 1):
            v = ws.cell(row=header_row, column=c).value
            if v is not None and str(v).strip() == STATUS_HEADER:
                status_col = c
                break
    if status_col is None:
        status_col = (ws.max_column or 1) + 1
    first_data_row = (header_row or 0) + 1

    found_fill = PatternFill("solid", fgColor="D9F2E1")
    missing_fill = PatternFill("solid", fgColor="F9DADA")
    if header_row:
        cell = ws.cell(row=header_row, column=status_col, value=STATUS_HEADER)
        cell.font = Font(bold=True)

    found = missing = 0
    for r in range(first_data_row, min(ws.max_row or 0, MAX_SCANNED_ROWS) + 1):
        key = normalize_employee_number(ws.cell(row=r, column=col).value)
        if not key:
            continue
        if key in device_numbers:
            found += 1
            c = ws.cell(row=r, column=status_col, value="موجود")
            c.fill = found_fill
        else:
            missing += 1
            c = ws.cell(row=r, column=status_col, value="غير موجود")
            c.fill = missing_fill

    if found + missing == 0:
        return jsonify({"success": False, "message": f"لم أجد أرقامًا في عمود «{header_text}»"}), 400

    bio = io.BytesIO()
    wb.save(bio)
    original = (data.get('filename') or 'ملف').rsplit('.', 1)[0]
    filename = f"{original}_مطابقة_{device['name'].replace(' ', '_')}.xlsx"
    token = _store_prepared_file(
        filename, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", bio.getvalue())

    log_action(device["name"], "مطابقة إكسل", f"✓ موجود {found}، غير موجود {missing}")
    return jsonify({
        "success": True, "token": token, "filename": filename,
        "found": found, "missing": missing, "column": header_text,
        "has_header": bool(header_row),
    })


# ===========================================================================
# API: الحضور (عرض فقط) + التصدير (ملف فعلي)
# ===========================================================================

def _fetch_attendance_filtered(device, start_str, end_str, query=""):
    with get_device_lock(device["ip"]):
        conn = None
        try:
            zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            attendance = conn.get_attendance()
        finally:
            if conn:
                conn.disconnect()

    if start_str and end_str:
        start_dt = datetime.strptime(start_str, '%Y-%m-%d')
        end_dt = datetime.strptime(end_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        attendance = [a for a in attendance if start_dt <= a.timestamp <= end_dt]

    if query:
        q = query.strip().lower()
        attendance = [
            a for a in attendance
            if q in str(a.user_id).lower() or q in str(a.uid).lower()
        ]

    # لا نعيد الترتيب حسب قيمة الوقت المكتوب (Timestamp) - لو ساعة الجهاز
    # كانت منحرفة لحظة تسجيل بصمة معيّنة، هذا الوقت نفسه يكون غير دقيق،
    # فإعادة الترتيب بناءً عليه كانت تُخرج الترتيب الفعلي لحدوث البصمات.
    # جهاز ZK يرجّع السجلات أصلاً بترتيب دخولها الحقيقي في ذاكرته (الأقدم
    # أولاً) - فنكتفي بعكس هذا الترتيب فقط (الأحدث دخولاً أولاً في العرض)
    # دون أي إعادة فرز حسب قيمة الوقت نفسها.
    attendance.reverse()
    return attendance


@app.route('/api/attendance/view', methods=['GET'])
def api_attendance_view():
    device_id = request.args.get('device_id')
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    query = request.args.get('q', '')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, query)
    except Exception as e:
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    records = [{
        "user_id": a.user_id,
        "uid": a.uid,
        "timestamp": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "status": a.status,
    } for a in attendance]

    return jsonify({"success": True, "records": records, "count": len(records)})


@app.route('/api/attendance/export', methods=['GET'])
def api_attendance_export():
    """محفوظ للتوافق القديم فقط - يُفضَّل استخدام prepare + download (أسفل)
    لأنهما يفصلان الاتصال البطيء بالجهاز عن التنزيل الفعلي نفسه."""
    device_id = request.args.get('device_id')
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    fmt = request.args.get('format', 'xlsx')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return Response("لا يوجد جهاز محدد", status=400)
    if not check_connectivity(device["ip"]):
        return Response(f"الجهاز ({device['name']}) غير متصل حاليًا", status=400)

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, "")
    except Exception as e:
        log_action(device["name"], "تصدير حضور", f"✕ فشل: {e}")
        return Response(f"تعذّر الاتصال: {e}", status=500)

    filename, mimetype, content = _build_export_file(device, attendance, start_str, end_str, fmt)
    log_action(device["name"], "تصدير حضور", f"✓ {fmt} — {len(attendance)} سجل")
    return Response(content, mimetype=mimetype, headers={"Content-Disposition": build_content_disposition(filename)})


def _build_export_file(device, attendance, start_str, end_str, fmt):
    """يبني محتوى الملف (بدون أي اتصال بالجهاز - البيانات جاهزة بالفعل)."""
    period_label = f"{start_str}_إلى_{end_str}" if start_str and end_str else "كل_السجلات"
    safe_device_name = device["name"].replace(" ", "_")
    base_filename = f"{safe_device_name}_{period_label}"

    header = ["ID الموظف", "UID", "الوقت", "الحالة"]
    rows = [[a.user_id, a.uid, a.timestamp.strftime("%Y-%m-%d %H:%M:%S"), a.status] for a in attendance]

    if fmt == 'csv':
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        content = ('\ufeff' + buf.getvalue()).encode('utf-8')  # BOM لدعم العربية في إكسل
        return f"{base_filename}.csv", "text/csv", content

    elif fmt == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "الحضور"
        ws.append(header)
        for row in rows:
            ws.append(row)
        bio = io.BytesIO()
        wb.save(bio)
        return (f"{base_filename}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                bio.getvalue())

    else:  # dat
        lines = ["\t".join(header)] + ["\t".join(str(c) for c in row) for row in rows]
        content = "\n".join(lines).encode('utf-8')
        return f"{base_filename}.dat", "application/octet-stream", content


# تخزين مؤقت للملفات المُجهَّزة بانتظار التنزيل الفعلي (يفصل الاتصال البطيء
# بالجهاز عن التنزيل نفسه، ويمنع تعليق DownloadManager أثناء انتظار الجهاز)
_export_cache = {}
_EXPORT_TOKEN_TTL_SECONDS = 300  # 5 دقائق كحد أقصى للاحتفاظ بالملف الجاهز


@app.route('/api/attendance/export/prepare', methods=['POST'])
def api_attendance_export_prepare():
    """الخطوة الأولى: تتصل بالجهاز وتجهّز الملف بالكامل في الذاكرة، وترجع
    token فقط (بدون بدء أي تنزيل بعد) - لو الجهاز غير متصل، ترجع خطأ واضح
    فورًا بدل ما تترك أداة التنزيل معلّقة تستنى."""
    data = request.json or {}
    device_id = data.get('device_id')
    start_str = data.get('start', '')
    end_str = data.get('end', '')
    fmt = data.get('format', 'xlsx')

    device = get_device_by_id(device_id) if device_id else get_active_device()
    if not device:
        return jsonify({"success": False, "message": "لا يوجد جهاز محدد"}), 400
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا", "offline": True}), 400

    try:
        attendance = _fetch_attendance_filtered(device, start_str, end_str, "")
        filename, mimetype, content = _build_export_file(device, attendance, start_str, end_str, fmt)
    except Exception as e:
        log_action(device["name"], "تصدير حضور", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"تعذّر الاتصال: {e}"}), 500

    token = str(uuid.uuid4())
    _export_cache[token] = {
        "filename": filename, "mimetype": mimetype, "content": content,
        "created": datetime.now(),
    }
    log_action(device["name"], "تصدير حضور", f"✓ {fmt} — {len(attendance)} سجل (جاهز للتنزيل)")

    # تنظيف أي رموز قديمة منتهية الصلاحية
    now = datetime.now()
    expired = [t for t, v in _export_cache.items() if (now - v["created"]).total_seconds() > _EXPORT_TOKEN_TTL_SECONDS]
    for t in expired:
        _export_cache.pop(t, None)

    return jsonify({"success": True, "token": token, "filename": filename})


@app.route('/api/attendance/export/download/<token>/<path:filename>', methods=['GET'])
def api_attendance_export_download(token, filename):
    """الخطوة الثانية: تنزيل فوري وسريع للملف الجاهز بالفعل - بدون أي
    اتصال بجهاز البصمة في هذه اللحظة، فلا يوجد احتمال تعليق.

    التوكن يبقى صالحًا حتى انتهاء مهلته الزمنية (وليس لاستخدام واحد فقط) -
    لأن أدوات التنزيل في أندرويد (DownloadManager) أحيانًا تُنشئ أكثر من
    طلب واحد لنفس الرابط (تحقق مبدئي + إعادة محاولة تلقائية)، وحذف الملف
    من الذاكرة بعد أول طلب فقط كان يسبب فشل الطلبات التالية بلا داعٍ.
    اسم الملف مُضمَّن في مسار الرابط نفسه (وليس فقط في ترويسة
    Content-Disposition) لضمان وصوله بشكل صحيح لأداة التنزيل في كل الحالات."""
    entry = _export_cache.get(token)
    if not entry:
        return Response("انتهت صلاحية رابط التنزيل، أعد التصدير من جديد", status=404)

    return Response(
        entry["content"],
        mimetype=entry["mimetype"],
        headers={"Content-Disposition": build_content_disposition(entry["filename"])}
    )


# ===========================================================================
# API: المزامنة
# ===========================================================================

def _sync_single_device(d):
    """ينفّذ مزامنة جهاز واحد بوقت الهاتف. يرجّع (نجح: bool, رسالة: str)."""
    if not check_connectivity(d["ip"]):
        return False, f"⚠️ تعذّرت مزامنة ({d['name']}) — الجهاز غير متصل"
    with get_device_lock(d["ip"]):
        conn = None
        try:
            zk = ZK(d["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(d.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
            conn = zk.connect()
            conn.set_time(datetime.now())

            # تصفير فورية لعلامة "الانحراف" بعد نجاح المزامنة مباشرة - بدون
            # هذا، يبقى الإشعار يقول "غير متزامن" حتى دورة الفحص القادمة
            # (قد تصل لدقيقة كاملة) رغم أن الوقت صحيح فعليًا بالفعل
            all_devices = load_devices()
            for x in all_devices:
                if x["id"] == d["id"] and x.get("drift_active"):
                    x["drift_active"] = False
                    save_devices(all_devices)
                    break

            return True, f"✓ تمت مزامنة ({d['name']}) مع وقت الهاتف"
        except Exception as e:
            return False, f"⚠️ تعذّرت مزامنة ({d['name']}): {e}"
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


def sync_devices_collect_alerts(device_ids=None):
    """يزامن أجهزة محددة (أو كل الأجهزة لو device_ids=None)، ويسجّل كل
    نتيجة، ويرجّع فقط رسائل الفشل (تُستخدم لإرسال إشعارات نظام من الخلفية)."""
    devices = load_devices()
    if device_ids is not None:
        devices = [d for d in devices if d["id"] in device_ids]
    alerts = []
    for d in devices:
        ok, msg = _sync_single_device(d)
        log_action(d["name"], "مزامنة", msg)
        if not ok:
            add_notification(d["name"], "فشل مزامنة", msg)
            alerts.append(msg)
    return alerts


@app.route('/api/sync/all', methods=['POST'])
def api_sync_all():
    devices = load_devices()
    success, failed = [], []
    for d in devices:
        ok, msg = _sync_single_device(d)
        log_action(d["name"], "مزامنة", msg)
        if ok:
            success.append(d["name"])
        else:
            failed.append(d["name"])
            add_notification(d["name"], "فشل مزامنة", msg)

    return jsonify({"success": True, "synced": success, "failed": failed})


@app.route('/api/sync/device/<device_id>', methods=['POST'])
def api_sync_device(device_id):
    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404

    ok, msg = _sync_single_device(device)
    log_action(device["name"], "مزامنة", msg)
    if not ok:
        add_notification(device["name"], "فشل مزامنة", msg)
        return jsonify({"success": False, "message": msg}), 400
    return jsonify({"success": True, "message": msg})


@app.route('/api/sync/custom', methods=['POST'])
def api_sync_custom():
    data = request.json or {}
    device_id = data.get('device_id')
    dt_str = data.get('datetime')

    device = get_device_by_id(device_id)
    if not device:
        return jsonify({"success": False, "message": "الجهاز غير موجود"}), 404
    if not check_connectivity(device["ip"]):
        return jsonify({"success": False, "message": f"الجهاز ({device['name']}) غير متصل حاليًا"}), 400

    try:
        custom_dt = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        with get_device_lock(device["ip"]):
            conn = None
            try:
                zk = ZK(device["ip"], port=4370, timeout=ZK_TIMEOUT, password=int(device.get("comm_key", 0) or 0), force_udp=True, ommit_ping=True)
                conn = zk.connect()
                conn.set_time(custom_dt)
            finally:
                if conn:
                    conn.disconnect()
        log_action(device["name"], "تعيين وقت مخصص", f"✓ إلى {custom_dt.strftime('%Y-%m-%d %H:%M')}")
        return jsonify({"success": True, "message": f"تم ضبط وقت ({device['name']}) بنجاح"})
    except Exception as e:
        log_action(device["name"], "تعيين وقت مخصص", f"✕ فشل: {e}")
        return jsonify({"success": False, "message": f"فشل: {e}"}), 500


# ===========================================================================
# تُستدعى مباشرة من WorkManager (Kotlin) بدون المرور عبر خادم Flask -
# تُستخدم للمزامنة المجدولة حتى لو كان التطبيق مغلقًا بالكامل.
# ===========================================================================

def run_fixed_time_sync(base_dir, schedule_id=None, time_str=None):
    """تُستدعى من WorkManager (Kotlin) عند حلول أحد الأوقات الثابتة لمجموعة
    جدولة معيّنة. تزامن فقط أجهزة تلك المجموعة."""
    global BASE_DIR
    BASE_DIR = base_dir
    sched = get_schedule_by_id(schedule_id) if schedule_id else None
    if not sched or not sched.get("enabled", True):
        return json.dumps([], ensure_ascii=False)
    alerts = sync_devices_collect_alerts(sched.get("device_ids"))
    return json.dumps(alerts, ensure_ascii=False)


# ===========================================================================
# وضع "المراقبة المستمرة" - حلقة تنفيذ دقيقة داخل Foreground Service حقيقي
# (بدل الاعتماد على WorkManager)، تفحص كل جدولة "فترة تكرار" في موعدها
# بالضبط وتفحص انحراف الوقت بشكل متكرر. الأوقات الثابتة تبقى دائمًا على
# WorkManager العادي بشكل مستقل تمامًا (لا تحتاج دقة إضافية أصلاً).
# فترة التكرار وكشف الانحراف في الخلفية أصبحا حصريًا هنا فقط - لا وجود
# لأي "نبضة موحّدة تقريبية" بديلة لهما بعد الآن.
# ===========================================================================

def run_precise_tick(base_dir):
    """تُستدعى من خدمة المراقبة المستمرة (ContinuousMonitoringService) كل
    دقيقة تقريبًا. تُرجع JSON فيه: alerts (رسائل تنبيه جديدة) + summary
    (حالة تزامن كل الأجهزة لعرضها في محتوى الإشعار)."""
    global BASE_DIR
    BASE_DIR = base_dir
    alerts = []
    now = datetime.now()

    # 1) الأوقات الثابتة - فحص دقيق للدقيقة الحالية بالضبط
    fired_state = _load_json("precise_fired_state.json", {})
    fired_changed = False
    current_hm = now.strftime("%H:%M")
    now_minute_key = now.strftime("%Y-%m-%d %H:%M")

    for sched in load_schedules():
        if not sched.get("enabled", True):
            continue
        sid = sched["id"]

        if sched.get("use_times") and current_hm in sched.get("times", []):
            fire_key = f"{sid}_{now_minute_key}"
            if not fired_state.get(fire_key):
                alerts += sync_devices_collect_alerts(sched.get("device_ids"))
                fired_state[fire_key] = True
                fired_changed = True

        # 2) فترة التكرار - نفس منطق النبضة العادية لكن يُفحص كل دقيقة
        # بدل كل 15-30 دقيقة، فالدقة أعلى بكثير
        if sched.get("use_interval"):
            tick_state = _load_json("interval_tick_state.json", {})
            last_str = tick_state.get(sid)
            due = True
            if last_str:
                try:
                    last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
                    due = (now - last_dt).total_seconds() >= sched.get("interval_minutes", 60) * 60
                except Exception:
                    due = True
            if due:
                alerts += sync_devices_collect_alerts(sched.get("device_ids"))
                tick_state[sid] = now.strftime("%Y-%m-%d %H:%M:%S")
                _save_json("interval_tick_state.json", tick_state)

    if fired_changed:
        # تنظيف دوري لمفاتيح "تم التنفيذ" القديمة حتى لا يكبر الملف بلا حدود
        cutoff = now - timedelta(minutes=5)
        pruned = {}
        for k, v in fired_state.items():
            try:
                ts = datetime.strptime(k.split("_", 1)[1], "%Y-%m-%d %H:%M")
                if ts >= cutoff:
                    pruned[k] = v
            except Exception:
                continue
        _save_json("precise_fired_state.json", pruned)

    # 3) كشف انحراف الوقت + تحديث حالة كل الأجهزة (لمحتوى الإشعار). الكشف
    # يعمل تلقائيًا دائمًا الآن (لا يوجد مفتاح تفعيل/إيقاف منفصل)، فالاتصال
    # الكامل بكل الأجهزة يحصل في كل نبضة طالما وضع المراقبة المستمرة نفسه
    # مفعّل (ثمن الإشعار الثابت مدفوع بالفعل، فالدقة الكاملة منطقية هنا).
    devices_status, drift_alerts = get_devices_with_live_status(full_all=True)
    alerts += drift_alerts

    online_count = sum(1 for d in devices_status if d["status"] == "online")

    # حالة التزامن الفعلية (وليست مجرد الاتصال) - بالاعتماد على علم
    # drift_active المحفوظ لكل جهاز في devices.json من check_drift_for_device.
    # "غير متصل" و"منحرف" حالتان منفصلتان تمامًا - جهاز غير متصل لا يُعتبر
    # "متزامنًا" أبدًا حتى لو لم يكن مسجَّلاً كمنحرف (لأننا ببساطة لا نملك
    # طريقة للتأكد من وقته الحالي وهو غير متصل).
    devices_map = {d["id"]: d for d in load_devices()}
    offline_names = [d["name"] for d in devices_status if d["status"] == "offline"]
    drifted_names = [
        d["name"] for d in devices_status
        if d["status"] == "online" and devices_map.get(d["id"], {}).get("drift_active")
    ]

    summary = {
        "online_count": online_count,
        "total": len(devices_status),
        "offline_names": offline_names,
        "drifted_names": drifted_names,
        "devices": [
            {
                "id": d["id"], "name": d["name"], "status": d["status"],
                "last_seen": d.get("device_time") or d.get("last_seen") or "",
            }
            for d in devices_status
        ],
    }

    return json.dumps({"alerts": alerts, "summary": summary}, ensure_ascii=False)


def run_single_device_sync(base_dir, device_id):
    """مزامنة جهاز واحد فقط - تُستدعى من زر المزامنة الصغير داخل الإشعار
    الموسّع، بدون فتح التطبيق."""
    global BASE_DIR
    BASE_DIR = base_dir
    device = get_device_by_id(device_id)
    if not device:
        return json.dumps({"success": False, "message": "الجهاز غير موجود"}, ensure_ascii=False)

    ok, msg = _sync_single_device(device)
    log_action(device["name"], "مزامنة", msg)
    if not ok:
        add_notification(device["name"], "فشل مزامنة", msg)
    return json.dumps({"success": ok, "message": msg}, ensure_ascii=False)


# ===========================================================================
# API: الجدولة (مجموعات - كل مجموعة مرتبطة بجهاز واحد أو أكثر)
# ===========================================================================

@app.route('/api/schedules', methods=['GET'])
def api_schedules_get():
    return jsonify({"schedules": load_schedules()})


@app.route('/api/schedules', methods=['POST'])
def api_schedules_create():
    data = request.json or {}
    device_ids = data.get("device_ids", [])
    if not device_ids:
        return jsonify({"success": False, "message": "اختر جهازًا واحدًا على الأقل"}), 400

    schedules = load_schedules()
    new_schedule = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "").strip() or "جدولة بدون اسم",
        "device_ids": device_ids,
        "enabled": True,
        "use_times": bool(data.get("use_times", False)),
        "times": data.get("times", []),
        "use_interval": bool(data.get("use_interval", False)),
        "interval_minutes": int(data.get("interval_minutes", 60)),
    }
    schedules.append(new_schedule)
    save_schedules(schedules)
    return jsonify({"success": True, "schedule": new_schedule})


@app.route('/api/schedules/<schedule_id>', methods=['DELETE'])
def api_schedules_delete(schedule_id):
    schedules = load_schedules()
    remaining = [s for s in schedules if s["id"] != schedule_id]
    if len(remaining) == len(schedules):
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404
    save_schedules(remaining)
    return jsonify({"success": True})


@app.route('/api/schedules/<schedule_id>', methods=['PUT'])
def api_schedules_update(schedule_id):
    """تعديل جدولة موجودة (بدلاً من حذفها وإضافة واحدة جديدة)."""
    data = request.json or {}
    device_ids = data.get("device_ids", [])
    if not device_ids:
        return jsonify({"success": False, "message": "اختر جهازًا واحدًا على الأقل"}), 400

    schedules = load_schedules()
    found = None
    for s in schedules:
        if s["id"] == schedule_id:
            s["name"] = data.get("name", "").strip() or "جدولة بدون اسم"
            s["device_ids"] = device_ids
            s["use_times"] = bool(data.get("use_times", False))
            s["times"] = data.get("times", [])
            s["use_interval"] = bool(data.get("use_interval", False))
            s["interval_minutes"] = int(data.get("interval_minutes", 60))
            found = s
            break
    if not found:
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404

    save_schedules(schedules)
    return jsonify({"success": True, "schedule": found})


@app.route('/api/schedules/<schedule_id>/toggle', methods=['POST'])
def api_schedules_toggle(schedule_id):
    """تفعيل/إيقاف فوري (حفظ تلقائي بدون الحاجة لزر حفظ منفصل)."""
    data = request.json or {}
    schedules = load_schedules()
    found = None
    for s in schedules:
        if s["id"] == schedule_id:
            s["enabled"] = bool(data.get("enabled", True))
            found = s
            break
    if not found:
        return jsonify({"success": False, "message": "الجدولة غير موجودة"}), 404
    save_schedules(schedules)
    return jsonify({"success": True, "schedule": found})


# ===========================================================================
# API: الإشعارات
# ===========================================================================

# ===========================================================================
# نقاط داخلية (Internal) - تُستدعى فقط من ContinuousMonitoringService عبر طلب
# شبكة محلي (بدل فتح اتصال بايثون منفصل من خيط مستقل، وهو ما كان يسبب
# تعطّلاً كاملاً للتطبيق نتيجة تعارض في طبقة الربط بين Kotlin وبايثون).
# ===========================================================================

@app.route('/api/internal/precise_tick', methods=['GET'])
def api_internal_precise_tick():
    result_json = run_precise_tick(BASE_DIR)
    return Response(result_json, mimetype="application/json")


@app.route('/api/internal/single_device_sync/<device_id>', methods=['GET'])
def api_internal_single_device_sync(device_id):
    result_json = run_single_device_sync(BASE_DIR, device_id)
    return Response(result_json, mimetype="application/json")


@app.route('/api/notifications', methods=['GET'])
def api_notifications_get():
    return jsonify({"notifications": get_notifications()})


@app.route('/api/notifications/read', methods=['POST'])
def api_notifications_read():
    mark_notifications_read()
    return jsonify({"success": True})


@app.route('/api/notifications/clear', methods=['POST'])
def api_notifications_clear():
    clear_notifications()
    return jsonify({"success": True})


# ===========================================================================
# API: السجل
# ===========================================================================

@app.route('/api/history', methods=['GET'])
def api_history_get():
    return jsonify({"history": read_history(50)})


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    path = _path(HISTORY_FILE)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"success": True})


# ===========================================================================
# API: الإعدادات
# ===========================================================================

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(load_settings())


@app.route('/api/settings', methods=['POST'])
def api_settings_set():
    data = request.json or {}
    settings = load_settings()
    settings.update({k: v for k, v in data.items() if k in default_settings()})
    save_settings(settings)
    return jsonify({"success": True, "settings": settings})


# ===========================================================================
# نقطة الدخول
# ===========================================================================

def start(base_dir=None):
    """نقطة الدخول التي يستدعيها تطبيق أندرويد (MainActivity.kt) عبر Chaquopy."""
    global BASE_DIR
    if base_dir:
        BASE_DIR = base_dir
    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        app.run(host="127.0.0.1", port=5000, threaded=True)
    except SystemExit:
        # يحدث لو كان خادم سابق لا يزال شغّالًا فعليًا على نفس المنفذ
        # (مثلاً بعد إعادة إنشاء الشاشة من غير ما تُقفل العملية بالكامل) -
        # هذا متوقع وغير ضار: الخادم الأصلي لا يزال يعمل بشكل طبيعي.
        pass
    except OSError:
        pass


if __name__ == '__main__':
    start()
