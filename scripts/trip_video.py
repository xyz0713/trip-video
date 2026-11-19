# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["google-colab-cli==0.8.0", "Pillow==12.0.0"]
# ///
"""Personal Colab client. The only paid allocation happens in generate()."""
import argparse
import base64
import hashlib
import io
import json
import logging
import math
import re
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageOps

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
JOBS = Path.home() / '.config' / 'trip-video' / 'personal-jobs'
API = 'https://colaboratory.googleapis.com/v1beta'
POLL_SECONDS = 15


class Failure(Exception):
    """User-facing error with no credentials or remote code."""


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def ticket(value):
    try:
        uid = uuid.UUID(value)
    except ValueError:
        raise Failure('受付IDが正しくありません。') from None
    if str(uid) != value or uid.version != 4 or not (JOBS / value / 'job.json').is_file():
        raise Failure('このPCに受付が見つかりません。')
    return JOBS / value


def parse_size(value):
    match = re.fullmatch(r'([0-9]+)[xX×]([0-9]+)', value)
    if match:
        width, height = map(int, match.groups())
        if min(width, height) > 0 and width % 2 == height % 2 == 0:
            return width, height
    raise argparse.ArgumentTypeError('幅x高さを正の偶数で指定してください。')


def new_ticket(prompts, images, size=(864, 480), seconds=None):
    if not prompts or any(not p.strip() for p in prompts):
        raise Failure('動画のプロンプトを指定してください。')
    if any(images[len(prompts):]):
        raise Failure('参考画像に対応するプロンプトがありません。')
    seconds = 5 * len(prompts) if seconds is None else seconds
    if not math.isfinite(seconds) or not 0 < seconds <= 150 * len(prompts):
        raise Failure('尺は正の秒数で、1クリップあたり最大150秒です。')
    total = round(seconds * 24)
    frames, remainder = divmod(total, len(prompts))
    if frames < 1:
        raise Failure('各クリップが1フレーム以上になる尺を指定してください。')
    normalized = {}
    for i, path in enumerate(images, 1):
        if path:
            with Image.open(path) as img:
                if img.width * img.height > 40_000_000:
                    raise Failure('参考画像は4000万画素以下を指定してください。')
                img.thumbnail((1280, 1280))
                buf = io.BytesIO()
                ImageOps.exif_transpose(img).convert('RGB').save(buf, format='JPEG', quality=90)
                normalized[i] = buf.getvalue()
    job = JOBS / str(uuid.uuid4())
    job.mkdir(parents=True, mode=0o700)
    for i, data in normalized.items():
        atomic_write(job / f'ref_{i}.jpg', data)
    payload = {
        'width': size[0], 'height': size[1],
        'shots': [{'prompt': prompt, 'image': i in normalized, 'frames': frames + int(i <= remainder)}
                  for i, prompt in enumerate(prompts, 1)],
    }
    atomic_write(job / 'job.json', json.dumps(payload, ensure_ascii=False).encode())
    print(f'受付ID {job.name}', flush=True)
    return job


