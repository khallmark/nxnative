"""Native observation errors, epoch ownership and frame-based UI contracts."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.replay import ReplayBridge,validate_world_xz
from evolution.live import TrialStopped


class ReplayTests(unittest.TestCase):
    def bridge(self,root):
        b=ReplayBridge.__new__(ReplayBridge);b.root=Path(root);b.native_status={};b.episode_epoch=None;b.fps=60;b.world_xz=None
        return b

    def binding(self):
        return dict(rotation_address='0x1000',guard1_address='0x2000',guard1_value='0x3000',
                    guard2_address='0x4000',guard2_value='0x5000')

    def test_world_binding_rejects_malformed_and_wrapping_addresses(self):
        self.assertIsNone(validate_world_xz(None))
        self.assertEqual(validate_world_xz(self.binding()),self.binding())
        for value in ({},[],True,{**self.binding(),'extra':'0x1'}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_world_xz(value)
        for name in self.binding():
            for value in (True,123,'123','0x_1','0x-1','0x10000000000000000'):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    validate_world_xz({**self.binding(),name:value})
        for name in ('rotation_address','guard1_address','guard2_address'):
            for value in ('0x0','0x1001','0xfffffffffffffffc'):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    validate_world_xz({**self.binding(),name:value})
        binding={**self.binding(),'guard1_value':'0x0','guard2_value':'0xFFFFFFFFFFFFFFFF'}
        normalized=validate_world_xz(binding);assert normalized is not None
        self.assertEqual(normalized['guard2_value'],'0xffffffffffffffff')

    def test_world_episode_requires_exact_native_mode_and_binding_ack(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.world_xz=self.binding();calls=[]
            valid=dict(epoch=7,current_frame=100,left_stick_space='world_xz',world_xz=self.binding(),last_camera_frame=0)
            def rpc(verb,before=(),after=()):
                calls.append((verb,Path(before[0]).read_text(),after));return valid
            b.rpc=rpc
            b.start_replay('f0:L1/0~f30\n',0x6000)
            self.assertEqual(b.episode_epoch,7)
            self.assertEqual(calls,[('replay','f0:L1/0~f30\n',('health=6000:0','world_xz=1000:2000:3000:4000:5000'))])
            self.assertEqual(list(Path(td).iterdir()),[])
            for bad in ({},dict(left_stick_space='camera'),dict(world_xz=None),
                        dict(world_xz={**self.binding(),'guard1_value':'0x9999'}),
                        dict(last_camera_frame=True),dict(last_camera_frame=-1),dict(last_camera_frame=101)):
                b.episode_epoch=None
                reply={**valid,**bad} if bad else dict(epoch=7,current_frame=100)
                b.rpc=lambda *args,r=reply,**kwargs:r
                with self.subTest(bad=bad),self.assertRaises(TrialStopped):b.start_replay('f0:L1/0~f30\n')
                self.assertTrue(b.owns_replay);self.assertIsNone(b.episode_epoch)
                self.assertEqual(list(Path(td).iterdir()),[])

    def test_world_session_ui_remains_camera_relative_and_old_hosts_are_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.world_xz=self.binding();calls=[]
            def rpc(verb,before=(),after=()):
                calls.append(after);return dict(epoch=7,current_frame=100)
            b.rpc=rpc
            b.start_replay('f0:LUp+A~f9\n',episode=False)
            self.assertEqual(calls,[()]);self.assertIsNone(b.episode_epoch)
            b.world_xz=None;b.start_replay('f0:L1/0~f30\n')
            self.assertEqual(calls,[(),()]);self.assertEqual(b.episode_epoch,7)
            b.episode_epoch=None
            b.rpc=lambda *args,**kwargs:dict(epoch=8,current_frame=100,left_stick_space='world_xz',world_xz=self.binding())
            with self.assertRaises(TrialStopped):b.start_replay('f0:L1/0~f30\n')

    def test_split_reads_reassemble_and_leave_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);counts=[]
            def rpc(verb,before=(),after=()):
                request=Path(before[0]).read_text().splitlines();rows=[]
                for line in request:
                    addr,size=line.split();a=int(addr,16);n=int(size);counts.append(n)
                    rows.append(dict(address=f'0x{a:x}',size=n,hex=bytes((a+i)%256 for i in range(n)).hex(),error=None))
                return dict(reads=rows)
            b.rpc=rpc
            self.assertEqual(b.read_memory({0x1000:196608})[0x1000],bytes(i%256 for i in range(196608)))
            self.assertEqual(counts,[4096]*48);self.assertEqual(list(Path(td).iterdir()),[])

    def test_read_errors_are_not_fabricated_zeroes(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td)
            for row in [dict(address='0x1000',size=4,hex=None,error='read_error'),dict(address='0x1000',size=4,hex='00',error=None),dict(address='0x1000',size=4,hex='zzzzzzzz',error=None)]:
                b.rpc=lambda *args,r=row,**kwargs:dict(reads=[r])
                with self.assertRaises(TrialStopped):b.read_memory({0x1000:4})

    def test_menu_hold_uses_guest_frames_without_a_death_guard(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);starts=[]
            def start(timeline,health_address=None,threshold=0,episode=True,target_feedback=False):
                starts.append((timeline,dict(episode=episode)))
            b.start_replay=start
            b.wait_replay=lambda timeout=10:dict(reason='completed')
            b.read_memory({},('A~0.1500',))
            self.assertEqual(starts,[('f0:A~f9\n',dict(episode=False))])
            b.episode_epoch=4
            with self.assertRaises(TrialStopped):b.read_memory({},('A~0.1500',))

    def test_uncertain_dispatched_start_still_cancels_on_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.owns_replay=False
            def send(*lines):Path(lines[0].split()[-1]).write_text('{broken-ack')
            b.send=send
            with self.assertRaises(ValueError):b.start_replay('f0:A~f10\n')
            self.assertTrue(b.owns_replay)
            with patch.object(b,'cancel_replay') as cancel,patch('evolution.live.Bridge.close'):
                b.close();cancel.assert_called_once()

    def test_cleanup_does_not_cancel_rejected_unowned_playback(self):
        b=ReplayBridge.__new__(ReplayBridge);b.owns_replay=False
        with patch.object(b,'cancel_replay') as cancel,patch('evolution.live.Bridge.close'):
            b.close();cancel.assert_not_called()
            b.owns_replay=True;b.close();cancel.assert_called_once()

    def test_scoped_deadline_expires_before_dispatch_and_restores_default(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.owns_replay=False;sent=[];b.send=lambda *lines:sent.extend(lines)
            with b.request_deadline(.1):
                deadline=b.rpc_deadline;assert deadline is not None
                with patch('evolution.replay.time.monotonic',return_value=deadline+1):
                    with self.assertRaisesRegex(TrialStopped,'deadline'):b.rpc('replay')
            self.assertEqual(sent,[]);self.assertFalse(b.owns_replay);self.assertIsNone(b.rpc_deadline)
            self.assertEqual(list(Path(td).iterdir()),[])
            for timeout in [True,0,-1,11,float('nan')]:
                with self.subTest(timeout=timeout),self.assertRaises(ValueError),b.request_deadline(timeout):pass

    def test_lost_ack_uses_operation_budget_and_cleanup_gets_new_budget(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.send=lambda *lines:None
            with patch('evolution.replay.time.monotonic',side_effect=[0.,0.,0.,.2]):
                with b.request_deadline(.1),self.assertRaisesRegex(TrialStopped,'ACK timed out'):b.rpc('replay')
            self.assertTrue(b.owns_replay);self.assertIsNone(b.rpc_deadline)
            self.assertEqual(list(Path(td).iterdir()),[])
            def send(*lines):
                Path(lines[0].split()[-1]).write_text(json.dumps(dict(protocol=1,ok=True,epoch=1,
                    current_frame=10,start_frame=10,end_frame=20,active=False)))
            b.send=send
            with b.request_deadline(2.):self.assertFalse(b.cancel_replay()['active'])
            self.assertFalse(b.owns_replay);self.assertIsNone(b.rpc_deadline)

    def test_rpc_rejects_other_epoch_and_accepts_atomic_ack(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(td);b.episode_epoch=4
            def send(*lines):
                Path(lines[0].split()[-1]).write_text(json.dumps(dict(protocol=1,ok=True,epoch=5,current_frame=10,start_frame=2,end_frame=12,active=True)))
            b.send=send
            with self.assertRaises(TrialStopped):b.rpc('replay_status')
            self.assertEqual(list(Path(td).iterdir()),[])


if __name__=='__main__':unittest.main()
