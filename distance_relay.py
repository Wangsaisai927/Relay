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
2. Event1 使用带零序补偿的 AG 测量回路。
3. Event2 使用 BC 相间测量回路。
4. 使用普通阻抗圆，与课程报告采用的动作特性一致。
   普通阻抗圆自身没有方向选择性，不能据此确认故障的物理方向。
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
    characteristic: str = "impedance"


@dataclass
class RelayState:
    zone2_timer_s: float = 0.0
    zone3_timer_s: float = 0.0
    trip_latched: bool = False
    trip_zone: str = "—"
    trip_time_s: float | None = None


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


def build_settings(characteristic: str = "impedance") -> RelaySettings:
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
    if characteristic != "impedance":
        raise ValueError(f"未知动作特性：{characteristic}")
    if measured is None or not (
        math.isfinite(measured.real) and math.isfinite(measured.imag)
    ):
        return False
    # 普通阻抗继电器：以原点为圆心，整定阻抗幅值为半径。
    # 这种特性对反向小阻抗也会启动；工程应用须另加方向元件。
    return abs(measured) <= abs(reach)


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
) -> StepResult:
    impedance = voltage / current if abs(current) >= MIN_LOOP_CURRENT_A else None
    in_zone1 = zone_pickup(impedance, settings.zone1, settings.characteristic)
    in_zone2 = zone_pickup(impedance, settings.zone2, settings.characteristic)
    in_zone3 = zone_pickup(impedance, settings.zone3, settings.characteristic)

    # 三个距离元件并行工作，因此故障落入 I 段时，II、III 段元件也会启动计时。
    state.zone2_timer_s = state.zone2_timer_s + dt_s if in_zone2 else 0.0
    state.zone3_timer_s = state.zone3_timer_s + dt_s if in_zone3 else 0.0

    if in_zone1:
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
    loop_name = forced_loop or ("AG" if event_number == "1" else "BC")
    window_size = max(4, round(cfg.sample_rate_hz / cfg.frequency_hz))
    state = RelayState()
    rows: list[list[object]] = []
    last_display_time = -math.inf

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
        voltage, current = select_loop_phasors(loop_name, phasors, settings.k0)
        dt_s = times[index] - times[index - 1] if index else 1.0 / cfg.sample_rate_hz
        result = update_relay(
            state=state,
            settings=settings,
            time_s=times[index],
            dt_s=max(dt_s, 0.0),
            voltage=voltage,
            current=current,
        )
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
    return event_name, loop_name, state, result_path


def print_settings(settings: RelaySettings) -> None:
    def line(name: str, value: complex) -> None:
        print(
            f"{name:<10}: {value.real:.4f} {value.imag:+.4f}j Ω  "
            f"|Z|={abs(value):.3f} Ω  ∠={math.degrees(cmath.phase(value)):.2f}°"
        )

    print("本次仿真采用的固定整定值")
    if settings.characteristic == "impedance":
        print("动作特性：普通阻抗圆 |Z| ≤ |Zset|（不带方向判别）")
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
        help="手动指定测量回路；默认 Event1=AG、Event2=BC",
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
        "--characteristic", choices=("impedance",), default="impedance",
        help="动作特性：impedance 普通阻抗圆（无方向性）",
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
            result = "未动作"
        print(f"{event_name} / {loop_name}: {result}")
        print(f"  逐点结果：{result_path}")
    if args.keep_open:
        input("\n本事件计算结束，历史记录已保留。按回车关闭此窗口……")


if __name__ == "__main__":
    main()
