#!/usr/bin/env python3
# 作者与使用声明 / Authorship and use: see NOTICE.md.
# 代码由本仓库作者编写；实际应用风险由使用者自行评估并承担（以适用法律为限）。
# Written by the repository author; practical use is at the user's risk, subject to applicable law.
# 原始 PDF 和数据来自互联网，权利归原权利人 / Original PDF and data are third-party materials.
r"""Read the EE271 COMTRADE events and export readable CSV tables and PNG plots.

Default layout:
    inputs/comtrade/*.cfg / *.dat
    outputs/raw/  (generated results)

Run:
    python data.py

Optional:
    python data.py --input-dir "..." --output-dir "..."
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    raise SystemExit("PNG output requires Pillow. Install it with: python -m pip install pillow") from exc


def resolve_font_path() -> Path:
    """Find an installed Chinese font; never bundle proprietary system fonts."""
    override = os.environ.get('RELAY_FONT_PATH')
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f'RELAY_FONT_PATH 指定的字体不存在：{candidate}')
        return candidate
    candidates = [
        'C:/Windows/Fonts/simsun.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        '/System/Library/Fonts/PingFang.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    ]
    for name in candidates:
        candidate = Path(name)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('未找到中文字体。请安装中文字体，并用 RELAY_FONT_PATH 指定字体文件。')


@dataclass
class AnalogChannel:
    index: int
    name: str
    phase: str
    unit: str
    multiplier: float
    offset: float


@dataclass
class ComtradeConfig:
    station_name: str
    analog_channels: list[AnalogChannel]
    digital_count: int
    nominal_frequency_hz: float
    sample_rate_hz: float
    expected_samples: int
    data_format: str
    time_multiplier: float


def read_text_with_fallback(path: Path) -> str:
    """Read a small text file while tolerating common COMTRADE encodings."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Cannot decode configuration file: {path}")


def split_csv_line(line: str) -> list[str]:
    return [item.strip() for item in next(csv.reader([line]))]


def number_from_token(token: str, default: float = 0.0) -> float:
    try:
        return float(token.strip())
    except (ValueError, AttributeError):
        return default


def count_from_token(token: str) -> int:
    match = re.search(r"\d+", token)
    if not match:
        raise ValueError(f"Cannot parse channel count from {token!r}")
    return int(match.group())


def parse_cfg(path: Path) -> ComtradeConfig:
    lines = [line.strip() for line in read_text_with_fallback(path).splitlines() if line.strip()]
    if len(lines) < 8:
        raise ValueError(f"CFG file is too short: {path}")

    station_fields = split_csv_line(lines[0])
    station_name = station_fields[0] or path.stem

    channel_fields = split_csv_line(lines[1])
    if len(channel_fields) < 2:
        raise ValueError(f"Missing channel definition in {path}")
    analog_count = count_from_token(channel_fields[1])
    digital_count = count_from_token(channel_fields[2]) if len(channel_fields) > 2 else 0

    analog_channels: list[AnalogChannel] = []
    cursor = 2
    for _ in range(analog_count):
        fields = split_csv_line(lines[cursor])
        cursor += 1
        if len(fields) < 7:
            raise ValueError(f"Invalid analog channel line in {path}: {fields}")
        analog_channels.append(
            AnalogChannel(
                index=int(fields[0]),
                name=fields[1] or f"Analog_{fields[0]}",
                phase=fields[2] if len(fields) > 2 else "",
                unit=fields[4] if len(fields) > 4 and fields[4] else "value",
                multiplier=number_from_token(fields[5], 1.0),
                offset=number_from_token(fields[6], 0.0),
            )
        )

    cursor += digital_count
    nominal_frequency_hz = number_from_token(split_csv_line(lines[cursor])[0])
    cursor += 1

    rate_count = int(number_from_token(split_csv_line(lines[cursor])[0], 1.0))
    cursor += 1
    rates: list[tuple[float, int]] = []
    for _ in range(rate_count):
        fields = split_csv_line(lines[cursor])
        cursor += 1
        rates.append((number_from_token(fields[0]), int(number_from_token(fields[1]))))
    if not rates:
        raise ValueError(f"No sampling rate found in {path}")
    if len(rates) != 1:
        raise NotImplementedError("This assignment reader currently expects one sampling rate")
    sample_rate_hz, expected_samples = rates[0]

    # Start time and trigger time occupy the next two rows.
    cursor += 2
    data_format = split_csv_line(lines[cursor])[0].upper()
    cursor += 1
    time_multiplier = number_from_token(split_csv_line(lines[cursor])[0], 1.0) if cursor < len(lines) else 1.0

    return ComtradeConfig(
        station_name=station_name,
        analog_channels=analog_channels,
        digital_count=digital_count,
        nominal_frequency_hz=nominal_frequency_hz,
        sample_rate_hz=sample_rate_hz,
        expected_samples=expected_samples,
        data_format=data_format,
        time_multiplier=time_multiplier,
    )


