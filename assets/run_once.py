"""Runs on the Linux Colab VM. Relaunch after a lost reply is idempotent."""
import fcntl
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run(root):
    with (root / 'render.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        render(root)


def render(root):
    status = root / 'render-status.json'

    def publish(value):
        scratch = status.with_suffix('.tmp')
        scratch.write_text(json.dumps(value))
        scratch.replace(status)

    # Even a crash after GPU work started must never start a second render.
    if status.exists():
        return
    publish({'state': 'running'})
    try:
        with (root / 'render.log').open('w') as log:
            subprocess.run([sys.executable, '-u', str(root / 'remote_render.py')],
                           cwd=root, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=4200)
        mp4 = root / 'result.mp4'
        info = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height,duration',
            '-of', 'json', str(mp4)], timeout=30)
        job = json.loads((root / 'job.json').read_bytes())
        size = job['width'], job['height']
        duration = sum(shot['frames'] for shot in job['shots']) / 24
        streams = json.loads(info)['streams']
        video = [s for s in streams if s['codec_type'] == 'video']
        audio = [s for s in streams if s['codec_type'] == 'audio']
        if (len(video) != 1 or len(audio) != 1 or
                (video[0]['width'], video[0]['height']) != size or
                any(abs(float(s['duration']) - duration) >= .05 for s in video + audio)):
            raise ValueError('Invalid rendered media')
        publish({'state': 'done', 'size': mp4.stat().st_size,
                 'sha256': hashlib.sha256(mp4.read_bytes()).hexdigest()})
    except Exception as exc:
        publish({'state': 'failed', 'error': type(exc).__name__})
        raise


if __name__ == '__main__':
    run(Path('/content'))
