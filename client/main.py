# ESP32 MicroPython —— DS18B20 + 双按钮 + 灯带 + I2C LCD + UDP上报（稳定版）
# - WiFi 连接更稳：连上即打印 & 首包心跳；断线指数回退重连
# - DS18B20 启动先同步采样一次，随后异步周期采样（12-bit 需 ~750ms）
# - UDP 上报：域名解析并缓存；按钮立刻触发一次上报，便于联调

import network, socket, machine, time
from machine import Pin, SoftI2C
import neopixel

# ==== 若固件未内置 onewire/ds18x20，请先上传同名 .py 到设备根目录 ====
from onewire import OneWire
import ds18x20

# ---------------- 基本配置 ----------------
HOST  = "temp.heilomeow.com"   # ✅ 你的公网域名
PORT  = 8080                   # ✅ UDP端口
SSID  = "Taffy"                # ✅ WiFi SSID
PSK   = "11235813"             # ✅ WiFi 密码
LOCAL_IP = None                # 用DHCP最稳；如需静态: ("192.168.x.x","255.255.255.0","网关","DNS")

LEDSTRIP_PIN_NUM = 4
NUM_LEDS         = 25
I2C_SDA, I2C_SCL = 13, 14

# 温度传感器（DS18B20）
TEMP_PIN       = 5
TEMP_PERIOD_S  = 1.0
TEMP_TCONV_MS  = 750  # 12-bit 转换典型 750ms（datasheet/驱动文档）  # noqa

# 温度→颜色映射
TEMP_MIN = 20
TEMP_MID = 26.0
TEMP_MAX = 32.0
COLD_RGB = (0, 0, 255)
MID_RGB  = (0, 255, 0)
WARM_RGB = (255, 0, 0)
DIMMER   = 0.18
USE_GAMMA= False
GAMMA    = 2.2

# 按钮与投票
BTN1_PIN, BTN2_PIN = 32, 33
DEBOUNCE_MS = 60
VOTE_MIN, VOTE_MAX = -1, 1
vote_val = 0

# 上报节流
SEND_INTERVAL_S = 2.0

# Wi-Fi 非阻塞状态机
CONNECT_TIMEOUT_MS = 8000
RETRY_BASE_MS      = 5000
RETRY_MAX_MS       = 60000
_wifi_state     = 'idle'    # 'idle' | 'connecting' | 'connected'
_wifi_deadline  = 0
_wifi_next_try  = 0
_wifi_backoff   = RETRY_BASE_MS
_wifi_ip_set    = False

# ---------------- 全局对象/状态 ----------------
uid_hex = machine.unique_id().hex()
wlan = network.WLAN(network.STA_IF)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.0)  # 非阻塞发送
_last_send_ms = 0
_peer_addr = None      # 缓存 (ip, port)
_peer_addr_ts = 0      # 上次解析时间
_PEER_TTL_MS = 5 * 60 * 1000  # 解析缓存5分钟

# NeoPixel
np = neopixel.NeoPixel(Pin(LEDSTRIP_PIN_NUM, Pin.OUT), NUM_LEDS, bpp=3, timing=1)  # 800KHz
# :contentReference[oaicite:3]{index=3}

# I2C LCD（i2c_lcd.py 需在设备上）
i2c = SoftI2C(sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=100000)
try:
    from i2c_lcd import I2cLcd
    addrs = i2c.scan()
    if not addrs: raise OSError("No I2C addr found")
    i2c_lcd = I2cLcd(i2c, addrs[0], 2, 16)
    i2c_lcd.clear()
except Exception as e:
    i2c_lcd = None
    print("LCD init failed:", e)

last_line1 = ""
last_line2 = ""

# DS18B20：异步采样状态
ow = OneWire(Pin(TEMP_PIN))
ds = ds18x20.DS18X20(ow)
_roms = ds.scan()
_temp_pending = False
_temp_conv_ms = 0
_next_temp_start_ms = 0
last_temp = None  # 最近温度

# ---------------- LCD/显示 ----------------
def display_line1(s: str):
    global last_line1
    if i2c_lcd is None: return
    if s != last_line1:
        i2c_lcd.move_to(0, 0); i2c_lcd.putstr(" "*16)
        i2c_lcd.move_to(0, 0); i2c_lcd.putstr(s[:16])
        last_line1 = s

def display_line2(s: str):
    global last_line2
    if i2c_lcd is None: return
    if s != last_line2:
        i2c_lcd.move_to(0, 1); i2c_lcd.putstr(" "*16)
        i2c_lcd.move_to(0, 1); i2c_lcd.putstr(s[:16])
        last_line2 = s

def vote_to_tag(v):
    v = 0 if v is None else int(v)
    if v > 0:   return "Warm"
    if v < 0:   return "Cold"
    return "Comfort"

