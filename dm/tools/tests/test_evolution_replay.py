"""Native observation errors, epoch ownership and frame-based UI contracts."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.replay import ReplayBridge
from evolution.live import TrialStopped


class ReplayTests(unittest.TestCase):
    def bridge(self,root):
        b=ReplayBridge.__new__(ReplayBridge);b.root=Path(root);b.native_status={};b.episode_epoch=None;b.fps=60
        return b

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
            def start(timeline,health_address=None,threshold=0,episode=True):
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
