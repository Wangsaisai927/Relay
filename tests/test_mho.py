"""Run with: python -m unittest discover -s tests -v"""
import unittest
import cmath, math
from pathlib import Path
import distance_relay as relay
import simulate_with_relay as sim

class MhoTests(unittest.TestCase):
    def test_sequence_direction(self):
        a=cmath.exp(2j*math.pi/3)
        for q,label in [(-30,'正向'),(30,'反向'),(0,'方向死区')]:
            # 独立构造正、负序网络，不使用两个算例的动作时刻。
            v1,i1=288675+0j,1400+0j;i2=500+0j
            v2=i2*cmath.rect(30 if q==0 else abs(q),cmath.phase(relay.Z1_LINE)+(math.pi/2 if q==0 else 0 if q>0 else math.pi))
            f=[i1+i2,a*a*i1+a*i2,a*i1+a*a*i2,v1+v2,a*a*v1+a*v2,a*v1+a*a*v2]
            d=relay.analyze_phasors(f)
            self.assertTrue(d.supervised);self.assertEqual(d.label,label)
            s=relay.build_settings();st=relay.RelayState()
            relay.update_relay(st,s,0,.01,s.zone1*50,100+0j,d)
            self.assertEqual(st.trip_latched,label=='正向')

    def test_auto_loop_and_direction_from_data(self):
        root=Path(__file__).resolve().parents[1]
        for name,loop,direction in [('Event1','AG','正向'),('Event2','BC','反向')]:
            p=root/'inputs/comtrade'/f'EE271_DistanceProtection_{name}.cfg'
            c=relay.parse_cfg(p);ts,vs=relay.read_dat(p.with_suffix('.dat'),c)
            k=min(range(len(ts)),key=lambda j:abs(ts[j]-1.1))
            ph=[relay.one_cycle_phasor([v[h] for v in vs[k-79:k+1]],ts[k-79:k+1],60) for h in range(6)]
            d=relay.analyze_phasors(ph)
            self.assertEqual((d.loop,d.label),(loop,direction))
            # 各相电流同时反极性，32Q 方向也必须反转，而不是由名称固定。
            inv=relay.analyze_phasors([-x for x in ph[:3]]+ph[3:])
            self.assertNotEqual(inv.label,d.label)

    def test_geometry_and_reverse_rejection(self):
        z=relay.build_settings().zone1
        for point,expected in [(z/2,True),(z,True),(-z/2,False),(z*1.01,False),(0j,False),(None,False),(complex(float('nan'),0),False)]:
            self.assertEqual(relay.zone_pickup(point,z,'mho'),expected)
        with self.assertRaises(ValueError):relay.build_settings('impedance')

    def test_parallel_delay_and_reset(self):
        s=relay.build_settings()
        for ratio,expected,delay in [(1.2,'Ⅱ段',.3),(2.2,'Ⅲ段',1.)]:
            st=relay.RelayState()
            for k in range(1100):relay.update_relay(st,s,k*.001,.001,s.zone1*ratio*100,100+0j)
            self.assertEqual(st.trip_zone,expected)
            self.assertAlmostEqual(st.trip_time_s,delay,delta=.002)
        st=relay.RelayState()
        relay.update_relay(st,s,1,.1,s.zone1*1.2*100,100+0j)
        relay.update_relay(st,s,1.1,.1,-s.zone1*100,100+0j)
        self.assertEqual(st.zone2_timer_s,0)
        self.assertEqual(st.zone3_timer_s,0)
        relay.update_relay(st,s,1.2,.1,1000+0j,0j)
        self.assertFalse(st.trip_latched)

    def test_records_and_breaker(self):
        root=Path(__file__).resolve().parents[1]
        for event in ['Event1','Event2']:
            p=sim.simulate(root/'inputs/comtrade'/f'EE271_DistanceProtection_{event}.cfg',.05)
            self.assertEqual(p['characteristic'],'mho')
            self.assertEqual(len(p['rows']),7200)
            self.assertEqual(set(p['all_loops']),set(relay.LOOPS))
            for name, history in p['all_loops'].items():
                self.assertEqual(len(history),7200)
                for row in history:
                    z=None if row[1] is None else complex(row[1],row[2])
                    for k,reach in enumerate([relay.build_settings().zone1,relay.build_settings().zone2,relay.build_settings().zone3]):
                        expected=relay.zone_pickup(z,reach,'mho') and row[8] not in ('反向','方向死区')
                        self.assertEqual(row[3+k],expected,(event,name,row[0]))
            if event=='Event1':
                self.assertEqual(p['trip_loop'],'AG')
                self.assertAlmostEqual(p['trip_time'],1.008952,places=6)
                self.assertAlmostEqual(p['open_time'],1.058952,places=6)
                self.assertTrue(all(r[3:6]==[0.,0.,0.] for r in p['rows'] if r[2]>=p['open_time']))
            else:
                self.assertIsNone(p['trip_time'])
                self.assertIsNone(p['open_time'])
                self.assertTrue(all(r[3:9]==a[3:9] for r,a in zip(p['rows'],p['raw_rows'])))
                self.assertTrue(all(r[13]==r[14]==0 for r in p['rows']))
                self.assertEqual(p['direction'],'反向')
                self.assertEqual(p['loop'],'BC')
                self.assertTrue(any(r[12]=='反向闭锁' for r in p['rows']))

if __name__=='__main__':unittest.main()
