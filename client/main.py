# main.py —— DS18B20 温度 + 双按钮反馈 + 灯带配色 + LCD + UDP上报
# 去除：舵机 / TTP223 / 超声波。新增：BTN1(+1)、BTN2(-1) 反馈计数（-1..0..+1）
# Wi-Fi 非阻塞重连；温度/按钮本地功能不受网络影响；DS18B20 非阻塞采样。

import network, socket, machine, time
from machine import Pin, SoftI2C
import neopixel

# ==== 若固件未内置 onewire/ds18x20，请先上传同名 .py 到设备根目录 ====
from onewire import OneWire
import ds18x20

# ---------------- 基本配置 ----------------
HOST  = "192.168.137.1"     # 服务器 IP
PORT  = 8080                # 服务器端口（UDP）
SSID  = "Taffy"             # Wi-Fi SSID
PSK   = "11235813"
LOCAL_IP = ("192.168.137.234","255.255.255.0","192.168.137.1","8.8.8.8")

LEDSTRIP_PIN_NUM = 4
NUM_LEDS         = 25       # 实际灯珠数
I2C_SDA, I2C_SCL = 13, 14   # I2C LCD

# 温度传感器（DS18B20）
TEMP_PIN       = 5          # OneWire 在 Pin5
TEMP_PERIOD_S  = 1.0        # 采样周期（s）
TEMP_TCONV_MS  = 750        # 12-bit 转换时间（ms），DS18B20 要求

# —— 温度范围（演示：10~42，26为中点）——
TEMP_MIN = 20
TEMP_MID = 26.0
TEMP_MAX = 32.0

# —— 灯带颜色端点（冷 / 中性 / 暖）——
# 可自行微调端点色；亮度不随温度变，仅用 DIMMER 做整体限亮
COLD_RGB = (0, 0, 255)     # 冷端（偏蓝青）
MID_RGB  = (0, 255, 0)  # 中点（中性浅灰/白，更不偏色）
WARM_RGB = (255, 0, 0)     # 暖端（琥珀橙）

# —— 全局亮度因子（不随温度变化）——
DIMMER   = 0.18             # 图书馆环境可 0.12~0.20
USE_GAMMA= False            # 需要时打开感知伽马
GAMMA    = 2.2

# —— 双按钮（GPIO32/33），内部上拉，按键另一端接 GND —— 
BTN1_PIN, BTN2_PIN = 32, 33
DEBOUNCE_MS = 60

# —— 反馈计数器 —— 
VOTE_MIN, VOTE_MAX = -1, 1
vote_val = 0                # 初始 0

# UDP 上报节流
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
sock.settimeout(0.0)  # 仅发送
_last_send_ms = 0

# NeoPixel
np = neopixel.NeoPixel(Pin(LEDSTRIP_PIN_NUM, Pin.OUT), NUM_LEDS, bpp=3, timing=1)

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

# DS18B20：非阻塞采样状态
ow = OneWire(Pin(TEMP_PIN))
ds = ds18x20.DS18X20(ow)
_roms = ds.scan()
_temp_pending = False
_temp_conv_ms = 0
_next_temp_start_ms = 0
last_temp = None  # 最近温度

# ---------------- LCD 辅助 ----------------
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
    if v > 0:
        return "Warm"
    elif v < 0:
        return "Cold"
    else:
        return "Confort"   # comfortable 简写

def render_temp_line(t, v):
    # LCD 第2行：T: 25.3°C V:warm/cold/conf
    s_t = "--.-" if t is None else "{:.1f}".format(t)
    display_line2("{}\xdfC {}".format(s_t, vote_to_tag(v)))


# ---------------- 通用工具 ----------------
def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def render_vote_line(v):
    # -1 => Cold, 0 => 舒适, +1 => Warm
    if v > 0:
        display_line2("Too Warm  (^_^')")
    elif v < 0:
        display_line2("Too Cold  (>_<)")
    else:
        display_line2("Comfort  (^-^)")

def lerp(a, b, u):  # 标量插值
    return a + (b - a) * u

def lerp3(c1, c2, u):  # 颜色三通道插值
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
    """发散配色：MIN..MID: COLD→MID；MID..MAX: MID→WARM；亮度恒由 DIMMER 控制"""
    if t is None: return (0,0,0)
    t = clamp(t, TEMP_MIN, TEMP_MAX)
    if t <= TEMP_MID:
        u = (t - TEMP_MIN)/(TEMP_MID - TEMP_MIN) if TEMP_MID>TEMP_MIN else 0.0
        rgb = lerp3(COLD_RGB, MID_RGB, u)
    else:
        u = (t - TEMP_MID)/(TEMP_MAX - TEMP_MID) if TEMP_MAX>TEMP_MID else 0.0
        rgb = lerp3(MID_RGB, WARM_RGB, u)
    return apply_dim_and_gamma(rgb)

