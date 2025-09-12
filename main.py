# main.py  —— 温度传感器版（DS18B20@Pin5）
# 功能：温度 → 伺服角度、整条灯带颜色/亮度、LCD 第2行；UDP 实时上报 UID+温度
# 断网/服务器不可达 ≠ 影响本地功能（非阻塞，全面 try/except 保护）

import network, socket, machine, time
from machine import Pin, SoftI2C, PWM
import neopixel

# ==== 可选：如固件未内置 onewire/ds18x20，请先上传同名 .py 到设备根目录 ====
from onewire import OneWire
import ds18x20

# ---------------- 基本配置 ----------------
HOST  = "192.168.137.1"     # 服务器 IP
PORT  = 8080                # 服务器端口（UDP）
SSID  = "Taffy"             # Wi-Fi
PSK   = "11235813"
LOCAL_IP = ("192.168.137.234","255.255.255.0","192.168.137.1","8.8.8.8")

LEDSTRIP_PIN_NUM = 4
NUM_LEDS         = 25       # 实际灯珠数
SERVO_PIN        = 19       # 舵机 PWM
I2C_SDA, I2C_SCL = 13, 14   # I2C LCD

# 温度传感器（DS18B20）
TEMP_PIN     = 5            # OneWire 在 Pin 5
TEMP_PERIOD_S = 1.0         # 每 2 秒刷新一次温度
TEMP_TCONV_MS = 750         # 12-bit 转换时间上限（datasheet）

# —— 温度范围（0~52，26 为中点）——
TEMP_MIN = 10.0
TEMP_MID = 26.0
TEMP_MAX = 42.0

# —— 灯带颜色端点（冷极 / 中性 / 暖极）——
# 冷极：偏蓝青、压低绿避免刺眼；暖极：琥珀橙
COLD_RGB = (0, 48, 255)     # 0 °C
MID_RGB  = (0, 255, 0)  # 26 °C（中性灰白，避免偏冷/偏暖）
WARM_RGB = (255, 0, 0)     # 52 °C

# —— 全局亮度因子（不随温度变化，只整体偏暗；0.12~0.25 之间调）——
DIMMER = 0.12

# 可选：开启伽马校正让低亮度色感更自然（LED人眼感知非线性）
USE_GAMMA = False  # 如需更自然，把 False 改 True
GAMMA = 2.2        # 常用 2.0~2.4（参考 Adafruit 的 Gamma 校正指导）


# 舵机映射范围（与您之前保持一致）
SERVO_MIN = 25
SERVO_MAX = 115
SERVO_RESET_DUTY = 25

# —— 长按电源键设置（TTP223 on PIN15）——
POWER_BTN_PIN   = 15
LONG_PRESS_MS   = 1200   # 长按判定阈值
DEBOUNCE_MS     = 80     # 基础消抖

system_on       = True   # 当前是否运行（False=休眠）
_last_irq_ms    = 0
_press_t0       = None   # 按下起始时间戳
_press_armed    = True   # 只触发一次，松手后再武装

# UDP 上报间隔
SEND_INTERVAL_S = 2.0

CONNECT_TIMEOUT_MS = 8000          # 一次连接的超时时间
RETRY_BASE_MS      = 5000          # 初始回退
RETRY_MAX_MS       = 60000         # 最大回退(1分钟)

# Wi-Fi 状态机
_wifi_state     = 'idle'           # 'idle' | 'connecting' | 'connected'
_wifi_deadline  = 0
_wifi_next_try  = 0
_wifi_backoff   = RETRY_BASE_MS
_wifi_ip_set    = False

# ---------------- 全局状态 ----------------
uid_hex = machine.unique_id().hex()
wlan = network.WLAN(network.STA_IF)

last_line1 = ""
last_line2 = ""

_last_send_ms = 0
_last_reg_ms  = 0  # 兼容以前逻辑（现在只发温度，不必“注册”也无妨）

# DS18B20 状态机
ow = OneWire(Pin(TEMP_PIN))
ds = ds18x20.DS18X20(ow)
_roms = ds.scan()      # 扫描总线上探头
_temp_pending = False  # 是否已发起 convert_temp
_temp_conv_ms = 0
_next_temp_start_ms = 0
last_temp = None       # 最近一次测得温度（°C）

