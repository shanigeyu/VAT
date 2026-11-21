"""Write SHA-256 checksums for the release artifacts.

用法：
    python make_hashes.py                  # 校验 dist/ 下的标准产物，写入 dist/SHA256SUMS.txt
    python make_hashes.py dist/VAT.exe -o out.txt

输出格式与 sha256sum 一致，每行 "<哈希>  <文件名>"。文件名相对于校验文件所在目录，
把校验文件和产物放在一起即可直接校验。
"""

import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DEFAULT_ARTIFACTS = ("VAT.exe", "VAT-source.zip", "VAT-macOS.zip", "VAT-Linux.tar.gz")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(destination, targets=()):
    destination = Path(destination).resolve()
    if targets:
        files = [Path(target).resolve() for target in targets]
    else:
        files = [DIST / name for name in DEFAULT_ARTIFACTS]
    missing = [str(path) for path in files if not path.is_file()]
    files = [path for path in files if path.is_file()]
    if not files:
        raise SystemExit("没有可校验的产物：" + ("、".join(missing) or "未指定文件"))

    names = [path.name for path in files]
    if len(set(names)) != len(names):
        base = destination.parent
        names = [path.relative_to(base).as_posix() if base in path.parents else path.as_posix() for path in files]

    lines = [f"{sha256(path)}  {name}" for path, name in zip(files, names)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    for path, name in zip(files, names):
        print(f"{name}  {path.stat().st_size:,} 字节")
    if missing:
        print("跳过不存在的产物：" + "、".join(missing))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path, help="要校验的产物，缺省为 dist/ 下的标准产物")
    parser.add_argument("-o", "--output", type=Path, default=DIST / "SHA256SUMS.txt", help="校验文件路径")
    arguments = parser.parse_args()
    print(write_checksums(arguments.output, arguments.files))
