"""
video_processor.py

Вся логика "тяжёлой" обработки видео:
- получить длительность и метаданные через ffprobe
- найти границы сцен через PySceneDetect
- выбрать N самых "ярких" сцен (по умолчанию просто равномерно по фильму + по детекции)
- вырезать и склеить их в один клип через ffmpeg
- прогнать через faster-whisper для распознавания речи -> .srt
- вшить субтитры в видео через ffmpeg (subtitles filter)
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("video_processor")


async def run_cmd(cmd: list[str]) -> tuple[str, str]:
    """Запускает внешнюю команду (ffmpeg/ffprobe) асинхронно и возвращает (stdout, stderr)."""
    log.info("RUN: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Команда завершилась с ошибкой ({proc.returncode}): {stderr.decode(errors='ignore')[-2000:]}")
    return stdout.decode(errors="ignore"), stderr.decode(errors="ignore")


async def get_duration_sec(path: Path) -> float:
    out, _ = await run_cmd([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    data = json.loads(out)
    return float(data["format"]["duration"])


async def detect_scenes(path: Path, max_scenes: int = 40) -> list[tuple[float, float]]:
    """
    Использует PySceneDetect (через его Python API) чтобы найти таймкоды смены сцен.
    Возвращает список (start_sec, end_sec) для каждой найденной сцены.
    """
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector

    def _detect_sync():
        video = open_video(str(path))
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=27.0))
        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()
        return [(s.get_seconds(), e.get_seconds()) for s, e in scene_list]

    scenes = await asyncio.to_thread(_detect_sync)

    if not scenes:
        return []

    # Если сцен слишком много - возьмём равномерно распределённую выборку
    if len(scenes) > max_scenes:
        step = len(scenes) / max_scenes
        scenes = [scenes[int(i * step)] for i in range(max_scenes)]

    return scenes


def pick_highlight_segments(
    scenes: list[tuple[float, float]],
    total_duration: float,
    target_seconds: int,
    clip_len: float = 3.0,
) -> list[tuple[float, float]]:
    """
    Выбирает короткие отрезки (по clip_len секунд) из найденных сцен,
    равномерно распределяя их по длительности фильма, пока не наберётся target_seconds.
    Если сцен не нашлось вообще - просто берёт равномерные отрезки по всему видео.
    """
    segments = []

    if not scenes:
        # запасной вариант: режем видео на равные интервалы
        n = max(1, int(target_seconds // clip_len))
        step = total_duration / (n + 1)
        for i in range(1, n + 1):
            start = step * i
            segments.append((start, min(start + clip_len, total_duration)))
        return segments

    n_needed = max(1, int(target_seconds // clip_len))

    if len(scenes) <= n_needed:
        chosen = scenes
    else:
        step = len(scenes) / n_needed
        chosen = [scenes[int(i * step)] for i in range(n_needed)]

    for start, end in chosen:
        scene_mid = (start + end) / 2
        seg_start = max(0, scene_mid - clip_len / 2)
        seg_end = min(total_duration, seg_start + clip_len)
        segments.append((seg_start, seg_end))

    return segments


async def cut_and_concat(path: Path, segments: list[tuple[float, float]], output: Path):
    """Вырезает каждый сегмент и склеивает их в один файл через ffmpeg concat."""
    tmp_dir = output.parent / "segments"
    tmp_dir.mkdir(exist_ok=True)

    list_file = tmp_dir / "list.txt"
    seg_paths = []

    for idx, (start, end) in enumerate(segments):
        seg_path = tmp_dir / f"seg_{idx:03d}.mp4"
        duration = max(0.1, end - start)
        await run_cmd([
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(path),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac",
            "-avoid_negative_ts", "make_zero",
            str(seg_path),
        ])
        seg_paths.append(seg_path)

    with open(list_file, "w") as f:
        for p in seg_paths:
            f.write(f"file '{p.resolve()}'\n")

    await run_cmd([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output),
    ])


async def transcribe_to_srt(path: Path, srt_path: Path, model_size: str = "small"):
    """Распознаёт речь в видео через faster-whisper и сохраняет результат как .srt субтитры."""
    from faster_whisper import WhisperModel

    def _transcribe_sync():
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(path), language=None, vad_filter=True)

        def fmt_ts(t: float) -> str:
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t % 60
            return f"{h:02}:{m:02}:{s:06.3f}".replace(".", ",")

        lines = []
        for i, seg in enumerate(segments, start=1):
            lines.append(str(i))
            lines.append(f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}")
            lines.append(seg.text.strip())
            lines.append("")
        return "\n".join(lines)

    srt_content = await asyncio.to_thread(_transcribe_sync)
    srt_path.write_text(srt_content, encoding="utf-8")


async def burn_subtitles(video_in: Path, srt_path: Path, video_out: Path):
    """Вшивает .srt субтитры прямо в кадр видео (hardsub) через ffmpeg subtitles filter."""
    # Путь нужно экранировать для фильтра ffmpeg
    srt_escaped = str(srt_path.resolve()).replace("\\", "/").replace(":", "\\:")
    await run_cmd([
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-vf", f"subtitles='{srt_escaped}':force_style='FontSize=20,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        str(video_out),
    ])


async def build_trailer(
    input_path: Path,
    output_path: Path,
    target_seconds: int = 60,
    max_duration_sec: int = 1800,
    progress_callback: Optional[Callable[[str], None]] = None,
):
    """Главная функция пайплайна: от исходного видео до готового трейлера с субтитрами."""

    def report(text: str):
        log.info(text)
        if progress_callback:
            progress_callback(text)

    duration = await get_duration_sec(input_path)
    if duration > max_duration_sec:
        raise ValueError(
            f"Видео слишком длинное ({duration / 60:.1f} мин), лимит {max_duration_sec / 60:.0f} мин."
        )

    report("Ищу смену сцен…")
    scenes = await detect_scenes(input_path)

    report(f"Найдено {len(scenes)} сцен, собираю нарезку…")
    segments = pick_highlight_segments(scenes, duration, target_seconds)

    raw_cut = output_path.parent / "raw_cut.mp4"
    await cut_and_concat(input_path, segments, raw_cut)

    report("Распознаю речь для субтитров…")
    srt_path = output_path.parent / "subs.srt"
    try:
        await transcribe_to_srt(raw_cut, srt_path)
        has_speech = srt_path.exists() and srt_path.stat().st_size > 0
    except Exception:
        log.exception("Whisper не справился, отдаю клип без субтитров")
        has_speech = False

    if has_speech:
        report("Накладываю субтитры…")
        await burn_subtitles(raw_cut, srt_path, output_path)
    else:
        raw_cut.rename(output_path)

    report("Финальная сборка готова.")
