"""Relocation, handle reuse and invalid-pointer tests for runtime observations."""
from pathlib import Path
import struct
import sys
import unittest
import tempfile
from unittest.mock import Mock,patch
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.actors import ActorReader,ActorUnavailable


class ActorReaderTests(unittest.TestCase):
    def setUp(self):
        self.base=0x10000;self.memory=bytearray(0x80000);self.calls=0
        self.profile: dict[str,Any]=dict(pool=dict(address=0x11000,count=4,stride=96,flags=[0,8],mask=65535,equals=257,pointer=16,serial=24),
            object=dict(size=64,self=8,name=16,position=24,vtable=0x123456),ranges=[[0x10000,0x90000]],
            targets={'hazard':{'name':'Rock','required':True}})
        self.actor(0,0x22000,1,'Rock',(1,2,3))
        self.reader=ActorReader(self.profile,self.read_batch)

    def put(self,address,fmt,*values):
        struct.pack_into('<'+fmt,self.memory,address-self.base,*values)

    def actor(self,slot,address,serial,name,position):
        handle=0x11000+slot*96;nameptr=0x52000+slot*256
        self.put(handle,'4Q',257,257,address,serial)
        self.put(address,'3Q3f',0x123456,address,nameptr,*position)
        encoded=name.encode()+b'\0';self.memory[nameptr-self.base:nameptr-self.base+128]=encoded.ljust(128,b'\0')

    def read_batch(self,ranges):
        self.calls+=1
        for address,size in ranges.items():self.assertTrue(0x10000<=address and address+size<=0x90000)
        return {a:bytes(self.memory[a-self.base:a-self.base+n]) for a,n in ranges.items()}

    def test_discovers_exact_named_actor_and_reads_current_position(self):
        first=self.reader.observe()['hazard'];self.assertEqual(first['position'],(1,2,3));self.assertEqual(first['serial'],1)
        self.put(0x22000+24,'3f',4,5,6)
        self.assertEqual(self.reader.observe()['hazard']['position'],(4,5,6))

    def test_prefetched_observation_avoids_an_extra_read_batch(self):
        self.reader.observe();self.put(0x22000+24,'3f',4,5,6)
        data=self.read_batch(self.reader.current_reads());calls=self.calls
        self.assertEqual(self.reader.observe(data)['hazard']['position'],(4,5,6))
        self.assertEqual(self.calls,calls)

    def test_prefetched_freed_actor_still_triggers_rediscovery(self):
        self.reader.observe();self.put(0x11000+16,'2Q',0,0xffffffff)
        self.actor(1,0x32000,20,'Rock',(7,8,9))
        data=self.read_batch(self.reader.current_reads())
        self.assertEqual(self.reader.observe(data)['hazard']['address'],0x32000)

    def test_relocates_after_old_handle_and_object_are_freed(self):
        self.reader.observe();self.put(0x11000+16,'2Q',0,0xffffffff);self.put(0x22000,'Q',0xffffffffffffffff)
        self.actor(1,0x32000,20,'Rock',(7,8,9));new=self.reader.observe()['hazard']
        self.assertEqual(new['address'],0x32000);self.assertEqual(new['serial'],20);self.assertEqual(new['position'],(7,8,9))

    def test_reused_address_is_not_the_old_actor(self):
        self.reader.observe();self.actor(0,0x22000,2,'Tree',(40,50,60));self.actor(1,0x32000,3,'Rock',(7,8,9))
        self.assertEqual(self.reader.observe()['hazard']['address'],0x32000)

    def test_changed_serial_forces_revalidation(self):
        self.reader.observe();self.actor(0,0x22000,8,'Rock',(7,8,9))
        self.assertEqual(self.reader.observe()['hazard']['serial'],8)

    def test_duplicate_target_names_are_rejected(self):
        self.actor(1,0x32000,2,'Rock',(7,8,9))
        with self.assertRaises(ActorUnavailable):self.reader.observe()

    def test_invalid_self_pointer_position_and_name_pointer_are_rejected(self):
        for address,fmt,value in [(0x22008,'Q',0),(0x22018,'f',float('nan')),(0x22010,'Q',0xffffffffffffffff)]:
            with self.subTest(address=address):
                self.actor(0,0x22000,1,'Rock',(1,2,3));self.put(address,fmt,value);self.reader.invalidate()
                with self.assertRaises(ActorUnavailable):self.reader.observe()

    def test_missing_required_actor_stops_but_optional_actor_is_absent(self):
        self.actor(0,0x22000,1,'Tree',(1,2,3))
        with self.assertRaises(ActorUnavailable):self.reader.observe()
        self.profile['targets']['hazard']['required']=False
        self.assertEqual(self.reader.observe(),{})

    def test_invalid_layout_is_rejected_before_reads(self):
        self.profile['object']['position']=60
        with self.assertRaises(ValueError):ActorReader(self.profile,self.read_batch)
        self.assertEqual(self.calls,0)