def render_temp_line(t, v):
    s_t = "--.-" if t is None else "{:.1f}".format(t)
    display_line2("{}\xdfC {}".format(s_t, vote_to_tag(v)))

# ---------------- 工具 ----------------
def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def render_vote_line(v):
    if v > 0:   display_line2("Too Warm  (^_^')")
    elif v < 0: display_line2("Too Cold  (>_<)")
    else:       display_line2("Comfort  (^-^)")

def lerp(a, b, u):  return a + (b - a) * u
def lerp3(c1, c2, u):
    return (int(lerp(c1[0], c2[0], u)),
            int(lerp(c1[1], c2[1], u)),
            int(lerp(c1[2], c2[2], u)))

def apply_dim_and_gamma(rgb):
    r, g, b = rgb
    r = int(r * DIMMER); g = int(g * DIMMER); b = int(b * DIMMER)
    if USE_GAMMA:
        r = int((r/255.0)**GAMMA * 255)
        g = int((g/255.0)**GAMMA * 255)
        b = int((b/255.0)**GAMMA * 255)
    return (max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b)))

def color_from_temp(t):
    if t is None: return (0,0,0)
    t = clamp(t, TEMP_MIN, TEMP_MAX)
    if t <= TEMP_MID:
        u = (t - TEMP_MIN)/(TEMP_MID - TEMP_MIN) if TEMP_MID>TEMP_MIN else 0.0
        rgb = lerp3(COLD_RGB, MID_RGB, u)
    else:
        u = (t - TEMP_MID)/(TEMP_MAX - TEMP_MID) if TEMP_MAX>TEMP_MID else 0.0
        rgb = lerp3(MID_RGB, WARM_RGB, u)
    return apply_dim_and_gamma(rgb)

# ---------------- 按钮（IRQ + 软消抖） ----------------
class Button:
    def __init__(self, pin_num, on_press, active_low=True):
        self.pin = Pin(pin_num, Pin.IN, Pin.PULL_UP if active_low else Pin.PULL_DOWN)
        trig = Pin.IRQ_FALLING if active_low else Pin.IRQ_RISING
        self.on_press = on_press
        self._last_ms = 0
        self.pin.irq(trigger=trig, handler=self._irq)

    def _irq(self, p):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_ms) < DEBOUNCE_MS:
            return
        self._last_ms = now
        try:
            self.on_press()
        except Exception as e:
            print("btn err:", e)

def set_vote(delta):
    global vote_val
    vote_val = clamp(vote_val + delta, VOTE_MIN, VOTE_MAX)
    render_vote_line(vote_val)
    try_send(last_temp, vote_val, force=True)  # ✅ 按一下立刻上报一包

BTN1 = Button(BTN1_PIN, lambda: set_vote(+1), active_low=True)
BTN2 = Button(BTN2_PIN, lambda: set_vote(-1), active_low=True)

# ---------------- DS18B20 采样 ----------------
def temp_tick():
    """异步：发起 convert_temp()；等待 TEMP_TCONV_MS；read_temp()；刷新 last_temp 与 LCD"""
    global _temp_pending, _temp_conv_ms, _next_temp_start_ms, last_temp
    if not _roms:
        return
    now = time.ticks_ms()
    if not _temp_pending:
        if time.ticks_diff(now, _next_temp_start_ms) >= 0:
            try:
                ds.convert_temp()
                _temp_pending = True
                _temp_conv_ms = now
            except Exception as e:
                print("convert_temp err:", e)
        return
    if time.ticks_diff(now, _temp_conv_ms) >= TEMP_TCONV_MS:
        try:
            t = ds.read_temp(_roms[0])
            if isinstance(t, (int, float)):
                last_temp = t
                render_temp_line(last_temp, vote_val)
        except Exception as e:
            print("read_temp err:", e)
        finally:
            _temp_pending = False
            _next_temp_start_ms = now + int(TEMP_PERIOD_S * 1000)

# ---------------- 灯带刷新 ----------------
def update_led_from_temp(t):
    try:
        r,g,b = color_from_temp(t)
        for i in range(NUM_LEDS):
            np[i] = (r,g,b)
        np.write()
    except Exception:
        pass

# ---------------- 网络 / UDP 上报 ----------------
def _resolve_peer(force=False):
    """解析并缓存 (HOST, PORT) → (ip, port)"""
    global _peer_addr, _peer_addr_ts
    now = time.ticks_ms()
    if (not force) and _peer_addr and time.ticks_diff(now, _peer_addr_ts) < _PEER_TTL_MS:
        return _peer_addr
    try:
        _peer_addr = socket.getaddrinfo(HOST, PORT)[0][-1]
        _peer_addr_ts = now
    except Exception as e:
        print("DNS err:", e)
        _peer_addr = None
    return _peer_addr

