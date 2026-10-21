#!/usr/bin/env python3
# 作者与使用声明 / Authorship and use: see NOTICE.md.
# 代码由本仓库作者编写；实际应用风险由使用者自行评估并承担（以适用法律为限）。
# Written by the repository author; practical use is at the user's risk, subject to applicable law.
# 原始 PDF 和数据来自互联网，权利归原权利人 / Original PDF and data are third-party materials.
r"""EE271 三段数字距离保护继电器终端仿真。

运行示例：
    python distance_relay.py
    python distance_relay.py --event 1
    python distance_relay.py --event 2 --scroll
    python distance_relay.py --event all --repeat
    python distance_relay.py --no-live

说明：
1. 三段整定值在程序启动时计算一次，运行中保持不变。
2. AG/BG/CG/AB/BC/CA 六回路并行运行，各自独立计时，不按文件名预设故障。
3. 32Q 负序方向监督：有效反向量闭锁，方向死区也闭锁。
4. 只使用正方向 mho 圆，圆心 Zset/2、半径 |Zset|/2。
   保留原始相量符号；不因某事件未动作而旋转或取绝对值。
5. 运行结果同时保存到 outputs/relay/relay_EventN_results.csv。
"""

from __future__ import annotations

import argparse
import cmath
import csv
import math
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# 保护整定参数：需要调整时集中修改这里
# ---------------------------------------------------------------------------

# 本线路和相邻线路的正序阻抗（来自作业题目）
Z1_LINE = complex(2.1272, 68.7150)
Z0_LINE = complex(60.8130, 222.4585)
Z1_ADJACENT = complex(3.4728, 73.6720)

# I 段：本线路的 80%
ZONE1_REACH_RATIO = 0.80

# II 段：按笔记中的“与相邻线路 I 段配合”公式暂定
K_REL_ZONE2 = 0.85
K_BRANCH_MIN = 1.00
ADJACENT_ZONE1_RATIO = 0.80

# III 段：题目没有明确给最大负荷电流，暂按录波故障前约 1.5 kA 取值
RATED_LINE_VOLTAGE_V = 500_000.0
MAX_LOAD_CURRENT_A = 1_500.0
K_REL_ZONE3 = 1.20
K_MOTOR_START = 1.00
K_RETURN = 1.05

# 动作延时；I 段不附加延时
ZONE2_DELAY_S = 0.30
ZONE3_DELAY_S = 1.00

# 电流过小时不计算阻抗，避免除以接近零的数
MIN_LOOP_CURRENT_A = 20.0

# 32Q 负序方向监督：统一用于所有录波，不按事件名称改变方向。
NEGATIVE_CURRENT_RATIO = 0.10
NEGATIVE_VOLTAGE_RATIO = 0.02
DIRECTION_DEADBAND_OHM = 0.10 * abs(Z1_LINE)
LOOPS = ('AG', 'BG', 'CG', 'AB', 'BC', 'CA')


@dataclass
class Channel:
    name: str
    unit: str
    multiplier: float
    offset: float


@dataclass
class ComtradeConfig:
    station: str
    channels: list[Channel]
    frequency_hz: float
    sample_rate_hz: float
    expected_samples: int
    time_multiplier: float


@dataclass
class RelaySettings:
    zone1: complex
    zone2: complex
    zone3: complex
    zone2_delay_s: float
    zone3_delay_s: float
    k0: complex
    z_load_min: float
    characteristic: str = "mho"


@dataclass
class RelayState:
    zone2_timer_s: float = 0.0
    zone3_timer_s: float = 0.0
    trip_latched: bool = False
    trip_zone: str = "—"
    trip_time_s: float | None = None
    measurement_loop: str | None = None
    fault_direction: str = '未判定'


@dataclass
class StepResult:
    time_s: float
    voltage: complex
    current: complex
    impedance: complex | None
    region: str
    in_zone1: bool
    in_zone2: bool
    in_zone3: bool
    zone2_timer_s: float
    zone3_timer_s: float
    output: str
    direction: str = '负序量不足'
    z2_projection: float | None = None


@dataclass
class DirectionDecision:
    label: str
    projection: float | None
    supervised: bool
    loop: str


