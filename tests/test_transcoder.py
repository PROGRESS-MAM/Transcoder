from pathlib import Path
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch
import sys,unittest,json,subprocess
from tempfile import TemporaryDirectory
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import transcoder
from transcoder import _analysis as a,_render as r,_presets as p,_media as m

class TranscoderTests(unittest.TestCase):
    def setUp(self):
        self.source=SimpleNamespace(width=1920,height=1080,sar=Fraction(1))
        self.values=a.section('black_bars',{'extra_crop':10})
    def measured(self,top=0,bottom=0,left=0,right=0):
        y,x=np.indices((1080,1920));image=(40+x//20%50+y//10%50).astype(np.uint8)
        if top:image[:top]=0
        if bottom:image[-bottom:]=0
        if left:image[:,:left]=0
        if right:image[:,-right:]=0
        f=a._gray_lines(image.tobytes(),1920,1080)
        return a._rate_black_bars(self.source,self.values,[[f] for _ in range(10)])
    def test_pillarbox_does_not_trigger(self):
        found=self.measured(left=240,right=240)
        self.assertEqual(found['rating'],'none');self.assertEqual(found['bars']['left'],0)
        self.assertEqual(a.fix_black_bars(found,self.values,1920,1080),({},None))
    def test_gradient_is_not_black(self):self.assertEqual(self.measured()['rating'],'none')
    def test_letterbox_inside_pillarbox(self):
        found=self.measured(top=50,bottom=50,left=240,right=240)
        self.assertEqual(found['bars'],{'top':50,'bottom':50,'left':0,'right':0})
        self.assertEqual(a.fix_black_bars(found,self.values,1920,1080)[0]['crop'],(0,60,1920,960))
    def test_extra_only_on_affected_side(self):
        found=self.measured(top=50)
        self.assertEqual(a.fix_black_bars(found,self.values,1920,1080)[0]['crop'],(0,60,1920,1020))
    def test_low_confidence_stops(self):
        found=self.measured(top=50);found['trust']=.5
        with self.assertRaises(a.FixError):a.fix_black_bars(found,self.values,1920,1080)
    def test_audio_silent_channels_removed(self):
        found={'channels':[{'channel':i,'trust':1} for i in range(1,6)],'audible':[2,4,5]}
        self.assertEqual(a.fix_audio(found,{'trust':.9},{'min_audible':2},5)[0]['audio_map'],[2,4,5])
    def test_leading_silent_channels(self):
        found={'channels':[{'channel':i,'trust':1} for i in range(1,5)],'audible':[3,4],'pairs':[]}
        self.assertEqual(a.fix_audio(found,{'trust':.9},{'channels':2},4)[0]['audio_map'],[3,4])
    def test_mono_duplicated(self):
        found={'channels':[{'channel':i,'trust':1} for i in range(1,4)],'audible':[3]}
        self.assertEqual(a.fix_audio(found,{'trust':.9},{'min_audible':2},3)[0]['audio_map'],[3,3])
    def test_silent_audio_stops(self):
        found={'channels':[{'channel':1,'trust':1}],'audible':[]}
        with self.assertRaises(a.FixError):a.fix_audio(found,{'trust':.9},{'min_audible':2},1)
    def test_unclear_silence_stops(self):
        found={'channels':[{'channel':1,'trust':1},{'channel':2,'trust':.8}],'audible':[1]}
        with self.assertRaises(a.FixError):a.fix_audio(found,{'trust':.9},{'min_audible':2},2)
    def test_master_presets(self):
        _,master=p.load('dnxhr_hqx');_,inter=p.load('dnxhd_185x')
        self.assertEqual(master['video']['frame'],'source');self.assertEqual(inter['video']['frame'],'1920x1080')
        self.assertEqual(master['audio'],{'codec':'pcm','bit_depth':24,'sample_rate':48000,'layout':'tracks'})
    def test_proportional_cover(self):
        _,master=p.load('dnxhr_hqx')
        plan=SimpleNamespace(frame=(1920,1080,Fraction(1)),field=None,preset=master)
        filters=r._picture(plan,1920,1080,Fraction(1),(0,60,1920,960),False)
        self.assertIn('scale=2160:1080:flags=lanczos',filters)
        self.assertIn('crop=1920:1080:120:0:exact=1',filters)
        self.assertFalse(any(item.startswith('pad=') for item in filters))
    def test_no_raster_switch(self):
        _,master=p.load('dnxhr_hqx')
        self.source.width=1280;self.source.height=720
        self.assertEqual(r._frame(self.source,master,(0,40,1280,640))[:2],(1280,720))
    def test_unsupported_side_data_stops(self):
        s=SimpleNamespace(width=1280,height=720,sar=Fraction(1),video=SimpleNamespace(data={'side_data_list':[{'side_data_type':'Mastering display metadata'}]}))
        with self.assertRaisesRegex(ValueError,'Zusatzmetadaten'):r.check_source_encoder(s,'dnxhr_hqx')

class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root=TemporaryDirectory();cls.folder=Path(cls.temp_root.name)
        cls.fixture=cls.folder/'source.mkv'
        args=['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i',
              'testsrc2=s=1280x720:r=25,drawbox=x=0:y=0:w=iw:h=50:color=black:t=fill,drawbox=x=0:y=670:w=iw:h=50:color=black:t=fill',
              '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-f','lavfi','-i','sine=frequency=880:sample_rate=44100',
              '-f','lavfi','-i','anullsrc=r=44100:cl=mono','-t','1','-map','0:v','-map','1:a','-map','2:a','-map','3:a',
              '-c:v','ffv1','-threads:v','1','-c:a','pcm_s16le','-colorspace','bt709','-color_primaries','bt470bg',
              '-color_trc','gamma28','-color_range','tv',
              '-metadata','timecode=02:03:04:05','-metadata','title=Prüfclip',str(cls.fixture)]
        subprocess.run(args,check=True,capture_output=True,timeout=30)
    @classmethod
    def tearDownClass(cls):cls.temp_root.cleanup()
    def setUp(self):
        self.temp = TemporaryDirectory(dir=self.folder)
        self.output_folder = Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()
    def checked_render(self,src,out):
        original=r._video_codec_args
        with patch.object(r,'_video_codec_args',side_effect=lambda video,field=None:original(video,field)+['-threads:v','1']):
            return transcoder.render(src,out)[0]
    def test_real_progressive_master(self):
        src=transcoder.source(self.fixture)
        output=transcoder.output('dnxhr_hqx',self.output_folder/'master.mov',fixes=[transcoder.fix('audio',{'samples':{'full':True}},min_audible=2)],modify=[transcoder.crop(0,60,1280,600)])
        r.check_source_encoder(src,'dnxhr_hqx')
        original=r._video_codec_args
        with patch.object(r,'_video_codec_args',side_effect=lambda video,field=None:original(video,field)+['-threads:v','1']):
            result=transcoder.render(src,output)[0]
        self.assertEqual(result.state,'ok',result.summary)
        probe=m.probe(result.file);video=next(s for s in probe['streams'] if s['codec_type']=='video');audio=[s for s in probe['streams'] if s['codec_type']=='audio']
        self.assertEqual((video['width'],video['height']),(1280,720));self.assertEqual(video['color_space'],'bt709');self.assertEqual(video['color_primaries'],'bt470bg');self.assertEqual(video['color_transfer'],'bt470bg')
        self.assertEqual(len(audio),2);self.assertTrue(all(s['codec_name']=='pcm_s24le' and s['sample_rate']=='48000' and s['channels']==1 for s in audio))
        self.assertEqual(probe['format']['tags']['title'],'Prüfclip')
    def test_real_interlaced_master(self):
        fixture=self.output_folder/'interlaced.mkv'
        subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','testsrc2=s=720x576:r=50,tinterlace=interleave_top',
                        '-f','lavfi','-i','sine=sample_rate=48000','-t','0.4','-map','0:v','-map','1:a','-c:v','ffv1',
                        '-threads:v','1','-field_order','tt','-c:a','pcm_s16le','-colorspace','bt709',
                        '-metadata','timecode=01:00:00:00',str(fixture)],check=True,capture_output=True,timeout=30)
        src=transcoder.source(fixture);src.video.data['field_order']='tt'
        r.check_source_encoder(src,'dnxhd_185x')
        out=transcoder.output('dnxhd_185x',self.output_folder/'interlaced.mov',
                             modify=[transcoder.crop(0,20,720,536),transcoder.audio_map([1,1])])
        result=self.checked_render(src,out)
        self.assertEqual(result.state,'ok',result.summary)
        probe=m.probe(result.file);video=probe['streams'][0]
        self.assertEqual((video['width'],video['height']),(1920,1080));self.assertEqual(video['profile'],'DNXHD')
        self.assertEqual(video['avg_frame_rate'],'25/1')
    def test_timecode_shift(self):
        src=transcoder.source(self.fixture)
        out=transcoder.output('dnxhr_hqx',self.output_folder/'shift.mov',modify=[transcoder.audio_map([1,2])],
                             timecode=transcoder.timecode(shift='-00:00:01:00'))
        result=self.checked_render(src,out);self.assertEqual(result.state,'ok',result.summary)
        self.assertEqual(m.probe(result.file)['format']['tags']['timecode'],'02:03:03:05')
    def test_inserted_black_and_timecode_anchor(self):
        src=transcoder.source(self.fixture)
        out=transcoder.output('dnxhr_hqx',self.output_folder/'intro.mov',modify=[transcoder.audio_map([1,2])],
                             merge=[transcoder.black(.4),transcoder.SOURCE],timecode=transcoder.timecode(anchor=transcoder.SOURCE))
        result=self.checked_render(src,out);self.assertEqual(result.state,'ok',result.summary)
        self.assertEqual(m.probe(result.file)['format']['tags']['timecode'],'02:03:03:20')
    def test_output_validation_rejects_wrong_audio(self):
        src=transcoder.source(self.fixture)
        out=transcoder.output('dnxhr_hqx',self.output_folder/'bad.mov',modify=[transcoder.audio_map([1,2])])
        plan=r.prepare(src,out,lambda *_:None,{})
        wrong={'streams':[{'codec_type':'video','codec_name':'h264','profile':'High','width':1280,'height':720,
                          'avg_frame_rate':'25/1','sample_aspect_ratio':'1:1','nb_frames':'25','pix_fmt':'yuv420p'}],
               'format':{'tags':{'timecode':src.start_tc}}}
        frames=json.dumps({'frames':[{'interlaced_frame':0}]})
        with patch.object(m,'probe',return_value=wrong),patch.object(m,'run_text',return_value=(frames,'')):
            with self.assertRaisesRegex(ValueError,'Audioformat'):r._verify_master(src,plan)
    def test_real_black_bar_decoding(self):
        src=transcoder.source(self.fixture)
        found=a.check_black_bars(src,a.section('black_bars',{'samples':{'full':True}}),lambda *_:None)
        self.assertEqual(found['bars'],{'top':50,'bottom':50,'left':0,'right':0})
    def test_unrepresentable_color_stops_before_render(self):
        src=transcoder.source(self.fixture);src.video.data['color_space']='bt470bg'
        with self.assertRaisesRegex(ValueError,'Farbangaben'):r.check_source_encoder(src,'dnxhr_hqx')
    def test_unsupported_metadata_never_renders(self):
        src=transcoder.source(self.fixture);src.video.data['side_data_list']=[{'side_data_type':'Display Matrix'}]
        out=transcoder.output('dnxhr_hqx',self.output_folder/'unsupported.mov')
        with patch.object(r,'run',side_effect=AssertionError('must not render')):
            result=transcoder.render(src,out)[0]
        self.assertEqual(result.state,'error');self.assertFalse(out.file.exists())
    def test_dnxhd_preflight_rejects_unsupported_rate(self):
        src=transcoder.source(self.fixture);src.fps=Fraction(24);src.video.data['field_order']='tt'
        with self.assertRaises(ValueError):r.check_source_encoder(src,'dnxhd_185x')
    def test_dnxhd_preflight_accepts_25_interlaced(self):
        src=transcoder.source(self.fixture);src.video.data['field_order']='tt'
        r.check_source_encoder(src,'dnxhd_185x')

if __name__=='__main__':unittest.main(verbosity=2)