class Runtime:
    def __init__(self, auth, job):
        self.auth = auth
        self.uid = job.name
        self.name = 'trip-video-' + uuid.UUID(job.name).hex
        self.url = f'{API}/runtimes/{self.name}'

    def allocate(self):
        r = self.auth.get(API + '/runtimespecs', timeout=30)
        r.raise_for_status()
        spec = next((s['key'] for s in r.json().get('runtimeSpecs', [])
                     if s.get('eligible') and s['key'].get('accelerator') == 'L4'
                     and s['key'].get('variant') == 'VARIANT_GPU'
                     and s['key'].get('shape') == 'SHAPE_STANDARD'), None)
        if spec is None:
            raise Failure('tripのColabでL4を現在確保できません。')
        try:
            r = self.auth.post(API + '/runtimes', params={'runtimeId': self.name, 'requestId': self.uid},
                               headers={'X-Server-Timeout': '240'}, json={'runtimeSpec': spec}, timeout=265)
        except (requests.ConnectionError, requests.Timeout):
            return  # A timed-out create may have succeeded. Poll the same ID.
        if r.status_code not in (409, 500, 502, 503, 504):
            if not r.ok:
                raise Failure(f'ColabがGPU確保を受け付けませんでした（HTTP {r.status_code}）。')
            error = r.json().get('error', {})
            if error and error.get('code') != 6:
                raise Failure(f'ColabでGPUを確保できませんでした（code {error.get("code")}）。')

    def connect(self):
        response = self.auth.get(self.url, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        info = response.json().get('connectionInfo', {})
        return info if info.get('url') and info.get('token') else None

    def release(self):
        r = self.auth.delete(self.url, params={'allowMissing': 'true'}, timeout=60)
        if r.status_code != 404:
            r.raise_for_status()


def read_remote(info, filename):
    for folder in ('', 'content/'):
        r = requests.get(info['url'].rstrip('/') + '/api/contents/' + folder + filename,
                         headers={'X-Colab-Runtime-Proxy-Token': info['token']},
                         params={'content': '1'}, timeout=60, allow_redirects=False)
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            raise requests.HTTPError(response=r)
        obj = r.json()
        return (base64.b64decode(obj['content'], validate=True)
                if obj.get('format') == 'base64' else obj['content'].encode())
    return None


def launch(info, job):
    from colab_cli.runtime import ColabRuntime

    paths = [job / 'job.json', *sorted(job.glob('ref_*.jpg')),
             *(ASSETS / f for f in ('remote_render.py', 'run_once.py', 'graph.json'))]
    files = {p.name: base64.b64encode(p.read_bytes()).decode('ascii') for p in paths}
    # v0.1 receipts predate user-selected geometry and timing.
    manifest = json.loads((job / 'job.json').read_bytes())
    if 'width' not in manifest:
        manifest.update(width=720, height=1280, model_size=[480, 864])
        for shot, frames in zip(manifest['shots'], (73, 73, 94), strict=True):
            shot['frames'] = frames
        files['job.json'] = base64.b64encode(json.dumps(manifest).encode()).decode('ascii')
    # Atomic identical input replacement also permits resuming an interrupted upload.
    code = (
        'import base64, os, subprocess, sys, tempfile\nfrom pathlib import Path\n'
        f'files = {files!r}\n'
        'for name, content in files.items():\n'
        "    with tempfile.NamedTemporaryFile(dir='/content', delete=False) as f:\n"
        '        f.write(base64.b64decode(content))\n'
        "    os.replace(f.name, Path('/content') / name)\n"
        "subprocess.Popen([sys.executable, '/content/run_once.py'], cwd='/content', "
        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, '
        'start_new_session=True)\n')
    outputs = ColabRuntime(info['url'], info['token']).execute_code(code, timeout=120)
    if any(x.get('output_type') == 'error' for x in outputs):
        raise Failure('Colabで起動を確認できません。受付IDで resume または cancel を実行してください。')


def finish(job, runtime):
    if not (job / 'released').exists():
        runtime.release()
        atomic_write(job / 'released', b'ok')


def recover(job, runtime):
    result = job / 'result.mp4'
    if result.exists():
        finish(job, runtime)
        return result
    if (job / 'released').exists():
        raise Failure('この受付のGPUは解放済みです。')
    start = time.monotonic()
    launched = False
    misses = 0
    while time.monotonic() - start < 4500:
        try:
            info = runtime.connect()
            if info is None:
                if time.monotonic() - start > 180:
                    raise Failure('この受付のランタイムを確認できません。Colabで失効した場合は復元できません。')
            else:
                data = read_remote(info, 'render-status.json')
                if data is None and not launched:
                    launch(info, job)
                    launched = True
                    print('Colabで生成処理を起動しました。', flush=True)
                elif data is None and time.monotonic() - start > 300:
                    raise Failure('起動の応答待ちです。受付IDで resume または cancel を実行してください。')
                elif data:
                    status = json.loads(data)
                    if status['state'] == 'failed':
                        finish(job, runtime)
                        raise Failure('Colabの生成処理が失敗しました。この受付のGPUを解放しました。')
                    if status['state'] == 'done':
                        video = read_remote(info, 'result.mp4')
                        if (not video or len(video) != status['size'] or
                                hashlib.sha256(video).hexdigest() != status['sha256']):
                            raise Failure('MP4の照合に失敗しました。同じ受付IDで再取得してください。')
                        atomic_write(result, video)
                        print(f'MP4 {result}', flush=True)
                        finish(job, runtime)
                        return result
            misses = 0
        except requests.RequestException:
            misses += 1
            if misses >= 4:
                raise Failure('Colabとの通信が中断しました。受付IDで resume または cancel を実行してください。') from None
        time.sleep(POLL_SECONDS)
    raise Failure('待機時間が経過しました。受付IDで resume または cancel を実行してください。')


def main():
    import trip_auth

    parser = argparse.ArgumentParser(description='Colab上のMiniMax H3で動画を生成')
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login')
    login.add_argument('--account', required=True, help='使用する本人のGoogleメールアドレス')
    login.add_argument('--project', required=True, help='Colab APIが有効なGoogle CloudプロジェクトID')
    for name in ('check', 'list'):
        sub.add_parser(name)
    create = sub.add_parser('generate')
    create.add_argument('--prompt', action='append', required=True)
    create.add_argument('--size', type=parse_size, default=(864, 480), metavar='WIDTHxHEIGHT')
    create.add_argument('--seconds', type=float, help='動画全体の秒数')
    create.add_argument('--image', action='append', type=Path, default=[], help='参考画像（プロンプト順）')
    for i in range(1, 4):  # Accept the image switches used by v0.1.
        create.add_argument(f'--image-{i}', type=Path, help=argparse.SUPPRESS)
    for name in ('resume', 'cancel'):
        sub.add_parser(name).add_argument('id')
    args = parser.parse_args()
    if args.command == 'list':
        for p in sorted(JOBS.glob('*/job.json'), key=lambda f: f.stat().st_mtime, reverse=True):
            folder = p.parent
            state = ('MP4保存済み' if (folder/'result.mp4').exists() else
                     '解放済み' if (folder/'released').exists() else '実行状況はresumeで確認')
            print(folder.name, state)
        return
    if args.command == 'login':
        trip_auth.login(args.account, args.project).close()
        print('本人のColab認証を保存しました。')
        return
    if args.command == 'check':
        auth = trip_auth.session()
        r = auth.get(API + '/runtimespecs', timeout=30)
        r.raise_for_status()
        print('tripの保存済み認証でColab APIへの接続を確認しました。')
        return
    if args.command == 'generate':
        auth = trip_auth.session()
        images = list(args.image)
        for i in range(3):
            old_image = getattr(args, f'image_{i + 1}')
            if old_image:
                if len(images) <= i:
                    images.extend([None] * (i + 1 - len(images)))
                elif images[i]:
                    raise Failure('同じ場面に参考画像が重複しています。')
                images[i] = old_image
        job = new_ticket(args.prompt, images, args.size, args.seconds)
        atomic_write(job / 'account.json', json.dumps([auth.trip_owner, auth.headers['x-goog-user-project']]).encode())
        runtime = Runtime(auth, job)
        try:
            runtime.allocate()
        except Failure:
            # A definitive rejection means this ticket has no payable runtime.
            # A timeout or lost create reply instead takes the recovery path.
            finish(job, runtime)
            raise
    else:
        job = ticket(args.id)
        if args.command == 'resume' and (job/'result.mp4').is_file() and (job/'released').exists():
            print(job/'result.mp4')
            return
        auth = trip_auth.session()
        owner = json.loads((job / 'account.json').read_bytes())
        if owner != [auth.trip_owner, auth.headers['x-goog-user-project']]:
            raise Failure('この受付を作成したGoogleアカウントとプロジェクトでログインしてください。')
        runtime = Runtime(auth, job)
    if args.command == 'cancel':
        finish(job, runtime)
        print('この受付のGPUを解放しました。')
    else:
        print(recover(job, runtime))


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)  # upstream SDK can log OAuth URLs and proxy details
    try:
        main()
    except (Failure, KeyboardInterrupt) as exc:
        sys.exit(str(exc) if isinstance(exc, Failure) else '中断しました。受付IDで resume または cancel を実行できます。')
    except Exception as exc:
        from trip_auth import AuthError
        if isinstance(exc, AuthError):
            sys.exit(str(exc))
        # Upstream exceptions can contain a callback URL or proxy tokens.
        code = getattr(getattr(exc, 'response', None), 'status_code', None)
        sys.exit(f'処理が完了しませんでした（{type(exc).__name__}{f" HTTP {code}" if code else ""}）。受付済みの場合は同じIDで resume または cancel を実行してください。')