def analyze_phasors(phasors: list[complex], forced_loop: str | None = None) -> DirectionDecision:
    """自动选回路和 32Q 方向监督；CT 正方向假设为母线指向被保护线路。

    正向故障负序网络满足 U2=-Zsource,2 I2；反向故障通常为正号。
    只有负序电压、电流均足够时启用监督；死区内闭锁，不猜方向。
    三相对称故障负序不足时仍由原有 mho 元件判断，不能宣称 32Q 覆盖。
    """
    ia, ib, ic, ua, ub, uc = phasors
    a = cmath.exp(2j * math.pi / 3)
    i1 = (ia + a * ib + a*a * ic) / 3
    i2 = (ia + a*a * ib + a * ic) / 3
    u2 = (ua + a*a * ub + a * uc) / 3
    residual = abs(ia + ib + ic)
    if residual > max(3 * MIN_LOOP_CURRENT_A, 1.5 * abs(i2)):
        loop = ['AG', 'BG', 'CG'][max(range(3), key=lambda k: abs(phasors[k]))]
    else:
        loop = max([('AB', abs(ia-ib)), ('BC', abs(ib-ic)), ('CA', abs(ic-ia))], key=lambda p: p[1])[0]
    if forced_loop:
        loop = forced_loop
    active = (abs(i2) >= max(MIN_LOOP_CURRENT_A, NEGATIVE_CURRENT_RATIO * abs(i1))
              and abs(u2) >= NEGATIVE_VOLTAGE_RATIO * RATED_LINE_VOLTAGE_V / math.sqrt(3))
    if not active:
        return DirectionDecision('负序量不足', None, False, loop)
    q = ((u2 / i2) * cmath.exp(-1j * cmath.phase(Z1_LINE))).real
    label = '正向' if q < -DIRECTION_DEADBAND_OHM else '反向' if q > DIRECTION_DEADBAND_OHM else '方向死区'
    return DirectionDecision(label, q, True, loop)


class RelayBank:
    """六回路各自计算三段并行计时，任一允许回路首次动作即锁存总命令。"""
    def __init__(self):
        self.states = {name: RelayState() for name in LOOPS}
        self.state = RelayState()
        self.trip_loop = None

    def step(self, settings, time_s, dt_s, phasors, forced_loop=None):
        direction = analyze_phasors(phasors, forced_loop)
        results = {}
        for name in LOOPS:
            u, i = select_loop_phasors(name, phasors, settings.k0)
            decision = DirectionDecision(direction.label, direction.projection, direction.supervised, name)
            results[name] = update_relay(self.states[name], settings, time_s, dt_s, u, i, decision)
        eligible = [forced_loop] if forced_loop else LOOPS
        if not self.state.trip_latched:
            trips = [name for name in eligible if self.states[name].trip_latched]
            if trips:
                # 同一采样点并列时优先 I、II、III 段，再按公开回路顺序消歧。
                name = min(trips, key=lambda n: (self.states[n].trip_time_s,
                           ('Ⅰ段','Ⅱ段','Ⅲ段').index(self.states[n].trip_zone), LOOPS.index(n)))
                st = self.states[name]
                self.state.trip_latched = True
                self.state.trip_time_s = st.trip_time_s
                self.state.trip_zone = st.trip_zone
                self.trip_loop = name
        selected = self.trip_loop or direction.loop
        self.state.fault_direction = direction.label
        chosen = results[selected]
        return selected, chosen, results


def _read_cfg_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise UnicodeError(f"无法读取配置文件：{path}")


def _fields(line: str) -> list[str]:
    return [item.strip() for item in next(csv.reader([line]))]


def _count(token: str) -> int:
    match = re.search(r"\d+", token)
    if not match:
        raise ValueError(f"无法解析通道数量：{token!r}")
    return int(match.group())


