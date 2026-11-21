"""Read visible words from local footage and retain their source timecodes."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import task_control
from pathlib import Path


OCR = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$fileType = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$streamType = [Windows.Storage.Streams.IRandomAccessStreamWithContentType,Windows.Storage.Streams,ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$resultType = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$bridge = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Length -eq 1 -and $_.IsGenericMethod -and
    $_.GetParameters()[0].ParameterType.Name -eq ('IAsyncOperation' + [char]96 + '1')
} | Select-Object -First 1
function ResultOf($op, $type) {
    $task = $bridge.MakeGenericMethod($type).Invoke($null,@($op))
    $task.GetAwaiter().GetResult()
}
$engine = $ocrType::TryCreateFromUserProfileLanguages()
if (-not $engine -and $ocrType::AvailableRecognizerLanguages.Count) {
    $engine = $ocrType::TryCreateFromLanguage($ocrType::AvailableRecognizerLanguages[0])
}
if (-not $engine) { exit 2 }
$out = @{}
foreach ($frame in Get-ChildItem -LiteralPath . -Filter '*.jpg') {
    $stream = $null
    $bitmap = $null
    try {
        $item = ResultOf ($fileType::GetFileFromPathAsync($frame.FullName)) $fileType
        $stream = ResultOf ($item.OpenReadAsync()) $streamType
        $decoder = ResultOf ($decoderType::CreateAsync($stream)) $decoderType
        $bitmap = ResultOf ($decoder.GetSoftwareBitmapAsync()) $bitmapType
        $result = ResultOf ($engine.RecognizeAsync($bitmap)) $resultType
        $out[$frame.BaseName] = $result.Text
    } catch { continue }
    finally {
        if ($bitmap) { $bitmap.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}
ConvertTo-Json -InputObject $out -Compress
"""


def frame_words(folder, timeout):
    if os.name != "nt":
        binary = shutil.which("tesseract")
        if not binary:
            raise ValueError("本机画面识字需要安装 Tesseract 和中文或英文字库。")
        catalog = task_control.run_process([binary, "--list-langs"], capture_output=True, timeout=10)
        available = catalog.stdout.decode("utf-8", "replace").splitlines()[1:]
        langs = [code for code in ("chi_sim", "chi_tra", "eng") if code in available]
        if not langs:
            raise ValueError("Tesseract 缺少 chi_sim、chi_tra 或 eng 字库。")
        words = {}
        deadline = time.monotonic() + timeout
        for frame in sorted(folder.glob("*.jpg")):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = task_control.run_process([binary, str(frame), "stdout", "-l", "+".join(langs)],
                                        capture_output=True, timeout=min(remaining, 15))
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                words[frame.stem] = result.stdout.decode("utf-8", "replace").strip()
        return words
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = task_control.run_process([str(shell), "-NoProfile", "-NonInteractive", "-Command", OCR],
                               cwd=folder, capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        return {}
    return json.loads(completed.stdout.decode("utf-8-sig"))


def read_assets(root, assets, report):
    from production import IMAGES, VIDEOS, ffmpeg, media_seconds, probe

    samples, jobs = {}, []
    if root is None:
        return samples
    cache = Path(root) / "素材识字缓存"
    for aid, asset in assets.items():
        path = Path(asset["path"])
        if asset["category"] not in {"video", "image"} or not asset.get("enabled", 1) or not path.is_file():
            continue
        if path.suffix.lower() not in IMAGES | VIDEOS:
            continue
        stat = path.stat()
        identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        entry = cache / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        try:
            samples[aid] = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            jobs.append((aid, path, entry))
    if not jobs:
        return samples
    if os.name != "nt" and not shutil.which("tesseract"):
        report("未安装 Tesseract，已跳过画面识字。安装 Tesseract 和 chi_sim 字库后可识别中文素材。")
        return samples
    cache.mkdir(parents=True, exist_ok=True)
    for start in range(0, len(jobs), 12):
        batch = jobs[start:start+12]
        report(f"正在本机识别素材画面文字和对应时间 {start+1}–{start+len(batch)} / {len(jobs)}")
        with tempfile.TemporaryDirectory(prefix="vat-ocr-") as work:
            temp = Path(work)
            offsets = {}
            for i, (aid, path, _) in enumerate(batch):
                try:
                    times = [0]
                    if path.suffix.lower() in VIDEOS:
                        seconds = media_seconds(path, probe(path), "video")
                        if seconds >= 3:
                            times = [round(seconds * ratio, 2) for ratio in (.10, .48, .80)]
                    for j, t in enumerate(times):
                        key = f"f{i}-{j}"
                        ffmpeg("-ss", t, "-i", path, "-map", "0:v:0", "-frames:v", 1, "-vf",
                               "scale='min(1400,iw)':'min(1400,ih)':force_original_aspect_ratio=decrease",
                               "-q:v", 2, temp / (key + ".jpg"))
                        offsets[key] = aid, t
                except (OSError, ValueError):
                    continue
            try:
                words = frame_words(temp, 20 + len(offsets) * 5)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                report(f"本批素材未能识字：{exc}")
                words = {}
            for key, text in words.items():
                if key in offsets:
                    aid, offset = offsets[key]
                    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
                    samples.setdefault(aid, []).append({"offset": offset, "text": text[:700]})
            for aid, _, dest in batch:
                if aid in samples:
                    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache, delete=False) as fp:
                        json.dump(samples[aid], fp, ensure_ascii=False)
                    os.replace(fp.name, dest)
    return samples