# 灯带
np = neopixel.NeoPixel(Pin(LEDSTRIP_PIN_NUM, Pin.OUT), NUM_LEDS, bpp=3, timing=1)

# 舵机
servo = PWM(Pin(SERVO_PIN), freq=50)
servo.duty(SERVO_RESET_DUTY)

# I2C LCD
i2c = SoftI2C(sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=100000)
try:
    from i2c_lcd import I2cLcd
    addr_list = i2c.scan()
    if not addr_list:
        raise OSError("No I2C addr found")
    i2c_lcd = I2cLcd(i2c, addr_list[0], 2, 16)
    i2c_lcd.clear()
except Exception as e:
    i2c_lcd = None
    print("LCD init failed:", e)

# ---------------- LCD 辅助 ----------------
def display_line1(s: str):
    global last_line1
    if i2c_lcd is None: return
    if s != last_line1:
        i2c_lcd.move_to(0, 0)
        i2c_lcd.putstr(" " * 16)
        i2c_lcd.move_to(0, 0)
        i2c_lcd.putstr(s[:16])
        last_line1 = s

def display_line2(s: str):
    global last_line2
    if i2c_lcd is None: return
    if s != last_line2:
        i2c_lcd.move_to(0, 1)
        i2c_lcd.putstr(" " * 16)
        i2c_lcd.move_to(0, 1)
        i2c_lcd.putstr(s[:16])
        last_line2 = s

# ---------------- 网络 ----------------
def wifi_connect():
    global _wifi_state, _wifi_deadline, _wifi_ip_set, _wifi_backoff, _wifi_next_try
    try:
        wlan.active(True)
        if not _wifi_ip_set:
            try:
                wlan.ifconfig(LOCAL_IP)  # 静态 IP 只设一次
                _wifi_ip_set = True
            except Exception:
                pass
        # 发起一次连接，不等待
        wlan.connect(SSID, PSK)
        _wifi_state = 'connecting'
        _wifi_deadline = time.ticks_ms() + CONNECT_TIMEOUT_MS
        display_line1("WiFi connecting...")
    except Exception as e:
        # 启动连接都失败了 → 安排下次重试
        _wifi_state = 'idle'
        _wifi_next_try = time.ticks_ms() + _wifi_backoff
        _wifi_backoff = min(_wifi_backoff * 2, RETRY_MAX_MS)
        print("WiFi connectting...")
        display_line1("WiFi retrying")


# 单独的 UDP socket（仅发送）
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.0)  # 非阻塞

def ensure_wifi():
    global _wifi_state, _wifi_deadline, _wifi_next_try, _wifi_backoff
    now = time.ticks_ms()

    if wlan.isconnected():
        if _wifi_state != 'connected':
            _wifi_state = 'connected'
            _wifi_backoff = RETRY_BASE_MS  # 成功后回退复位
            try:
                print("Wi-Fi OK:", wlan.ifconfig())
            except:
                print("Wi-Fi OK")
            display_line1("WiFi OK")
        return True

    # 未连上
    if _wifi_state == 'connecting':
        # 连接中但超时 → 进入 idle，安排回退重试
        if time.ticks_diff(now, _wifi_deadline) > 0:
            try:
                if hasattr(wlan, "disconnect"):
                    wlan.disconnect()
            except:
                pass
            _wifi_state = 'idle'
            _wifi_next_try = now + _wifi_backoff
            print("Wi-Fi: connect timeout, retry in %ds" % int(_wifi_backoff/1000))
            display_line1("WiFi retrying")
            _wifi_backoff = min(_wifi_backoff * 2, RETRY_MAX_MS)
        return False

    # idle 状态：到点才发起下一次连接尝试（不刷屏）
    if time.ticks_diff(now, _wifi_next_try) >= 0:
        wifi_connect()
    return False