def parse_cfg(path: Path) -> ComtradeConfig:
    lines = [line.strip() for line in _read_cfg_text(path).splitlines() if line.strip()]
    station = _fields(lines[0])[0]
    channel_counts = _fields(lines[1])
    analog_count = _count(channel_counts[1])
    digital_count = _count(channel_counts[2]) if len(channel_counts) > 2 else 0

    channels: list[Channel] = []
    cursor = 2
    for _ in range(analog_count):
        row = _fields(lines[cursor])
        cursor += 1
        channels.append(
            Channel(
                name=row[1],
                unit=row[4],
                multiplier=float(row[5]),
                offset=float(row[6]),
            )
        )

    cursor += digital_count
    frequency_hz = float(_fields(lines[cursor])[0])
    cursor += 1
    rate_count = int(float(_fields(lines[cursor])[0]))
    cursor += 1
    if rate_count != 1:
        raise NotImplementedError("本程序目前只处理单一采样率的 COMTRADE 文件。")
    rate_row = _fields(lines[cursor])
    sample_rate_hz = float(rate_row[0])
    expected_samples = int(float(rate_row[1]))
    cursor += rate_count
    cursor += 2  # 起始时间、触发时间
    data_format = _fields(lines[cursor])[0].upper()
    if data_format != "BINARY":
        raise NotImplementedError(f"目前只支持 BINARY DAT，实际为 {data_format}。")
    cursor += 1
    time_multiplier = float(_fields(lines[cursor])[0]) if cursor < len(lines) else 1.0
    return ComtradeConfig(
        station=station,
        channels=channels,
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
        expected_samples=expected_samples,
        time_multiplier=time_multiplier,
    )


def read_dat(path: Path, cfg: ComtradeConfig) -> tuple[list[float], list[list[float]]]:
    analog_count = len(cfg.channels)
    record = struct.Struct(f"<II{analog_count}h")
    raw = path.read_bytes()
    if len(raw) % record.size:
        raise ValueError(f"DAT 文件长度与通道数不匹配：{path}")

    times: list[float] = []
    values: list[list[float]] = []
    first_sample: int | None = None
    for unpacked in record.iter_unpack(raw):
        sample_number, timestamp = unpacked[:2]
        if first_sample is None:
            first_sample = sample_number
        if timestamp:
            sample_time = timestamp * cfg.time_multiplier * 1e-6
        else:
            sample_time = (sample_number - first_sample) / cfg.sample_rate_hz
        times.append(sample_time)
        values.append(
            [
                raw_value * channel.multiplier + channel.offset
                for raw_value, channel in zip(unpacked[2:], cfg.channels)
            ]
        )

    if cfg.expected_samples and len(values) != cfg.expected_samples:
        raise ValueError(
            f"CFG 声明 {cfg.expected_samples} 点，但 DAT 中读取到 {len(values)} 点。"
        )
    return times, values


def build_settings(characteristic: str = "mho") -> RelaySettings:
    if characteristic != "mho":
        raise ValueError("仅支持正方向 mho 圆。")
    zone1 = ZONE1_REACH_RATIO * Z1_LINE
    adjacent_zone1 = ADJACENT_ZONE1_RATIO * Z1_ADJACENT
    zone2 = K_REL_ZONE2 * (Z1_LINE + K_BRANCH_MIN * adjacent_zone1)

    z_load_min = (
        0.9 * RATED_LINE_VOLTAGE_V / (math.sqrt(3.0) * MAX_LOAD_CURRENT_A)
    )
    zone3_magnitude = z_load_min / (K_REL_ZONE3 * K_MOTOR_START * K_RETURN)
    line_angle = cmath.phase(Z1_LINE)
    zone3 = cmath.rect(zone3_magnitude, line_angle)
    k0 = (Z0_LINE - Z1_LINE) / (3.0 * Z1_LINE)
    return RelaySettings(
        zone1=zone1,
        zone2=zone2,
        zone3=zone3,
        zone2_delay_s=ZONE2_DELAY_S,
        zone3_delay_s=ZONE3_DELAY_S,
        k0=k0,
        z_load_min=z_load_min,
        characteristic=characteristic,
    )


def one_cycle_phasor(
    samples: list[float], times: list[float], frequency_hz: float
) -> complex:
    """用一周波离散傅里叶算法计算 RMS 基波相量。"""
    count = len(samples)
    if count == 0:
        return 0j
    total = sum(
        value * cmath.exp(-1j * 2.0 * math.pi * frequency_hz * sample_time)
        for value, sample_time in zip(samples, times)
    )
    return math.sqrt(2.0) * total / count


def zone_pickup(measured: complex | None, reach: complex, characteristic: str) -> bool:
    """根据明确选择的动作特性判断，而不修改录波或测量阻抗符号。"""
    if characteristic != "mho":
        raise ValueError(f"未知动作特性：{characteristic}")
    if measured is None or not (
        math.isfinite(measured.real) and math.isfinite(measured.imag)
    ):
        return False
    # 正方向自极化静态 mho 圆：经过原点和整定点，直径为整定向量。
    # 显式检查正向投影，避免原点或数值容差被误当作有效方向证据。
    projection = (measured * reach.conjugate()).real
    return projection > 0.0 and abs(measured - reach / 2.0) <= abs(reach) / 2.0 + 1e-9


