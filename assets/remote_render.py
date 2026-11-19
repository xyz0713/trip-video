"""Trusted Colab-side renderer. Only job.json prompt strings are user supplied."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess, sys, time, json, shutil
import requests
import copy
start = time.perf_counter()
def mark(label):
    print(datetime.now(timezone.utc).isoformat(timespec='seconds'), label, f'{time.perf_counter()-start:.1f}s', flush=True)
def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)
def generate_shots(job, graph, comfy, server, log_path, base, node, first):
    sources=[]
    width, height = job.get('model_size') or [((job[axis] + 31) // 32) * 32 for axis in ('width', 'height')]
    for shot, scene in enumerate(job['shots'], 1):
        job_graph=copy.deepcopy(graph)
        if job.get('draft'):
            job_graph['140:124']['inputs']['steps'] = 10
        job_graph['image']['inputs']['image']=f'ref_{shot}.jpg'
        job_graph[node]['inputs'].update(prompt=scene['prompt'], width=width, height=height,
                                         length=max(5, scene['frames']))
        if scene['image']:
            job_graph[node]['inputs'][first]=['image',0]
        else:
            del job_graph['image']
        job_graph['140:129']['inputs']['noise_seed']=912469+shot
        prefix=f'trip_shot{shot}'
        job_graph['92']['inputs']['filename_prefix']='video/'+prefix
        shot_start=time.perf_counter()
        response=requests.post(base+'/prompt',json={'prompt':job_graph},timeout=60)
        if not response.ok: raise RuntimeError(response.text[:6000])
        prompt_id=response.json()['prompt_id']
        while True:
            history=requests.get(base+'/history/'+prompt_id,timeout=10).json().get(prompt_id)
            if history: break
            if server.poll() is not None: raise RuntimeError(log_path.read_text()[-6000:])
            time.sleep(1)
        if history['status']['status_str']!='success': raise RuntimeError(log_path.read_text()[-6000:])
        sources.append(max((comfy/'output'/'video').glob(prefix+'*.mp4'),key=lambda p:p.stat().st_mtime))
        mark(f'SHOT_{shot}_DONE {time.perf_counter()-shot_start:.1f}s')
    return sources


def encode(sources, job, destination):
    width, height = job['width'], job['height']
    args = ['ffmpeg', '-y', '-loglevel', 'error']
    filters = []
    pads = []
    for i, (src, scene) in enumerate(zip(sources, job['shots'], strict=True)):
        args.extend(['-i', str(src)])
        frames = scene['frames']
        filters.append(f'[{i}:v]trim=end_frame={frames},setpts=PTS-STARTPTS,'
                       f'scale={width}:{height}:force_original_aspect_ratio=increase,'
                       f'crop={width}:{height},setsar=1[v{i}]')
        filters.append(f'[{i}:a]apad,atrim=duration={frames / 24:.8f},asetpts=PTS-STARTPTS[a{i}]')
        pads.append(f'[v{i}][a{i}]')
    filters.append(''.join(pads) + f'concat=n={len(sources)}:v=1:a=1[v][a]')
    run(args + ['-filter_complex', ';'.join(filters), '-map', '[v]', '-map', '[a]',
                '-r', '24', '-c:v', 'libx264', '-crf', '17', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(destination)])


def main():
    mark('START')
    run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv'])
    print('Free disk GiB:', round(shutil.disk_usage('/content').free / 2**30), flush=True)
    comfy = Path('/content/ComfyUI')
    if not comfy.exists():
        run(['git','clone','--depth','1','https://github.com/Comfy-Org/ComfyUI.git',str(comfy)])
    run(['git','-C',str(comfy),'rev-parse','HEAD'])
    run([sys.executable,'-m','pip','install','-q','-r',str(comfy/'requirements.txt')])
    mark('INSTALLED')

    from huggingface_hub import HfApi, hf_hub_download
    from concurrent.futures import ThreadPoolExecutor
    repo = 'Comfy-Org/MiniMax-H3'
    api = HfApi()
    revision = api.model_info(repo).sha
    paths = api.list_repo_files(repo,revision=revision)
    assets = [('vae', 'minimax_h3_video_vae_int8_convrot.safetensors'), ('vae', 'minimax_h3_audio_vae_fp32.safetensors'), ('diffusion_models', 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'), ('text_encoders', 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors')]
    def fetch(item):
        directory, name = item
        matches = [p for p in paths if p.split('/')[-1]==name]
        if len(matches)!=1: raise ValueError((name,matches))
        cached = hf_hub_download(repo, matches[0], revision=revision)
        target = comfy/'models'/directory/name
        target.parent.mkdir(exist_ok=True,parents=True)
        if not target.exists(): target.symlink_to(cached)
        mark(name)
    print('HF revision', revision, flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fetch, assets))
    mark('MODELS_READY')

    log_path = Path('/content/h3_colab_server.log')
    base = 'http://127.0.0.1:8188'
    with log_path.open('w') as log:
        server = subprocess.Popen([sys.executable,'-u','main.py','--listen','127.0.0.1','--port','8188','--disable-auto-launch'],cwd=comfy,stdout=log,stderr=subprocess.STDOUT)
    for _ in range(180):
        try:
            if requests.get(base+'/system_stats',timeout=3).ok: break
        except requests.RequestException: pass
        if server.poll() is not None: raise RuntimeError(log_path.read_text()[-7000:])
        time.sleep(1)
    else: raise TimeoutError(log_path.read_text()[-5000:])
    mark('SERVER_READY')
    from PIL import Image
    job = json.loads(Path('/content/job.json').read_text())
    graph = json.loads(Path('/content/graph.json').read_text())
    for index, shot in enumerate(job['shots'], 1):
        if shot['image']:
            with Image.open(f'/content/ref_{index}.jpg') as image:
                image.convert('RGB').save(comfy/'input'/f'ref_{index}.jpg')

    node='140:131'
    schema=requests.get(base+'/object_info/MiniMaxH3ImageToVideo',timeout=10).json()['MiniMaxH3ImageToVideo']['input']
    image_params=[k for k,v in schema.get('optional',{}).items() if v[0]=='IMAGE']
    first=next((k for k in image_params if 'start' in k or 'first' in k),None)
    if first is None and any(shot['image'] for shot in job['shots']): raise RuntimeError(f'No first frame input in {image_params}')
    generation_start=time.perf_counter()
    mark('GENERATION_START')
    sources = generate_shots(job, graph, comfy, server, log_path, base, node, first)
    generation_s=time.perf_counter()-generation_start
    mark('GENERATION_DONE')
    video=Path('/content/result.mp4')
    encode(sources, job, video)
    mark('MP4_READY')
    print('Total seconds',round(time.perf_counter()-start,2),'Generation seconds',round(generation_s,2),flush=True)



if __name__ == '__main__':
    main()