def read_binary_dat(path: Path, cfg: ComtradeConfig) -> tuple[list[int], list[int], list[list[float]]]:
    if cfg.data_format != "BINARY":
        raise NotImplementedError(f"Only BINARY DAT is supported, got {cfg.data_format}")

    analog_count = len(cfg.analog_channels)
    digital_words = math.ceil(cfg.digital_count / 16)
    record_struct = struct.Struct(f"<II{analog_count}h{digital_words}H")
    raw = path.read_bytes()
    if len(raw) % record_struct.size:
        raise ValueError(
            f"DAT size {len(raw)} is not a multiple of record size {record_struct.size}: {path}"
        )

    record_count = len(raw) // record_struct.size
    if cfg.expected_samples and record_count != cfg.expected_samples:
        raise ValueError(
            f"CFG expects {cfg.expected_samples} records but DAT contains {record_count}: {path}"
        )

    sample_numbers: list[int] = []
    timestamps: list[int] = []
    values: list[list[float]] = []

    for record in record_struct.iter_unpack(raw):
        sample_numbers.append(record[0])
        timestamps.append(record[1])
        raw_analogs = record[2 : 2 + analog_count]
        values.append(
            [
                raw_value * channel.multiplier + channel.offset
                for raw_value, channel in zip(raw_analogs, cfg.analog_channels)
            ]
        )
    return sample_numbers, timestamps, values


def build_time_axis(
    sample_numbers: list[int], timestamps: list[int], cfg: ComtradeConfig
) -> list[float]:
    if timestamps and any(value != timestamps[0] for value in timestamps[1:]):
        return [value * cfg.time_multiplier * 1e-6 for value in timestamps]
    first_sample = sample_numbers[0] if sample_numbers else 1
    return [(sample - first_sample) / cfg.sample_rate_hz for sample in sample_numbers]


def friendly_column_names(channels: Iterable[AnalogChannel]) -> list[str]:
    channels = list(channels)
    current_index = 0
    voltage_index = 0
    phase_order = ("A", "B", "C")
    names: list[str] = []
    for channel in channels:
        unit = channel.unit.strip() or "value"
        phase = channel.phase.strip().upper()
        if unit.upper() == "A":
            if phase not in phase_order:
                phase = phase_order[current_index] if current_index < 3 else str(current_index + 1)
            names.append(f"I_{phase}(A)")
            current_index += 1
        elif unit.upper() == "V":
            if phase not in phase_order:
                phase = phase_order[voltage_index] if voltage_index < 3 else str(voltage_index + 1)
            names.append(f"U_{phase}N(V)")
            voltage_index += 1
        else:
            names.append(f"CH_{channel.index}_{unit}")
    return names


def write_event_csv(
    path: Path,
    sample_numbers: list[int],
    timestamps: list[int],
    time_seconds: list[float],
    values: list[list[float]],
    column_names: list[str],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["采样序号", "时间戳(μs)", "时间(s)", *column_names])
        for sample, timestamp, time_s, row in zip(sample_numbers, timestamps, time_seconds, values):
            writer.writerow([sample, timestamp, f"{time_s:.9f}", *(f"{value:.6f}" for value in row)])


def rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values)) if values else float("nan")


def append_summary_rows(
    summary_rows: list[list[object]],
    event_name: str,
    cfg: ComtradeConfig,
    values: list[list[float]],
    column_names: list[str],
    duration_s: float,
) -> None:
    for index, (channel, column_name) in enumerate(zip(cfg.analog_channels, column_names)):
        series = [row[index] for row in values]
        summary_rows.append(
            [
                event_name,
                cfg.station_name,
                f"{cfg.nominal_frequency_hz:.6f}",
                f"{cfg.sample_rate_hz:.9f}",
                len(values),
                f"{duration_s:.9f}",
                column_name,
                channel.name,
                channel.unit,
                f"{min(series):.6f}",
                f"{max(series):.6f}",
                f"{rms(series):.6f}",
            ]
        )


def plot_waveforms(
    path: Path,
    event_name: str,
    time_seconds: list[float],
    values: list[list[float]],
    cfg: ComtradeConfig,
    column_names: list[str],
) -> None:
    current_indices = [i for i, channel in enumerate(cfg.analog_channels) if channel.unit.upper() == "A"]
    voltage_indices = [i for i, channel in enumerate(cfg.analog_channels) if channel.unit.upper() == "V"]

    width, height = 1600, 980
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = resolve_font_path()
    title_font = ImageFont.truetype(str(font_path), 24)
    panel_font = ImageFont.truetype(str(font_path), 19)
    label_font = ImageFont.truetype(str(font_path), 16)
    tick_font = ImageFont.truetype(str(font_path), 13)
    colors = ("#d62728", "#2ca02c", "#1f77b4")
    event_number = re.search(r"\d+", event_name)
    event_label = f"事件{event_number.group()}" if event_number else event_name
    draw.text(
        (80, 25),
        f"EE271 COMTRADE 波形图 - {event_label}    采样率={cfg.sample_rate_hz:.3f} Hz    额定频率={cfg.nominal_frequency_hz:.1f} Hz",
        fill="#111111",
        font=title_font,
    )

    time_min = min(time_seconds)
    time_max = max(time_seconds)

    def draw_panel(indices: list[int], top: int, bottom: int, title: str, y_label: str) -> None:
        left, right = 135, width - 45
        series_list = [[row[index] for row in values] for index in indices]
        value_min = min(min(series) for series in series_list)
        value_max = max(max(series) for series in series_list)
        padding = 0.05 * (value_max - value_min) if value_max != value_min else 1.0
        value_min -= padding
        value_max += padding

        draw.rectangle((left, top, right, bottom), outline="#333333", width=1)
        draw.text((left, top - 30), title, fill="#111111", font=panel_font)
        label_bbox = label_font.getbbox(y_label)
        label_width = label_bbox[2] - label_bbox[0] + 12
        label_height = label_bbox[3] - label_bbox[1] + 12
        label_image = Image.new("RGBA", (label_width, label_height), (255, 255, 255, 0))
        label_draw = ImageDraw.Draw(label_image)
        label_draw.text((6, 2), y_label, fill="#111111", font=label_font)
        rotated_label = label_image.rotate(90, expand=True)
        image.paste(rotated_label, (12, (top + bottom - rotated_label.height) // 2), rotated_label)

        for grid_index in range(7):
            fraction = grid_index / 6
            x = int(left + fraction * (right - left))
            draw.line((x, top, x, bottom), fill="#dddddd", width=1)
            time_value = time_min + fraction * (time_max - time_min)
            draw.text((x - 18, bottom + 8), f"{time_value:.2f}", fill="#333333", font=tick_font)

        for grid_index in range(5):
            fraction = grid_index / 4
            y = int(bottom - fraction * (bottom - top))
            draw.line((left, y, right, y), fill="#dddddd", width=1)
            value = value_min + fraction * (value_max - value_min)
            tick_text = f"{value:.0f}"
            tick_bbox = draw.textbbox((0, 0), tick_text, font=tick_font)
            tick_width = tick_bbox[2] - tick_bbox[0]
            draw.text((left - tick_width - 10, y - 8), tick_text, fill="#333333", font=tick_font)

        for color, index, series in zip(colors, indices, series_list):
            points = []
            for time_value, value in zip(time_seconds, series):
                x = left + (time_value - time_min) / (time_max - time_min) * (right - left)
                y = bottom - (value - value_min) / (value_max - value_min) * (bottom - top)
                points.append((int(x), int(y)))
            draw.line(points, fill=color, width=1)

        legend_x = right - 290
        legend_y = top + 12
        for color, index in zip(colors, indices):
            draw.line((legend_x, legend_y + 5, legend_x + 22, legend_y + 5), fill=color, width=3)
            draw.text((legend_x + 28, legend_y - 4), column_names[index], fill="#222222", font=label_font)
            legend_x += 105

    draw_panel(current_indices, 110, 440, f"{event_label} - 三相电流波形", "电流 I (A)")
    draw_panel(voltage_indices, 560, 890, f"{event_label} - 三相对地电压波形", "电压 U (V)")
    draw.text((width // 2 - 35, 935), "时间 (s)", fill="#111111", font=label_font)
    image.save(path, format="PNG")


def process_all(input_dir: Path, output_dir: Path) -> None:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg_paths = sorted(input_dir.glob("*.cfg"))
    if not cfg_paths:
        raise FileNotFoundError(f"No CFG files found in: {input_dir}")

    summary_header = [
        "事件",
        "站点",
        "额定频率(Hz)",
        "采样率(Hz)",
        "采样点数",
        "记录时长(s)",
        "表格列名",
        "原始通道名称",
        "单位",
        "最小值",
        "最大值",
        "全记录有效值(RMS)",
    ]
    summary_rows: list[list[object]] = []

    for cfg_path in cfg_paths:
        dat_path = cfg_path.with_suffix(".dat")
        if not dat_path.is_file():
            raise FileNotFoundError(f"Matching DAT file not found: {dat_path}")

        cfg = parse_cfg(cfg_path)
        sample_numbers, timestamps, values = read_binary_dat(dat_path, cfg)
        time_seconds = build_time_axis(sample_numbers, timestamps, cfg)
        column_names = friendly_column_names(cfg.analog_channels)

        event_match = re.search(r"Event\d+", cfg_path.stem, flags=re.IGNORECASE)
        event_name = event_match.group().title() if event_match else cfg_path.stem
        event_number = re.search(r"\d+", event_name)
        event_label = f"事件{event_number.group()}" if event_number else event_name
        event_csv = output_dir / f"{event_name}_readable_data.csv"
        event_png = output_dir / f"{event_name}_waveforms.png"

        write_event_csv(event_csv, sample_numbers, timestamps, time_seconds, values, column_names)
        plot_waveforms(event_png, event_name, time_seconds, values, cfg, column_names)
        duration_s = time_seconds[-1] - time_seconds[0] if len(time_seconds) > 1 else 0.0
        append_summary_rows(summary_rows, event_label, cfg, values, column_names, duration_s)

        print(f"已处理{event_label}：{len(values)}个采样点")
        print(f"  表格：{event_csv}")
        print(f"  图片：{event_png}")

    summary_path = output_dir / "processing_summary.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(summary_header)
        writer.writerows(summary_rows)
    print(f"汇总表：{summary_path}")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Convert EE271 binary COMTRADE recordings to CSV and PNG.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=script_dir / "inputs" / "comtrade",
        help="Directory containing matching CFG and DAT files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=script_dir / "outputs" / "raw",
        help="Directory for generated CSV and PNG files",
    )
    args = parser.parse_args()
    process_all(args.input_dir.resolve(), args.output_dir.resolve())


if __name__ == "__main__":
    main()