def select_loop_phasors(
    loop_name: str, phasors: list[complex], k0: complex
) -> tuple[complex, complex]:
    ia, ib, ic, ua, ub, uc = phasors
    if loop_name == "AG":
        return ua, ia + k0 * (ia + ib + ic)
    if loop_name == "BG":
        return ub, ib + k0 * (ia + ib + ic)
    if loop_name == "CG":
        return uc, ic + k0 * (ia + ib + ic)
    if loop_name == "AB":
        return ua - ub, ia - ib
    if loop_name == "BC":
        return ub - uc, ib - ic
    if loop_name == "CA":
        return uc - ua, ic - ia
    raise ValueError(f"未知测量回路：{loop_name}")


def update_relay(
    state: RelayState,
    settings: RelaySettings,
    time_s: float,
    dt_s: float,
    voltage: complex,
    current: complex,
    direction: DirectionDecision | None = None,
) -> StepResult:
    if direction is not None and state.measurement_loop != direction.loop:
        state.zone2_timer_s = state.zone3_timer_s = 0.0
        state.measurement_loop = direction.loop
    impedance = voltage / current if abs(current) >= MIN_LOOP_CURRENT_A else None
    in_zone1 = zone_pickup(impedance, settings.zone1, settings.characteristic)
    in_zone2 = zone_pickup(impedance, settings.zone2, settings.characteristic)
    in_zone3 = zone_pickup(impedance, settings.zone3, settings.characteristic)
    blocked = direction is not None and direction.supervised and direction.label != '正向'
    if blocked:
        in_zone1 = in_zone2 = in_zone3 = False

    # 三个距离元件并行工作，因此故障落入 I 段时，II、III 段元件也会启动计时。
    state.zone2_timer_s = state.zone2_timer_s + dt_s if in_zone2 else 0.0
    state.zone3_timer_s = state.zone3_timer_s + dt_s if in_zone3 else 0.0

    if blocked:
        region = direction.label + '闭锁'
    elif in_zone1:
        region = "Ⅰ段动作区"
    elif in_zone2:
        region = "Ⅱ段动作区"
    elif in_zone3:
        region = "Ⅲ段动作区"
    else:
        region = "动作区外"

    trip_now = "—"
    if in_zone1:
        trip_now = "Ⅰ段"
    elif in_zone2 and state.zone2_timer_s >= settings.zone2_delay_s:
        trip_now = "Ⅱ段"
    elif in_zone3 and state.zone3_timer_s >= settings.zone3_delay_s:
        trip_now = "Ⅲ段"

    if trip_now != "—" and not state.trip_latched:
        state.trip_latched = True
        state.trip_zone = trip_now
        state.trip_time_s = time_s

    output = (
        f"{state.trip_zone}已跳闸（锁存）"
        if state.trip_latched
        else "未跳闸"
    )
    return StepResult(
        time_s=time_s,
        voltage=voltage,
        current=current,
        impedance=impedance,
        region=region,
        in_zone1=in_zone1,
        in_zone2=in_zone2,
        in_zone3=in_zone3,
        zone2_timer_s=state.zone2_timer_s,
        zone3_timer_s=state.zone3_timer_s,
        output=output,
        direction=direction.label if direction else '未启用监督',
        z2_projection=direction.projection if direction else None,
    )


def _complex_text(value: complex, scale: float, unit: str) -> str:
    value /= scale
    sign = "+" if value.imag >= 0 else "-"
    return f"{value.real:9.3f} {sign} j{abs(value.imag):8.3f} {unit}"


def _impedance_text(value: complex | None) -> str:
    if value is None:
        return "电流过小，暂不计算"
    return (
        f"{value.real:8.3f} {'+' if value.imag >= 0 else '-'} j{abs(value.imag):8.3f} Ω"
        f"    |Z|={abs(value):8.3f} Ω    ∠Z={math.degrees(cmath.phase(value)):7.2f}°"
    )


