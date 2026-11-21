"""Create the source release from the files maintained with VAT."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    ".github/workflows/desktop.yml", ".gitignore", "README.md", "使用说明.md", "requirements.txt",
    "LICENSE", "THIRD-PARTY-NOTICES.md", "CHANGELOG.md",
    "docs/RELEASE-NOTES-v0.1.0.md", "docs/RELEASE-NOTES-v0.1.1.md", "docs/RELEASE-NOTES-v0.1.2.md",
    "docs/RELEASE-NOTES-v0.2.1.md",
    "third_party_licenses",
    "vat.spec", "make_source_zip.py", "make_hashes.py", "studio.ico", "docs/settings.png", "docs/cover.png", "ego_douyin.mjs",
    "api_profiles.py", "api_settings.py", "app_paths.py", "appearance.py", "asset_reading.py",
    "auto_video.py", "auto_video_ui.py", "codex_harness.py", "covers.py", "creative_art.py", "desktop.py",
    "discovery.py", "generated_images.py", "gguf_ui.py", "local_models.py", "planning.py",
    "production.py", "providers.py", "release.py", "render_progress.py", "review.py", "script_timing.py", "settings_ui.py", "shot_visuals.py",
    "source_visuals.py", "sovits.py", "speech_api.py", "step_models.py", "store.py", "studio.py",
    "talk_import.py", "task_control.py", "traditional_chars.py", "ui_language.py", "widgets.py", "workflow_ui.py",
    "tests/test_asset_folders.py", "tests/test_audio_alignment.py", "tests/test_auto_video.py",
    "tests/test_auto_visuals.py", "tests/test_codex_images.py", "tests/test_covers.py",
    "tests/test_creative_video.py", "tests/test_desktop.py",
    "tests/test_ego_result.py", "tests/test_elevenlabs.py", "tests/test_github_topics.py", "tests/test_local_api.py",
    "tests/test_local_models.py", "tests/test_model_settings.py", "tests/test_review.py",
    "tests/test_script_timing.py", "tests/test_shot_materials.py", "tests/test_speech_api.py",
    "tests/test_talk_import.py", "tests/test_tutorial.py", "tests/test_user_storage.py", "tests/test_job_control.py",
)


def source_entries():
    """Yield each file in SOURCE_FILES; a directory entry is included recursively."""
    for name in SOURCE_FILES:
        item = ROOT / name
        if item.is_dir() and item.resolve() == item:
            yield from sorted(path for path in item.rglob("*") if path.is_file())
            continue
        if not item.is_file() or item.resolve() != item:
            raise ValueError(f"发行清单需要项目中的常规文件或目录：{name}")
        yield item


def write_source_zip(destination):
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as scratch:
        package = Path(scratch) / destination.name
        with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
            for path in source_entries():
                archive.write(path, path.relative_to(ROOT).as_posix())
        package.replace(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT / "dist/VAT-source.zip")
    print(write_source_zip(parser.parse_args().output))
