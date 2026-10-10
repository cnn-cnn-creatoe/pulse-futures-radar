import unittest,tempfile
from pathlib import Path
from monitor.actionability import assess_inputs,refresh_saved
from monitor.instance_lock import InstanceLock

class ScoreTests(unittest.TestCase):
    def test_frozen_liquidity_interval_only_decides_unambiguous_gate(self):
        values={'status':'ready','atr':1,'closes':[100]*4,'as_of':100,'trend':'up'}
        plan={'risk_reward':3,'stop_loss':99,'direction':'long'}
        for bounds,expected in [([.79,.9],'通过'),([.1,.3],'不通过'),([.3,.5],'待验证'),([.9,.1],'待验证')]:
            result=assess_inputs({'close_time':100,'spread_bps':2,'liquidity_bounds':bounds},values,plan,None)
            self.assertEqual(result['execution_status'],expected)

    def test_missing_book_keeps_structure_and_bad_spread_caps_score(self):
        signal={'close_time':100}
        values={'status':'ready','atr':1,'closes':[100,100,100,100],'as_of':100,'trend':'up'}
        plan={'risk_reward':3,'stop_loss':99,'direction':'long'}
        score=assess_inputs(signal,values,plan,None)
        self.assertEqual(score['score'],100)
        self.assertEqual(score['execution_status'],'待验证')
        self.assertFalse(score['probability'])
        self.assertEqual(assess_inputs({**signal,'spread_bps':30},values,plan,None)['score'],59)
        self.assertIsNone(assess_inputs(signal,{**values,'as_of':101},plan,None)['score'])

    def test_different_process_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            first=InstanceLock(Path(folder)/'lock');second=InstanceLock(Path(folder)/'lock')
            first.acquire()
            try:
                with self.assertRaises(RuntimeError): second.acquire()
            finally: first.close()
            second.acquire();second.close()

    def test_no_plan_is_waiting_structure_not_fake_success(self):
        score=assess_inputs({'close_time':1},{'status':'ready','atr':1,'closes':[1]*4,'as_of':1},None,None)
        self.assertEqual(score['score'],0)
        self.assertEqual(score['label'],'等待结构')
