# ds18x20.py  — DS18B20/DS18S20 driver for MicroPython
# 用法:
#   from onewire import OneWire
#   import ds18x20, machine
#   ow = OneWire(machine.Pin(21))
#   ds = ds18x20.DS18X20(ow)
#   roms = ds.scan()
#   ds.convert_temp(); time.sleep_ms(750)
#   print(ds.read_temp(roms[0]))

try:
    from micropython import const
except ImportError:
    def const(x): return x

_CONVERT     = const(0x44)
_RD_SCRATCH  = const(0xBE)
_WR_SCRATCH  = const(0x4E)
_SKIP_ROM    = const(0xCC)

_FAM_DS18S20 = const(0x10)
_FAM_DS1822  = const(0x22)
_FAM_DS18B20 = const(0x28)

class DS18X20:
    def __init__(self, onewire):
        self.ow = onewire
        self.buf = bytearray(9)

    def scan(self):
        # 仅保留温度传感器家族码
        return [rom for rom in self.ow.scan()
                if rom and rom[0] in (_FAM_DS18S20, _FAM_DS1822, _FAM_DS18B20)]

    def convert_temp(self):
        # 对总线上全部探头触发温度转换（skip rom）
        self.ow.reset(True)
        self.ow.writebyte(_SKIP_ROM)
        self.ow.writebyte(_CONVERT)

    def read_scratch(self, rom):
        self.ow.reset(True)
        self.ow.select_rom(rom)
        self.ow.writebyte(_RD_SCRATCH)
        self.ow.readinto(self.buf)
        # CRC 校验不为 0 视为错误
        if self.ow.crc8(self.buf):
            raise ValueError("DS18X20 scratchpad CRC error")
        return self.buf

    def write_scratch(self, rom, th_tls):
        # 可选：写报警阈值和配置字节（3 字节）
        if len(th_tls) != 3:
            raise ValueError("need 3 bytes (TH, TL, config)")
        self.ow.reset(True)
        self.ow.select_rom(rom)
        self.ow.writebyte(_WR_SCRATCH)
        self.ow.write(th_tls)

    def read_temp(self, rom):
        buf = self.read_scratch(rom)
        fam = rom[0]
        if fam == _FAM_DS18S20:
            # DS18S20: 9-bit，特殊补偿
            if buf[1]:  # 负数
                t = (buf[0] >> 1) | 0x80
                t = -((~t + 1) & 0xFF)
            else:
                t = buf[0] >> 1
            return t - 0.25 + (buf[7] - buf[6]) / buf[7]
        else:
            # DS18B20/DS1822: 16-bit，LSB 在 buf[0]
            t = (buf[1] << 8) | buf[0]
            if t & 0x8000:  # 负数
                t = -((t ^ 0xFFFF) + 1)
            return t / 16.0