def render_dashboard(
    event_name: str,
    loop_name: str,
    result: StepResult,
    settings: RelaySettings,
) -> None:
    # ANSI 清屏后回到左上角；Windows Terminal、PowerShell 和 Codex 终端均支持。
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write("EE271 三段数字距离保护继电器（终端实时仿真）\n")
    sys.stdout.write("=" * 72 + "\n")
    sys.stdout.write(f"事件 / 测量回路 : {event_name} / {loop_name}\n")
    sys.stdout.write(f"测算时间         : {result.time_s:9.6f} s\n")
    sys.stdout.write(f"测量电压 U       : {_complex_text(result.voltage, 1000.0, 'kV')}\n")
    sys.stdout.write(f"测量电流 I       : {_complex_text(result.current, 1000.0, 'kA')}\n")
    sys.stdout.write(f"测量阻抗 Z       : {_impedance_text(result.impedance)}\n")
    sys.stdout.write("-" * 72 + "\n")
    sys.stdout.write(f"当前判断         : {result.region}\n")
    sys.stdout.write(f"32Q 方向         : {result.direction}；投影={result.z2_projection} Ω\n")
    sys.stdout.write(
        f"I 段元件         : {'启动（瞬时）' if result.in_zone1 else '未启动'}\n"
    )
    sys.stdout.write(
        f"II 段计时        : {result.zone2_timer_s:7.4f} / "
        f"{settings.zone2_delay_s:.4f} s  "
        f"{'计时中' if result.in_zone2 else '已复归'}\n"
    )
    sys.stdout.write(
        f"III 段计时       : {result.zone3_timer_s:7.4f} / "
        f"{settings.zone3_delay_s:.4f} s  "
        f"{'计时中' if result.in_zone3 else '已复归'}\n"
    )
    sys.stdout.write(f"跳闸输出         : {result.output}\n")
    sys.stdout.write("\n按 Ctrl+C 可停止仿真。\n")
    sys.stdout.flush()


def render_scroll(event_name: str, loop_name: str, result: StepResult) -> None:
    q_text = '—' if result.z2_projection is None else f'{result.z2_projection:+.2f}Ω'
    z_text = (
        f"{result.impedance.real:+.2f}{result.impedance.imag:+.2f}j Ω"
        if result.impedance is not None
        else "不可计算"
    )
    print(
        f"{event_name}  t={result.time_s:8.5f}s  回路={loop_name}  "
        f"U={abs(result.voltage)/1000:7.2f}∠{math.degrees(cmath.phase(result.voltage)):+6.1f}°kV  "
        f"I={abs(result.current)/1000:6.3f}∠{math.degrees(cmath.phase(result.current)):+6.1f}°kA  "
        f"Z={z_text:>22}  判断={result.region:<7}  "
        f"方向={result.direction} q2={q_text}  "
        f"tII={result.zone2_timer_s:6.3f}s  "
        f"tIII={result.zone3_timer_s:6.3f}s  输出={result.output}",
        flush=True,
    )


def save_results(path: Path, rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "测算时间(s)",
                "测量回路",
                "U实部(V)",
                "U虚部(V)",
                "I实部(A)",
                "I虚部(A)",
                "Z实部(Ω)",
                "Z虚部(Ω)",
                "阻抗幅值(Ω)",
                "当前判断",
                "II段计时(s)",
                "III段计时(s)",
                "跳闸输出",
                "32Q方向", "负序方向投影(Ω)",
            ]
        )
        writer.writerows(rows)