def try_send_temp(temp_c):
    """定期上报 uid+温度（UDP），失败不影响本地运行"""
    global _last_send_ms
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_send_ms) < int(SEND_INTERVAL_S * 1000):
        return
    _last_send_ms = now
    if temp_c is None:
        return
    try:
        pkt = "{}:temp:{:.2f}".format(uid_hex, float(temp_c))
        sock.sendto(pkt.encode(), (HOST, PORT))
        # print("[SEND]", pkt)
    except Exception as e:
        # 静默失败，保持本地功能
        # print("send fail:", e)
        pass

# ---------------- 通用工具 ----------------
def clamp(v, lo, hi):
    if v < lo: return lo
    if v > hi: return hi
    return v

def map_value(x, in_min, in_max, out_min, out_max):
    if x is None:
        return out_min
    if in_max == in_min:
        return out_min
    x = clamp(x, in_min, in_max)
    ratio = (x - in_min) / (in_max - in_min)
    return int(out_min + ratio * (out_max - out_min))

# ---------------- 温度读取（非阻塞） ----------------
def temp_tick():
    """
    定时发起转换 → 750ms 后读取温度（不阻塞主循环）
    成功读取后：更新 last_temp，并刷新 LCD 第2行
    """
    global _temp_pending, _temp_conv_ms, _next_temp_start_ms, last_temp

    if not _roms:
        # 没探头就不做事
        return

    now = time.ticks_ms()

    if not _temp_pending:
        # 到了下一次采样时间，发起转换
        if time.ticks_diff(now, _next_temp_start_ms) >= 0:
            try:
                ds.convert_temp()  # 所有探头一起开始转换
                _temp_pending = True
                _temp_conv_ms = now
            except Exception as e:
                print("convert_temp err:", e)
        return

    # 已经在等待转换完成
    if time.ticks_diff(now, _temp_conv_ms) >= TEMP_TCONV_MS:
        try:
            t = ds.read_temp(_roms[0])   # 只读第一个
            if isinstance(t, (int, float)):
                last_temp = t
                display_line2("Temp:{:5.1f}\xdfC".format(t))
        except Exception as e:
            print("read_temp err:", e)
        finally:
            _temp_pending = False
            _next_temp_start_ms = now + int(TEMP_PERIOD_S * 1000)

# ---------------- 输出联动（温度→伺服/灯带） ----------------
def clamp(v, lo, hi):
    if v < lo: return lo
    if v > hi: return hi
    return v

def lerp(a, b, u):  # 标量线性插值
    return a + (b - a) * u

def lerp3(c1, c2, u):  # 颜色三通道插值（返回 int）
    return (
        int(lerp(c1[0], c2[0], u)),
        int(lerp(c1[1], c2[1], u)),
        int(lerp(c1[2], c2[2], u)),
    )

def apply_dim_and_gamma(rgb):
    # 先全局限亮，再可选伽马校正（把 0~255 归一化后做幂次）
    r, g, b = rgb
    r = int(r * DIMMER); g = int(g * DIMMER); b = int(b * DIMMER)
    if USE_GAMMA:
        # 简易伽马：x' = (x/255)^gamma * 255 —— 更贴近人眼（Adafruit 建议）
        r = int((r/255.0) ** GAMMA * 255)
        g = int((g/255.0) ** GAMMA * 255)
        b = int((b/255.0) ** GAMMA * 255)
    # 裁剪到 0~255
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

def color_from_temp(t):
    """
    发散配色（diverging）：
    0..26 °C 从 COLD_RGB → MID_RGB
    26..52 °C 从 MID_RGB  → WARM_RGB
    仅做颜色插值，不映射亮度；亮度由 DIMMER 全局控制
    """
    if t is None:
        return (0, 0, 0)
    t = clamp(t, TEMP_MIN, TEMP_MAX)

    if t <= TEMP_MID:
        u = (t - TEMP_MIN) / (TEMP_MID - TEMP_MIN) if TEMP_MID > TEMP_MIN else 0.0
        rgb = lerp3(COLD_RGB, MID_RGB, u)
    else:
        u = (t - TEMP_MID) / (TEMP_MAX - TEMP_MID) if TEMP_MAX > TEMP_MID else 0.0
        rgb = lerp3(MID_RGB, WARM_RGB, u)

    return apply_dim_and_gamma(rgb)