# ---------------- 双按钮（消抖 + 短/长按） ----------------
class Button:
    def __init__(self, pin_num, on_press, active_low=True):
        self.pin = Pin(pin_num, Pin.IN, Pin.PULL_UP if active_low else Pin.PULL_DOWN)
        self.active_low = active_low
        self.on_press = on_press
        self._last_ms = 0
        # 只监听“按下”那一侧的边沿：active_low 用 FALLING，active_high 用 RISING
        trig = Pin.IRQ_FALLING if active_low else Pin.IRQ_RISING
        self.pin.irq(trigger=trig, handler=self._irq)

    def _irq(self, p):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_ms) < DEBOUNCE_MS:
            return
        self._last_ms = now
        # 到这里基本可以认为是一次“短按”
        try:
            self.on_press()
        except:
            pass

def set_vote(delta):
    global vote_val
    vote_val = clamp(vote_val + delta, VOTE_MIN, VOTE_MAX)  # 夹 [-1,0,+1]
    render_vote_line(vote_val)  # 覆盖第二行为 Warm/Cold/舒适
    # 温度行会在下一次 temp_tick() 读取成功后自动刷回

BTN1 = Button(BTN1_PIN, lambda: set_vote(+1), active_low=True)
BTN2 = Button(BTN2_PIN, lambda: set_vote(-1), active_low=True)

# ---------------- DS18B20 非阻塞采样 ----------------
def temp_tick():
    """定时发起 convert_temp()；满 750ms 再 read_temp()；刷新 last_temp 与 LCD"""
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
    # 等待转换完成
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

# ---------------- 灯带更新 ----------------
def update_led_from_temp(t):
    try:
        r,g,b = color_from_temp(t)
        for i in range(NUM_LEDS):
            np[i] = (r,g,b)
        np.write()
    except Exception:
        pass

# ---------------- 网络（非阻塞 Wi-Fi 状态机 + UDP 上报） ----------------
def wifi_connect():
    global _wifi_state, _wifi_deadline, _wifi_ip_set, _wifi_backoff, _wifi_next_try
    try:
        wlan.active(True)
        if not _wifi_ip_set:
            try:
                wlan.ifconfig(LOCAL_IP)  # 静态 IP 仅设一次
                _wifi_ip_set = True
            except Exception:
                pass
        wlan.connect(SSID, PSK)         # 发起一次连接，不等待
        _wifi_state = 'connecting'
        _wifi_deadline = time.ticks_ms() + CONNECT_TIMEOUT_MS
        display_line1("WiFi connecting...")
    except Exception:
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
            try: print("Wi-Fi OK:", wlan.ifconfig())
            except: print("Wi-Fi OK")
            display_line1("WiFi Connected")
        return True
    if _wifi_state == 'connecting':
        if time.ticks_diff(now, _wifi_deadline) > 0:
            try:
                if hasattr(wlan, "disconnect"):
                    wlan.disconnect()
            except: pass
            _wifi_state = 'idle'
            _wifi_next_try = now + _wifi_backoff
            print("Wi-Fi timeout, retry in %ds" % int(_wifi_backoff/1000))
            display_line1("WiFi retrying")
            _wifi_backoff = min(_wifi_backoff * 2, RETRY_MAX_MS)
        return False
    if time.ticks_diff(now, _wifi_next_try) >= 0:
        wifi_connect()
    return False

def try_send(temp_c, vote):
    """定期上报 uid+温度+vote（UDP），失败静默"""
    global _last_send_ms
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_send_ms) < int(SEND_INTERVAL_S * 1000):
        return
    _last_send_ms = now
    if temp_c is None:
        return
    try:
        pkt = "{}:temp:{:.2f}:vote:{}".format(uid_hex, float(temp_c), int(vote))
        sock.sendto(pkt.encode(), (HOST, PORT))
        # print("[SEND]", pkt)
    except Exception:
        pass

# ---------------- 启动与主循环 ----------------
print("Board UID:", uid_hex)
display_line1("Loading...")
display_line2("UID:"+uid_hex[:10])
time.sleep(1.2)
display_line2("")

wifi_connect()

try:
    while True:

        # 温度 → 灯带
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
    # 清理
    try: sock.close(); print("Socket closed")
    except: pass
    try: np.fill((0,0,0)); np.write(); print("LED cleared")
    except: pass
    try: display_line1("Bye Bye~"); display_line2("")
    except: pass
