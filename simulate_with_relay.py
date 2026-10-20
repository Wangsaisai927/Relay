#!/usr/bin/env python3
# 作者与使用声明 / Authorship and use: see NOTICE.md.
# 代码由本仓库作者编写；实际应用风险由使用者自行评估并承担（以适用法律为限）。
# Written by the repository author; practical use is at the user's risk, subject to applicable law.
# 原始 PDF 和数据来自互联网，权利归原权利人 / Original PDF and data are third-party materials.
"""回放真实录波，联动继电器与理想断路器，导出 XLSX、PNG 和无文字阻抗 GIF。

python simulate_with_relay.py
python simulate_with_relay.py --breaker-delay 0.08
python simulate_with_relay.py --gif-only

注意：这是录波驱动的简化闭环演示，不是完整电力网络电磁暂态仿真。
断开后，线路 CT 电流为零，电源侧 PT 电压取故障前基波外推值。
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import distance_relay as relay
from data import resolve_font_path


def simulate(cfg_path: Path, delay: float, characteristic: str = 'impedance') -> dict:
    if not math.isfinite(delay) or delay < 0:
        raise ValueError('断路器延迟必须是非负有限数。')
    if characteristic != 'impedance':
        raise ValueError('此版本只采用普通阻抗圆。')
    cfg = relay.parse_cfg(cfg_path)
    times, original = relay.read_dat(cfg_path.with_suffix('.dat'), cfg)
    if len(cfg.channels) != 6 or any(c.unit.strip().upper() != 'A' for c in cfg.channels[:3]):
        raise ValueError('需要本作业的 IA、IB、IC、UA、UB、UC 六通道录波。')
    event = 'Event1' if 'Event1' in cfg_path.stem else 'Event2'
    loop = 'AG' if event == 'Event1' else 'BC'
    settings = relay.build_settings(characteristic)
    n = round(cfg.sample_rate_hz / cfg.frequency_hz)
    # 采用故障前最初一个周波估计各相电源侧电压；相位与时间原点一致。
    source_voltage = [
        relay.one_cycle_phasor([r[c] for r in original[:n]], times[:n], cfg.frequency_hz)
        for c in range(3, 6)
    ]
    state = relay.RelayState()
    modeled: list[list[float]] = []
    rows = []
    open_time = None
    scheduled_open_time = None
    for k, (t, raw) in enumerate(zip(times, original)):
        if state.trip_time_s is not None and scheduled_open_time is None:
            scheduled_open_time = state.trip_time_s + delay
        opened = scheduled_open_time is not None and t >= scheduled_open_time
        if opened:
            if open_time is None:
                open_time = t
            rotation = cmath.exp(1j * 2 * math.pi * cfg.frequency_hz * t)
            values = [0.0, 0.0, 0.0] + [
                math.sqrt(2) * (u * rotation).real for u in source_voltage
            ]
        else:
            values = list(raw)
        modeled.append(values)
        result = None
        if k >= n - 1:
            first = k - n + 1
            phasors = [
                relay.one_cycle_phasor([r[c] for r in modeled[first:k+1]], times[first:k+1], cfg.frequency_hz)
                for c in range(6)
            ]
            u, i = relay.select_loop_phasors(loop, phasors, settings.k0)
            result = relay.update_relay(state, settings, t, times[k] - times[k-1], u, i)
            # 零延时断路器也只在收到命令后才改变样本，不提前修改测量。
        if opened:
            stage = '断路器已断开'
        elif state.trip_latched:
            stage = '已发跳闸命令，等待断开'
        else:
            stage = '断路器闭合'
        z = result.impedance if result else None
        rows.append([
            k+1, round(t*1e6), t, *values,
            int(state.trip_latched), int(not opened), stage,
            result.region if result else '一周波窗口未满',
            result.zone2_timer_s if result else 0.0,
            result.zone3_timer_s if result else 0.0,
            z.real if z is not None else None,
            z.imag if z is not None else None,
        ])
    # 回归检查：断开前数据保持原样，断开后电流为零，不出现提前切除。
    for t, old, new in zip(times, original, modeled):
        if open_time is None or t < open_time:
            assert old == new
        else:
            assert new[:3] == [0.0, 0.0, 0.0]
    if state.trip_time_s is not None:
        assert state.trip_time_s >= 1.0, '出现故障前误动作，需要检查整定或算法。'
    if open_time is not None:
        assert open_time >= state.trip_time_s + delay - 1e-12
        assert open_time - (state.trip_time_s + delay) < 1.1 / cfg.sample_rate_hz
    return {
        'event': event, 'loop': loop, 'frequency': cfg.frequency_hz,
        'sample_rate': cfg.sample_rate_hz,
        'characteristic': characteristic, 'breaker_delay': delay,
        'trip_time': state.trip_time_s, 'trip_zone': state.trip_zone,
        'open_time': open_time, 'rows': rows,
        'raw_rows': [[k+1, round(t*1e6), t, *r] for k, (t, r) in enumerate(zip(times, original))],
        'headers': ['采样序号','时间戳(μs)','时间(s)','I_A(A)','I_B(A)','I_C(A)',
                    'U_AN(V)','U_BN(V)','U_CN(V)','跳闸命令(1为发出)','断路器闭合(1为闭合)',
                    '设备状态','当前动作区','II段计时(s)','III段计时(s)','Z实部(Ω)','Z虚部(Ω)'],
        'zone_reaches': [abs(settings.zone1), abs(settings.zone2), abs(settings.zone3)],
    }


def plot_event(payload: dict, output: Path) -> None:
    """保留 data 文件夹中上下两幅三相波形的形式，添加两个事件标记。"""
    image = Image.new('RGB', (1600, 1060), 'white')
    draw = ImageDraw.Draw(image)
    font_path = resolve_font_path()
    font = ImageFont.truetype(str(font_path), 19)
    small = ImageFont.truetype(str(font_path), 15)
    title = ImageFont.truetype(str(font_path), 25)
    event = payload['event']
    draw.text((70, 18), f'{event} — 继电器投入后三相波形（简化断路器仿真）', fill='#172B4D', font=title)
    trip = payload['trip_time']
    opened = payload['open_time']
    trip_text = '未发出跳闸命令' if trip is None else f"{payload['trip_zone']}跳闸命令：{trip:.6f} s"
    open_text = '未断开' if opened is None else f'断路器断开：{opened:.6f} s'
    draw.text((70, 57), f"{trip_text}    {open_text}    断路器延迟={payload['breaker_delay']*1000:.0f} ms", fill='#333333', font=font)
    draw.text((70, 89), '模型：断开后线路电流为零；电源侧电压按故障前基波外推；不模拟电弧、重合闸和网络暂态。', fill='#555555', font=small)
    rows = payload['rows']
    start_time, end_time = rows[0][2], rows[-1][2]
    left, right = 140, 1540
    colors = ['#d62728', '#2ca02c', '#1f77b4']
    for top, bottom, columns, label, names in [
        (190,490,[3,4,5],'电流 I (A)',['I_A','I_B','I_C']),
        (650,950,[6,7,8],'电压 U (V)',['U_AN','U_BN','U_CN']),
    ]:
        maximum = max(abs(row[c]) for row in rows for c in columns) * 1.08 or 1
        def xy(t, v):
            return (left+(t-start_time)/(end_time-start_time)*(right-left), (top+bottom)/2-v/maximum*(bottom-top)/2)
        draw.rectangle((left,top,right,bottom), outline='#444444')
        draw.text((left,top-35), f'{event} — 三相' + ('电流波形' if columns[0]==3 else '电源侧对地电压波形'), fill='#222222',font=font)
        # 中文竖向轴标题
        label_img = Image.new('RGBA',(170,34),(255,255,255,0))
        ImageDraw.Draw(label_img).text((2,2),label,fill='#222222',font=font)
        label_img = label_img.rotate(90,expand=True)
        image.paste(label_img,(12,(top+bottom-label_img.height)//2),label_img)
        for j in range(7):
            t=start_time+(end_time-start_time)*j/6
            x=xy(t,0)[0]
            draw.line((x,top,x,bottom),fill='#dddddd')
            draw.text((x-19,bottom+8),f'{t:.2f}',font=small,fill='#333333')
        for j in range(5):
            val=-maximum+2*maximum*j/4
            y=xy(start_time,val)[1]
            draw.line((left,y,right,y),fill='#dddddd')
            text=f'{val:.0f}'
            draw.text((left-12-draw.textlength(text,font=small),y-9),text,fill='#333333',font=small)
        for c,color,name in zip(columns,colors,names):
            draw.line([xy(row[2],row[c]) for row in rows],fill=color,width=1)
            pos=right-290+(c-columns[0])*100
            draw.line((pos,top-21,pos+20,top-21),fill=color,width=3)
            draw.text((pos+25,top-32),name,fill='#333333',font=small)
        for t,color in [(trip,'#8e44ad'),(opened,'#d97706')]:
            if t is not None:
                x=xy(t,0)[0]
                for y in range(top,bottom,12):
                    draw.line((x,y,x,min(y+6,bottom)),fill=color,width=2)
        draw.text((750,bottom+38),'时间 (s)',font=font,fill='#333333')
    draw.text((140,1020),'紫色虚线：跳闸命令    橙色虚线：断路器断开    电压测点：电源侧；电流测点：线路支路',fill='#555555',font=small)
    image.save(output)


def export_excel(payload: dict, output: Path) -> None:
    """Public Python dependency only; export values, not spreadsheet formulas."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    main = wb.active
    main.title = '继电器投入后波形'
    raw = wb.create_sheet('原始录波')
    main.merge_cells('A1:Q1')
    main['A1'] = f"{payload['event']} 继电器与理想断路器仿真"
    main['A1'].font = Font(name='宋体', size=16, bold=True)
    main.row_dimensions[1].height = 30
    metadata = [
        ['项目', '本次计算结果'],
        ['测量回路', payload['loop']],
        ['动作特性', '普通阻抗圆（无方向性）'],
        ['跳闸段别', payload['trip_zone']],
        ['跳闸命令时间(s)', payload['trip_time']],
        ['断路器延迟(s)', payload['breaker_delay']],
        ['实际断开时间(s)', payload['open_time']],
    ]
    for row_index, values in enumerate(metadata, 4):
        for col, value in enumerate(values, 1):
            main.cell(row_index, col, value)
    notes = [
        '仿真假设', '断开前沿用原录波，断开后线路三相电流为零。',
        '电压测点为电源侧，断开后按故障前基波外推。',
        '不模拟电弧、重合闸、过电压和完整电网暂态。',
        '动作不证明故障位于正方向。',
        f"采样率 {payload['sample_rate']:.3f} Hz，基波 {payload['frequency']:.1f} Hz",
        'I/II/III 半径(Ω)：' + '/'.join(f'{v:.3f}' for v in payload['zone_reaches']),
    ]
    for row_index, note in enumerate(notes, 4):
        main.cell(row_index, 4, note)
    for row_index, values in enumerate([payload['headers'], *payload['rows']], 12):
        for col, value in enumerate(values, 1):
            main.cell(row_index, col, value)
    raw['A1'] = f"原始录波来源：EE271_DistanceProtection_{payload['event']}.cfg / .dat"
    raw.merge_cells('A1:I1')
    for row_index, values in enumerate([payload['headers'][:9], *payload['raw_rows']], 3):
        for col, value in enumerate(values, 1):
            raw.cell(row_index, col, value)
    head_fill = PatternFill('solid', fgColor='243E60')
    stripe_fill = PatternFill('solid', fgColor='F2F5F8')
    thin = Side(style='thin', color='D9D9D9')
    for ws, header_row, columns in [(main, 12, 17), (raw, 3, 9)]:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = f'A{header_row + 1}'
        ws.auto_filter.ref = f'A{header_row}:{get_column_letter(columns)}{ws.max_row}'
        for col in range(1, columns + 1):
            ws.column_dimensions[get_column_letter(col)].width = 20
        for row in ws.iter_rows():
            for cell in row:
                cell.font = Font(name='宋体', size=10)
                cell.alignment = Alignment(vertical='center')
                if cell.row >= header_row:
                    if cell.row == header_row:
                        cell.fill = head_fill
                        cell.font = Font(name='宋体', size=10, bold=True, color='FFFFFF')
                        cell.alignment = Alignment(vertical='center', wrap_text=True)
                        cell.border = Border(bottom=thin)
                    else:
                        if cell.row % 2 == 0:
                            cell.fill = stripe_fill
                        if isinstance(cell.value, float):
                            cell.number_format = '0.000000'
            ws.row_dimensions[row[0].row].height = 20
        ws.row_dimensions[header_row].height = 32
    main['A1'].font = Font(name='宋体', size=16, bold=True)
    main.column_dimensions['A'].width = 26
    main.column_dimensions['B'].width = 29
    main.column_dimensions['L'].width = 32
    for row_index in range(8, 11):
        main.cell(row_index, 2).number_format = '0.000000'
    wb.save(output)