def update_outputs_from_temp(t):
    # 1) 舵机角度（线性映射）
    duty = map_value(t, TEMP_MIN, TEMP_MAX, SERVO_MIN, SERVO_MAX)
    try:
        servo.duty(duty)
    except Exception:
        pass

    # 2) 灯带颜色（整条同色，亮度随温度）
    try:
        r, g, b = color_from_temp(t)
        for i in range(NUM_LEDS):
            np[i] = (r, g, b)
        np.write()
    except Exception:
        pass
BTN = Pin(POWER_BTN_PIN, Pin.IN, Pin.PULL_DOWN)
# 若你的 TTP223 连接后闲置是高电平，就把上面改成 Pin.PULL_UP 并反过来判断即可

def btn_irq(pin):
    """只做：消抖 + 记录‘按下起点’，以及在松手时重置武装。
       长按判定放到主循环里做，兼容‘翻转保持’模式。"""
    global _last_irq_ms, _press_t0, _press_armed
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_irq_ms) < DEBOUNCE_MS:
        return
    _last_irq_ms = now

    if pin.value():  # 上升沿：开始按
        if _press_armed and _press_t0 is None:
            _press_t0 = now
    else:            # 下降沿：松手/翻转为低
        _press_t0 = None
        _press_armed = True  # 允许下一次触发

BTN.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=btn_irq)
def go_sleep_soft():
    """休眠：关灯、复位舵机、关 Wi-Fi、提示"""
    global system_on
    system_on = False
    try:
        np.fill((0, 0, 0)); np.write()
    except: pass
    try:
        servo.duty(SERVO_RESET_DUTY)
    except: pass
    try:
        display_line1("Sleep"); display_line2("")
    except: pass
    try:
        wlan.active(False)
    except: pass
    print("[POWER] -> SLEEP")

def wake_up_soft():
    """唤醒：开 Wi-Fi，恢复业务"""
    global system_on
    system_on = True
    try:
        display_line1("Waking...")
    except: pass
    try:
        wlan.active(True)
    except: pass
    # 让原有的 wifi 状态机去衔接就行
    print("[POWER] -> RUN")
def power_button_tick():
    """兼容 TTP223 的‘翻转保持’和‘瞬时’两种模式。
       条件：按下后保持为高达 LONG_PRESS_MS 即判定长按。
       触发一次后等回到低电平再武装。"""
    global _press_t0, _press_armed
    if _press_t0 is None:
        return
    if BTN.value() and _press_armed:
        if time.ticks_diff(time.ticks_ms(), _press_t0) >= LONG_PRESS_MS:
            _press_armed = False  # 不重复触发，等回到低电平再武装
            _press_t0 = None
            # 执行电源切换
            if system_on:
                go_sleep_soft()
            else:
                wake_up_soft()

# ---------------- 启动与主循环 ----------------
print("Board UID:", uid_hex)
display_line1("Loading...")
display_line2("UID:"+uid_hex[:10])
time.sleep(1.5)
display_line2("")

wifi_connect()

try:
    while True:
    # 先处理电源键（无论休眠/运行都要响应）
        power_button_tick()

        if system_on:
            # —— 正常运行：温度 → 伺服/灯带 + Wi-Fi/上报 ——
            temp_tick()
            update_outputs_from_temp(last_temp)

            ensure_wifi()         # 你的非阻塞 Wi-Fi 状态机
            try_send_temp(last_temp)
        else:
            # 休眠：什么都不做，留一点点喘息时间
            time.sleep(0.05)

        # 你原来的节流
        time.sleep(0.05)


except KeyboardInterrupt:
    print("\nExiting")
    display_line1("Exiting...")

finally:
    # 清理与安全收尾
    try:
        sock.close()
        print("Socket closed")
    except:
        pass

    try:
        np.fill((0, 0, 0))
        np.write()
        print("LED cleared")
    except:
        pass

    try:
        servo.duty(SERVO_RESET_DUTY)
    except:
        pass

    try:
        display_line1("Bye Bye~")
        display_line2("")
    except:
        pass