class ActorLifecycleTests(unittest.TestCase):
    def test_player_ui_and_cached_actor_are_one_input_read_request(self):
        from evolution.live import Bridge
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);control=root/'control';control.touch()
            formats={'position':'3f','camera_rotation':'9f','health':'2I','hud':'I','pause':'I'}
            values={'position':(0,0,0),'camera_rotation':(1,0,0,0,1,0,0,0,1),'health':(12,12),'hud':(1,),'pause':(0,)}
            config: dict[str,Any]=dict(pid=0,control=str(control),scratch=str(root/'reads'),identity={},
                reads={name:dict(address=hex(i*128),format=fmt) for i,(name,fmt) in enumerate(formats.items())},
                ui={'gameplay':[dict(read='hud',equals=1)],'pause':[dict(read='pause',equals=1)]})
            data={int(spec['address'],0):struct.pack('<'+spec['format'],*values[name]) for name,spec in config['reads'].items()}
            data[1024]=b'actor123'
            bridge=Bridge(config);actor=Mock(spec=ActorReader);bridge.actors=actor
            actor.current_reads.return_value={1024:8};actor.observe.return_value={'hazard':{'position':(4,1,2)}}
            try:
                with patch.object(bridge,'read_memory',return_value=data) as read:
                    state=bridge.sample('L0/1',.1)
                    read.assert_called_once_with({a:len(v) for a,v in data.items()},('L0/1~0.1000',))
                    actor.observe.assert_called_once_with(data);self.assertEqual(state['hazard'],(4,1,2))
            finally:bridge.close()

    def test_bad_actor_cannot_prevent_observed_pause(self):
        from evolution.live import Bridge,TrialStopped,ui_matches
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);control=root/'control';control.touch()
            config: dict[str,Any]=dict(pid=0,control=str(control),scratch=str(root/'reads'),identity={},
                reads={name:dict(address=hex(index*128),format=fmt) for index,(name,fmt) in enumerate([
                    ('position','3f'),('camera_rotation','9f'),('health','2I'),('hud','I'),('pause','I')])},
                ui={'gameplay':[dict(read='hud',equals=1)],'pause':[dict(read='pause',equals=1)]})
            values={'position':(0,0,0),'camera_rotation':(1,0,0,0,1,0,0,0,1),'health':(12,12),'hud':(1,),'pause':(0,)}
            lookup={int(spec['address'],0):(name,spec['format']) for name,spec in config['reads'].items()}
            actions=[]
            def send(*lines):
                for line in lines:
                    if line.startswith('Plus~'):
                        actions.append('Plus');values['hud']=(0,);values['pause']=(1,)
                    elif line.startswith('memdump '):
                        _,address,size,path=line.split();name,fmt=lookup[int(address,16)]
                        Path(path).write_bytes(struct.pack('<'+fmt,*values[name]))
            bridge=Bridge(config);actor=Mock(spec=ActorReader);actor.observe.side_effect=ActorUnavailable('freed actor');bridge.actors=actor;actor.current_reads.return_value={}
            try:
                with patch.object(bridge,'send',side_effect=send):
                    with self.assertRaisesRegex(TrialStopped,'freed actor'):bridge.sample()
                    actor.observe.reset_mock();bridge.running=True;bridge.pause()
                    actor.observe.assert_not_called();self.assertEqual(actions,['Plus'])
                    assert bridge.last is not None
                    self.assertTrue(ui_matches(config,bridge.last,'pause'))
            finally:bridge.close()


if __name__=='__main__':unittest.main()