def plot_impedance_gif(payload: dict, output: Path, start: float = .98,
                       end: float = 1.05, frame_count: int = 120) -> None:
    """Two equal-scale R-X panels: overview and zoom; deliberately no text."""
    points = [(r[2], complex(r[15], r[16])) for r in payload['rows']
              if r[15] is not None and start <= r[2] <= end
              and math.isfinite(r[15]) and math.isfinite(r[16])]
    if len(points) < 2:
        raise ValueError('GIF 时间窗内没有足够的有效阻抗点，请调整时间范围。')
    bg = Image.new('RGB', (1100, 600), 'white')
    event1 = payload['event'] == 'Event1'
    limits = [((-180, 250), (-180, 180)), ((-35, 160), (-100, 100))] if event1 else [
        ((-180, 530), (-490, 220)), ((-40, 155), (-165, 40))]
    panels = []
    colors = ['#2878b5', '#20a06b', '#343434']
    for position, ((xmin, xmax), (ymin, ymax)) in enumerate(limits):
        box = (35 + 550 * position, 35, 515 + 550 * position, 555)
        scale = min((box[2]-box[0])/(xmax-xmin), (box[3]-box[1])/(ymax-ymin))
        width = round((xmax-xmin)*scale)
        height = round((ymax-ymin)*scale)
        origin_x = box[0] + ((box[2]-box[0])-width)//2
        origin_y = box[1] + ((box[3]-box[1])-height)//2
        layer = Image.new('RGB', (width+1, height+1), 'white')
        def xy(z, xmin=xmin, ymax=ymax, scale=scale):
            return ((z.real-xmin)*scale, (ymax-z.imag)*scale)
        draw = ImageDraw.Draw(layer)
        step = 50 if position == 0 else 25
        for val in range(math.ceil(xmin/step)*step, math.floor(xmax/step)*step+1, step):
            x = xy(complex(val, 0))[0]
            draw.line((x, 0, x, height), fill='#e6e9ed')
        for val in range(math.ceil(ymin/step)*step, math.floor(ymax/step)*step+1, step):
            y = xy(complex(0, val))[1]
            draw.line((0, y, width, y), fill='#e6e9ed')
        x, y = xy(0j)
        draw.line((x, 0, x, height), fill='#8c96a0', width=2)
        draw.line((0, y, width, y), fill='#8c96a0', width=2)
        for radius, color in reversed(list(zip(payload['zone_reaches'], colors))):
            a = xy(complex(-radius, radius)); b = xy(complex(radius, -radius))
            draw.ellipse((*a, *b), outline=color, width=3)
        draw.rectangle((0, 0, width, height), outline='#aab2ba')
        panels.append((layer, (origin_x, origin_y), xy))
    frames = []
    for frame_index in range(frame_count):
        k = round(frame_index*(len(points)-1)/(frame_count-1))
        frame = bg.copy()
        for base, origin, xy in panels:
            layer = base.copy(); draw = ImageDraw.Draw(layer)
            trail = [xy(z) for _, z in points[:k+1]]
            if len(trail) > 1:
                draw.line(trail, fill='#d43535', width=3)
            x, y = trail[-1]
            draw.ellipse((x-7, y-7, x+7, y+7), fill='#d43535', outline='white', width=2)
            frame.paste(layer, origin)
        frames.append(frame)
    durations = [50]*len(frames)
    durations[0] = 600; durations[-1] = 1600
    frames[0].save(output, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True, disposal=2)


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir',type=Path,default=here/'inputs'/'comtrade')
    parser.add_argument('--output-dir',type=Path,default=here/'outputs'/'simulation')
    parser.add_argument('--breaker-delay',type=float,default=0.05,help='断路器动作延迟(s)，默认0.05')
    parser.add_argument('--event',choices=['1','2','all'],default='all')
    parser.add_argument('--gif-only',action='store_true',help='只生成无文字阻抗轨迹 GIF')
    parser.add_argument('--gif-start',type=float,default=.98)
    parser.add_argument('--gif-end',type=float,default=1.05)
    parser.add_argument('--gif-frames',type=int,default=120)
    args=parser.parse_args()
    if not math.isfinite(args.breaker_delay) or args.breaker_delay<0:
        parser.error('断路器延迟必须是非负有限数。')
    if not all(math.isfinite(v) for v in [args.gif_start,args.gif_end]) or args.gif_start>=args.gif_end:
        parser.error('GIF 起止时刻必须是递增的有限数。')
    if not 2 <= args.gif_frames <= 600:
        parser.error('GIF 帧数须在 2～600 之间。')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    summaries=[]
    events=['Event1','Event2'] if args.event=='all' else [f'Event{args.event}']
    for event in events:
        item=simulate(args.input_dir/f'EE271_DistanceProtection_{event}.cfg',args.breaker_delay)
        if not args.gif_only:
            plot_event(item,args.output_dir/f'{event}_with_relay_waveforms.png')
            export_excel(item,args.output_dir/f'{event}_with_relay_data.xlsx')
        plot_impedance_gif(item,args.output_dir/f'{event}_RX_trajectory.gif',args.gif_start,args.gif_end,args.gif_frames)
        summaries.append({key:item[key] for key in ['event','loop','trip_time','trip_zone','open_time','breaker_delay','zone_reaches']})
        print(f"{event}: 跳闸={item['trip_time']} s，断开={item['open_time']} s",flush=True)
    (args.output_dir/'summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'已生成仿真结果：{args.output_dir.resolve()}')


if __name__=='__main__':
    main()