def simulate_event(
    cfg_path: Path,
    settings: RelaySettings,
    output_dir: Path,
    live: bool,
    scroll: bool,
    speed: float,
    display_interval_s: float,
    forced_loop: str | None,
) -> tuple[str, str, RelayState, Path]:
    cfg = parse_cfg(cfg_path)
    dat_path = cfg_path.with_suffix(".dat")
    times, values = read_dat(dat_path, cfg)
    if len(cfg.channels) < 6:
        raise ValueError("本作业程序要求前三个通道为三相电流、后三个通道为三相电压。")

    event_match = re.search(r"Event(\d+)", cfg_path.stem, re.IGNORECASE)
    event_number = event_match.group(1) if event_match else "?"
    event_name = f"Event{event_number}"
    loop_name = forced_loop or '自动'
    window_size = max(4, round(cfg.sample_rate_hz / cfg.frequency_hz))
    bank = RelayBank()
    state = bank.state
    rows: list[list[object]] = []
    all_rows: list[list[object]] = []
    last_display_time = -math.inf
    fault_loops: list[str] = []
    fault_directions: list[str] = []

    for index in range(window_size - 1, len(values)):
        start = index - window_size + 1
        window_times = times[start : index + 1]
        phasors = [
            one_cycle_phasor(
                [values[row_index][channel_index] for row_index in range(start, index + 1)],
                window_times,
                cfg.frequency_hz,
            )
            for channel_index in range(6)
        ]
        loop_name, result, loop_results = bank.step(settings, times[index], max(times[index]-times[index-1],0), phasors, forced_loop)
        direction = analyze_phasors(phasors, forced_loop)
        if direction.label in ('正向', '反向'):
            fault_loops.append(loop_name)
            fault_directions.append(direction.label)
        voltage, current = result.voltage, result.current
        for name, item in loop_results.items():
            iz = item.impedance
            all_rows.append([item.time_s,name,item.voltage.real,item.voltage.imag,item.current.real,item.current.imag,
                             None if iz is None else iz.real,None if iz is None else iz.imag,
                             None if iz is None else abs(iz),item.region,item.zone2_timer_s,item.zone3_timer_s,
                             item.output,item.direction,item.z2_projection])
        z = result.impedance
        rows.append(
            [
                f"{result.time_s:.9f}",
                loop_name,
                f"{voltage.real:.6f}",
                f"{voltage.imag:.6f}",
                f"{current.real:.6f}",
                f"{current.imag:.6f}",
                "" if z is None else f"{z.real:.6f}",
                "" if z is None else f"{z.imag:.6f}",
                "" if z is None else f"{abs(z):.6f}",
                result.region,
                f"{result.zone2_timer_s:.6f}",
                f"{result.zone3_timer_s:.6f}",
                result.output,
                result.direction,
                '' if result.z2_projection is None else f'{result.z2_projection:.6f}',
            ]
        )

        should_display = (
            index == window_size - 1
            or index == len(values) - 1
            or result.time_s - last_display_time >= display_interval_s
        )
        if live and should_display:
            if scroll:
                render_scroll(event_name, loop_name, result)
            else:
                render_dashboard(event_name, loop_name, result, settings)
            if last_display_time > -math.inf and speed > 0:
                time.sleep(max(0.0, (result.time_s - last_display_time) / speed))
            last_display_time = result.time_s

    result_path = output_dir / f"relay_{event_name}_results.csv"
    save_results(result_path, rows)
    save_results(output_dir / f'relay_{event_name}_all_loops.csv', all_rows)
    if fault_loops:
        loop_name = bank.trip_loop or max(set(fault_loops), key=fault_loops.count)
        state.fault_direction = max(set(fault_directions), key=fault_directions.count)
    return event_name, loop_name, state, result_path


