import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import trip_video as app


class PersonalVideoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.object(app, 'JOBS', self.root/'jobs'))
        self.enterContext(patch.object(app, 'POLL_SECONDS', 0))
        self.cloud = Mock()
        self.cloud.connect.return_value = {'url': 'https://test.invalid', 'token': 'fake'}
        self.enterContext(patch.object(app, 'Runtime', return_value=self.cloud))
        self.auth = Mock(trip_owner='owner@example.test', headers={'x-goog-user-project':'test-project'})
        self.enterContext(patch('trip_auth.session',return_value=self.auth))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.launch = self.enterContext(patch.object(app, 'launch'))

    def cli(self, *args):
        with patch.object(sys, 'argv', ['trip_video.py', *args]):
            app.main()

    def test_generate_resume_no_second_allocation(self):
        video = b'fixture mp4 bytes'
        done = json.dumps({'state':'done','size':len(video),'sha256':hashlib.sha256(video).hexdigest()}).encode()
        with patch.object(app, 'read_remote', side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.cli('generate', '--prompt','A','--prompt','B','--prompt','C')
        job = next(app.JOBS.iterdir())
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual([v['prompt'] for v in json.loads((job/'job.json').read_text())['shots']], list('ABC'))
        with patch.object(app, 'read_remote', side_effect=[done,video]):
            self.cli('resume', job.name)
        self.cli('resume', job.name)
        self.assertEqual((job/'result.mp4').read_bytes(), video)
        self.cloud.allocate.assert_called_once()
        self.cloud.release.assert_called_once()

    def test_corrupt_transfer_retains_remote_then_cancel(self):
        job = app.new_ticket(['A','B','C'], [None]*3)
        app.atomic_write(job/'account.json', json.dumps(['owner@example.test','test-project']).encode())
        with patch.object(app, 'read_remote', side_effect=[b'{"state":"done","size":1,"sha256":"bad"}',b'A']):
            with self.assertRaises(app.Failure):
                self.cli('resume',job.name)
        self.assertFalse((job/'result.mp4').exists())
        self.cloud.release.assert_not_called()
        self.cli('cancel',job.name)
        self.cli('cancel',job.name)
        self.cloud.release.assert_called_once()
        self.cloud.allocate.assert_not_called()

    def test_model_failure_releases(self):
        job = app.new_ticket(['A','B','C'], [None]*3)
        app.atomic_write(job/'account.json', json.dumps(['owner@example.test','test-project']).encode())
        with patch.object(app,'read_remote',return_value=b'{"state":"failed"}'):
            with self.assertRaises(app.Failure):
                self.cli('resume',job.name)
        self.cloud.release.assert_called_once()

    def test_real_mp4_on_remote_then_lost_ack(self):
        path = app.ASSETS / 'run_once.py'
        s = importlib.util.spec_from_file_location('once', path)
        once = importlib.util.module_from_spec(s)
        s.loader.exec_module(once)
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('requires ffmpeg and ffprobe')
        rendered = self.root/'fixture.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=s=720x1280:r=24',
                        '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','10',
                        '-c:v','libx264','-preset','ultrafast','-c:a','aac',str(rendered)],check=True)
        (self.root/'remote_render.py').write_text(
            f'from pathlib import Path\nPath("result.mp4").write_bytes(Path({str(rendered)!r}).read_bytes())\n')
        once.run(self.root)
        status = json.loads((self.root/'render-status.json').read_text())
        self.assertEqual(status['state'],'done')
        self.assertEqual(status['sha256'],hashlib.sha256(rendered.read_bytes()).hexdigest())
        with patch.object(subprocess,'run',side_effect=AssertionError('duplicate launch')):
            once.run(self.root)

    def test_three_comfy_requests_keep_manifest_and_image_flags(self):
        spec = importlib.util.spec_from_file_location('renderer', app.ASSETS / 'remote_render.py')
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        output = self.root/'output'/'video'
        output.mkdir(parents=True)
        for n in range(1,4):
            (output/f'trip_shot{n}_00001.mp4').touch()
        http = Mock()
        http.post.return_value.json.return_value = {'prompt_id':'server-task'}
        http.get.return_value.json.return_value = {'server-task':{'status':{'status_str':'success'}}}
        scenes = [{'prompt': s, 'image': im} for s, im in zip('ABC',(False,True,False))]
        with patch.object(renderer,'requests',http):
            files = renderer.generate_shots(scenes, json.loads((app.ASSETS/'graph.json').read_text()),
                       self.root, Mock(), self.root/'log', 'http://local', '140:131', 'first_image')
        graphs = [c.kwargs['json']['prompt'] for c in http.post.call_args_list]
        self.assertEqual([g['140:131']['inputs']['prompt'] for g in graphs],list('ABC'))
        self.assertEqual([g['140:131']['inputs']['length'] for g in graphs],[73,73,107])
        self.assertEqual([('image' in g) for g in graphs],[False,True,False])
        self.assertEqual(graphs[1]['image']['inputs']['image'],'ref_2.jpg')
        self.assertEqual(len(files),3)