def wifi_connect():
    global _wifi_state, _wifi_deadline, _wifi_ip_set, _wifi_backoff, _wifi_next_try
    try:
        wlan.active(True)  # STA模式  :contentReference[oaicite:4]{index=4}
        if (not _wifi_ip_set) and LOCAL_IP:
            try:
                wlan.ifconfig(LOCAL_IP)
                _wifi_ip_set = True
            except Exception:
                pass
        wlan.connect(SSID, PSK)     # 连接AP  :contentReference[oaicite:5]{index=5}
        _wifi_state = 'connecting'
        _wifi_deadline = time.ticks_ms() + CONNECT_TIMEOUT_MS
        display_line1("WiFi connecting...")
    except Exception as e:
        print("wifi_connect err:", e)
        _wifi_state = 'idle'
        _wifi_next_try = time.ticks_ms() + _wifi_backoff
        _wifi_backoff = min(_wifi_backoff * 2, RETRY_MAX_MS)
        display_line1("WiFi retrying")

def ensure_wifi():
    global _wifi_state, _wifi_deadline, _wifi_next_try, _wifi_backoff
    now = time.ticks_ms()
    if wlan.isconnected():
        if _wifi_state != 'connected':
            _wifi_state = 'connected'
            _wifi_backoff = RETRY_BASE_MS
            try:
                print("Wi-Fi OK:", wlan.ifconfig())
            except:
                print("Wi-Fi OK")
            display_line1("WiFi Connected")
            # 刚连上，立刻解析一次域名并发首包心跳
            _resolve_peer(force=True)
            try_send(last_temp, vote_val, force=True)
        return True
    if _wifi_state == 'connecting':
        if time.ticks_diff(now, _wifi_deadline) > 0:
            try:
                if hasattr(wlan, "disconnect"):
                    wlan.disconnect()
            except:
                pass
            _wifi_state = 'idle'
            _wifi_next_try = now + _wifi_backoff
            print("Wi-Fi timeout, retry in %ds" % int(_wifi_backoff/1000))
            display_line1("WiFi retrying")
            _wifi_backoff = min(_wifi_backoff * 2, RETRY_MAX_MS)
        return False
    if time.ticks_diff(now, _wifi_next_try) >= 0:
        wifi_connect()
    return False

def try_send(temp_c, vote, force=False):
    """定期/强制上报 uid+温度+vote（UDP）"""
    global _last_send_ms
    if not wlan.isconnected():
        return
    now = time.ticks_ms()
    if (not force) and time.ticks_diff(now, _last_send_ms) < int(SEND_INTERVAL_S * 1000):
        return
    _last_send_ms = now

    # 若首次还没温度，启动时已同步采样过；理论上很快会有 t
    # 若仍 None，也照样发（用 "--" 占位），便于服务端识别心跳
    try:
        if temp_c is None:
            pkt = "{}:temp:{}:vote:{}".format(uid_hex, "", int(vote))
        else:
            pkt = "{}:temp:{:.2f}:vote:{}".format(uid_hex, float(temp_c), int(vote))

        peer = _resolve_peer()
        if not peer:
            return
        sock.sendto(pkt.encode(), peer)     # UDP sendto   :contentReference[oaicite:6]{index=6}
        print("[SEND]", pkt)
    except Exception as e:
        print("send err:", e)

# ---------------- 启动与主循环 ----------------
print("Board UID:", uid_hex)
display_line1("Loading...")
display_line2("UID:"+uid_hex[:10])
time.sleep(1.0)
display_line2("")

# 启动时同步采样一轮 → 避免前几秒 last_temp=None
try:
    print("DS18B20 roms:", _roms)
    if not _roms:
        print("WARN: No DS18B20 on pin", TEMP_PIN)
    else:
        ds.convert_temp()
        time.sleep_ms(TEMP_TCONV_MS)  # 12-bit 转换典型 750ms  :contentReference[oaicite:7]{index=7}
        t0 = ds.read_temp(_roms[0])
        if isinstance(t0, (int, float)):
            last_temp = t0
            print("First temp:", last_temp)
            render_temp_line(last_temp, vote_val)
except Exception as e:
    print("First temp read err:", e)

wifi_connect()

try:
    while True:
        # 温度 → 灯带 & LCD
        temp_tick()
        update_led_from_temp(last_temp)

        # 网络（不阻塞）
        ensure_wifi()
        try_send(last_temp, vote_val)

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\nExiting")
    display_line1("Exiting...")

finally:
    try: sock.close(); print("Socket closed")
    except: pass
    try: np.fill((0,0,0)); np.write(); print("LED cleared")
    except: pass
    try: display_line1("Bye Bye~"); display_line2("")
    except: pass

