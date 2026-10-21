"""Build the site from the actual relay calculation; no hand-drawn sample signals."""
from pathlib import Path
import sys
import json

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
import simulate_with_relay as sim
import distance_relay as relay

ROOT=Path(__file__).resolve().parent
events={}
for name in ['Event1','Event2']:
    result=sim.simulate(SOURCE/'inputs/comtrade'/f'EE271_DistanceProtection_{name}.cfg',.05)
    rows=result['rows'];raw=result['raw_rows']
    reaches=result['zone_reaches']
    entries=[]
    for zone,reach in zip(['I','II','III'],reaches):
        vector=complex(*result['zone_vectors'][['I','II','III'].index(zone)])
        first=next((r for r in rows if r[15] is not None and relay.zone_pickup(complex(r[15],r[16]),vector,'mho')),None)
        entries.append({'zone':zone,'time':first[2] if first else None})
    e={
        'name':name,'loop':result['loop'],'frequency':result['frequency'],
        'sampleRate':result['sample_rate'],'faultTime':1.0,'rawFaultEnd':1.2,
        'tripTime':result['trip_time'],'openTime':result['open_time'],
        'delay':result['breaker_delay'],'reaches':reaches,'entries':entries,
        'vectors':result['zone_vectors'],'centers':result['zone_centers'],
        'radii':result['zone_radii'],'characteristic':'mho',
        'direction':result['direction'],
        'loopSeries':[r[17] for r in rows],
        'directionSeries':[r[18] for r in rows],
        'q2':[None if r[19] is None else round(r[19],6) for r in rows],
        'directionDeadband':result['direction_deadband'],
        'tripLoop':result['trip_loop'],
        'loopData':{name:{'z':[[None if r[c] is None else round(r[c],6) for r in history] for c in [1,2]],
                          'timers':[[round(r[c],7) for r in history] for c in [6,7]],
                          'pickup':[[int(r[c]) for r in history] for c in [3,4,5]]}
                    for name,history in result['all_loops'].items()},
        't':[r[2] for r in rows],
        'raw':[[round(r[c]/1000,6) for r in raw] for c in range(3,9)],
        'after':[[round(r[c]/1000,6) for r in rows] for c in range(3,9)],
        'z':[[None if r[c] is None else round(r[c],6) for r in rows] for c in [15,16]],
        'timers':[[round(r[c],7) for r in rows] for c in [13,14]],
    }
    assert len(e['t'])==7200
    assert all(len(a)==7200 for a in e['raw']+e['after']+e['z']+e['timers'])
    events[name]=e
    print(name,e['tripTime'],e['openTime'])
template=(ROOT/'page.html').read_text(encoding='utf-8')
assert '__RELAY_PAYLOAD__' in template
result=template.replace('__RELAY_PAYLOAD__',json.dumps(events,ensure_ascii=False,separators=(',',':'),allow_nan=False))
(ROOT/'dist').mkdir(exist_ok=True)
(ROOT/'dist/index.html').write_text(result,encoding='utf-8')
print('Built HTML:',len(result.encode('utf-8')),'bytes')