def print_settings(settings: RelaySettings) -> None:
    def line(name: str, value: complex) -> None:
        print(
            f"{name:<10}: {value.real:.4f} {value.imag:+.4f}j Ω  "
            f"|Z|={abs(value):.3f} Ω  ∠={math.degrees(cmath.phase(value)):.2f}°"
        )

    print("本次仿真采用的固定整定值")
    print("动作特性：正方向 mho 圆 |Z-Zset/2| ≤ |Zset|/2，且 Re(Z·conj(Zset)) > 0")
    print(f"32Q 方向监督：死区 ±{DIRECTION_DEADBAND_OHM:.3f} Ω；反向闭锁，不改动原始 R、X。")
    print("=" * 72)
    line("I 段", settings.zone1)
    line("II 段", settings.zone2)
    line("III 段", settings.zone3)
    print(
        f"零序补偿 K0: {settings.k0.real:.4f} {settings.k0.imag:+.4f}j    "
        f"II 延时={settings.zone2_delay_s:.3f}s    "
        f"III 延时={settings.zone3_delay_s:.3f}s"
    )
    print(
        f"III 段暂按最大负荷电流 {MAX_LOAD_CURRENT_A/1000:.3f} kA 计算，"
        f"Zload,min={settings.z_load_min:.3f} Ω。"
    )
    print("=" * 72)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="EE271 三段数字距离保护继电器：终端显示 U、I、Z、段别和计时。"
    )
    parser.add_argument(
        "--event",
        choices=("1", "2", "all"),
        default="all",
        help="选择 Event1、Event2 或全部，默认 all",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=script_dir / "inputs" / "comtrade",
        help="COMTRADE CFG/DAT 文件所在目录",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=script_dir / "outputs" / "relay",
        help="计算结果 CSV 输出目录",
    )
    parser.add_argument(
        "--loop",
        choices=("AG", "BG", "CG", "AB", "BC", "CA"),
        help="手动指定测量回路；默认根据三相相量自动选择，方向判断仍启用",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="终端回放速度倍数；2 表示二倍速，0 表示不等待",
    )
    parser.add_argument(
        "--display-interval",
        type=float,
        default=0.02,
        help="终端刷新所对应的录波时间间隔，默认 0.02 s",
    )
    parser.add_argument("--scroll", action="store_true", default=True, help="逐行追加显示（默认），保留历史记录")
    parser.add_argument("--single-window", action="store_true", help="在当前终端依次处理两个事件，便于日志检查")
    parser.add_argument("--keep-open", action="store_true", help="结束后等待回车，保留窗口")
    parser.add_argument(
        "--characteristic", choices=("mho",), default="mho",
        help="动作特性：mho 正方向阻抗圆（唯一支持的特性）",
    )
    parser.add_argument("--no-live", action="store_true", help="不实时显示，只计算并输出汇总")
    parser.add_argument("--repeat", action="store_true", help="循环回放，按 Ctrl+C 停止")
    args = parser.parse_args()

    if args.speed < 0:
        parser.error("--speed 不能小于 0")
    if args.display_interval <= 0:
        parser.error("--display-interval 必须大于 0")

    input_dir = args.input_dir.resolve()
    requested = ("1", "2") if args.event == "all" else (args.event,)
    cfg_paths = [
        input_dir / f"EE271_DistanceProtection_Event{number}.cfg"
        for number in requested
    ]
    for cfg_path in cfg_paths:
        if not cfg_path.is_file() or not cfg_path.with_suffix(".dat").is_file():
            raise FileNotFoundError(f"找不到配套的 CFG/DAT：{cfg_path}")

    # 同一个程序分别运行两个事件，每个事件拥有独立的终端和历史输出。
    if args.event == "all" and not args.no_live and not args.single_window:
        if os.name != "nt":
            parser.error("独立终端窗口功能适用于 Windows；其他系统请加 --single-window")
        for number in requested:
            command = [
                sys.executable, "-u", str(Path(__file__).resolve()),
                "--event", number, "--scroll", "--keep-open",
                "--input-dir", str(input_dir),
                "--output-dir", str(args.output_dir.resolve()),
                "--speed", str(args.speed),
                "--display-interval", str(args.display_interval),
                "--characteristic", args.characteristic,
            ]
            if args.loop:
                command.extend(["--loop", args.loop])
            if args.repeat:
                command.append("--repeat")
            subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
        print("已打开 Event1、Event2 两个独立终端窗口；各窗口逐行保留结果。")
        return

    if os.name == "nt" and not args.no_live:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(f"event{args.event}")

    settings = build_settings(args.characteristic)
    print_settings(settings)
    summaries: list[tuple[str, str, RelayState, Path]] = []
    try:
        while True:
            if not args.no_live and not args.scroll:
                print_settings(settings)
                time.sleep(1.0)
            for cfg_path in cfg_paths:
                if not args.no_live:
                    print(f"\n开始处理：{cfg_path.stem}（U、I 为测量回路基波 RMS 相量）", flush=True)
                summaries.append(
                    simulate_event(
                        cfg_path=cfg_path,
                        settings=settings,
                        output_dir=args.output_dir.resolve(),
                        live=not args.no_live,
                        scroll=args.scroll,
                        speed=args.speed,
                        display_interval_s=args.display_interval,
                        forced_loop=args.loop,
                    )
                )
            if not args.repeat:
                break
            summaries.clear()
    except KeyboardInterrupt:
        print("\n已由用户停止仿真。")
        return

    if not args.scroll and not args.no_live:
        sys.stdout.write("\033[2J\033[H")
    print_settings(settings)
    print("仿真结果")
    print("-" * 72)
    for event_name, loop_name, state, result_path in summaries:
        if state.trip_latched and state.trip_time_s is not None:
            result = f"{state.trip_zone}动作，跳闸时刻 {state.trip_time_s:.6f} s"
        else:
            result = f"{state.fault_direction}闭锁，未动作" if state.fault_direction == '反向' else "未动作"
        print(f"{event_name} / {loop_name}: {result}")
        print(f"  逐点结果：{result_path}")
    if args.keep_open:
        input("\n本事件计算结束，历史记录已保留。按回车关闭此窗口……")


if __name__ == "__main__":
    main()
