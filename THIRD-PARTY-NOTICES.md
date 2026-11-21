# 第三方组件与许可声明

本文件列出 VAT 自动剪辑软件（下称"本软件"）中包含的第三方组件、各自的许可证，以及随包附带的许可文本位置。
许可文本原文在 `third_party_licenses/` 目录中；本文件与之一并构成对第三方许可证的履行。

- 对应版本：v0.1.0
- 最后更新：2026-11-20
- 构建环境：CPython 3.12 · PyInstaller 6.22.3 · Windows x64

---

## 一、随可执行程序分发的组件

单文件 `VAT.exe` 内部包含下列组件。它们均以未修改的上游形式打包。

| 组件 | 版本 | 许可证 | 在软件中的作用 | 许可文本 |
| --- | --- | --- | --- | --- |
| CPython | 3.12 | PSF-2.0 | 运行 Python 代码 | `CPython-LICENSE.txt` |
| Tcl/Tk | 8.6 | Tcl/Tk License（BSD 式） | 图形界面（Tkinter） | `Tcl-Tk-license.terms` |
| tkdnd | 随 tkinterdnd2 2.2.x | Tcl/Tk 式（上游未随包提供文本） | 文件拖拽 | 待补，见第五节 |
| tkinterdnd2 | 0.7.0 | MIT | 拖拽的 Python 绑定 | `tkinterdnd2-LICENSE` |
| PyInstaller bootloader | 6.22.3 | GPLv2+，附特殊例外 | 打包后的启动器 | `PyInstaller-COPYING.txt` |
| PyYAML | 6.0.3 | MIT | 读取 YAML 配置 | `PyYAML-LICENSE` |
| Pillow | 13.0.0 | MIT-CMU（HPND 系） | 图片读写与缩放 | `Pillow-LICENSE.txt` |
| langdetect | 1.0.9 | Apache-2.0 | 简介语言识别 | `langdetect-LICENSE` + `langdetect-NOTICE` |
| svg.path | 7.1 | MIT | 解析 SVG 路径，用于动效 | `svg.path-LICENSE.txt` |
| resvg-py | 0.5.0 | MIT | SVG 光栅化（Python 绑定） | `resvg-py-LICENSE` |
| resvg（Rust） | 由 resvg-py 静态链接 | MPL-2.0 | 实际的光栅化实现 | 见第四节 |

两点说明：

1. **langdetect 的包元数据写的是 MIT，实际是 Apache-2.0。** 其 `LICENSE` 与 `NOTICE` 均为 Apache-2.0（源自 Cybozu Labs 的 language-detection）。按 Apache-2.0 处理。
2. **PyInstaller 的 bootloader 是 GPLv2+**，但带有"允许用它打包并分发非自由程序"的特殊例外，因此本软件以专有许可分发不违反该许可证。文本见 `PyInstaller-COPYING.txt` 末尾的例外条款。

## 二、仅在 macOS / Linux 构建中包含

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| cryptography | ≥43 | Apache-2.0 OR BSD-3-Clause | 仅非 Windows 平台安装（见 `requirements.txt`），用于本机密钥加密 |

发布 macOS / Linux 的构建产物时，需把该组件的许可文本补入 `third_party_licenses/`。当前 Windows 版未包含它。

## 三、构建期工具（不随 exe 分发）

下列包只参与构建，不进入可执行文件。若你发布源码包，使用者自行安装时会重新引入它们：

PyInstaller 6.22.3（GPLv2+ 特殊例外，工具本体）、pyinstaller-hooks-contrib 2026.10（Apache-2.0）、altgraph 0.17.5（MIT）、pefile 2024.8.26（MIT）、pywin32-ctypes 0.2.3（BSD-3-Clause）、packaging 26.3（Apache-2.0 OR BSD-2-Clause）、setuptools 84.2.1（MIT）、six 1.17.0（MIT）。

## 四、MPL-2.0 组件（resvg）的说明

本软件包含以 Mozilla Public License 2.0 授权的 resvg 组件（Rust 实现，经由 resvg-py 静态链接）。

- 本软件**未修改** resvg 的源代码。
- resvg 的源代码可从 <https://github.com/linebender/resvg> 获取；MPL-2.0 文本见 <https://www.mozilla.org/MPL/2.0/>。
- 依据 MPL-2.0 第 3.1 条，以上信息即为"如何获取受 MPL 覆盖的源代码"的告知。
- 如需在离线环境完整履行，可把 MPL-2.0 全文另存为 `third_party_licenses/resvg-MPL-2.0.txt`（发布前可选，见第五节）。

## 五、发布前待补（尚未完成）

| 事项 | 影响 | 处理方式 |
| --- | --- | --- |
| tkdnd 的许可文本 | 已在 exe 内，但包内无文本 | 到 tkdnd 上游仓库取许可文本，另存为 `third_party_licenses/tkdnd-LICENSE` |
| resvg 的 MPL-2.0 全文 | 声明已足够，全文更稳妥 | 需要离线合规时补 `resvg-MPL-2.0.txt` |
| macOS / Linux 的 cryptography | 发布对应平台包才涉及 | 从 PyPI 包内的 `LICENSE` 复制 |

## 六、各类许可证要求做什么（自查用）

- **MIT / BSD / HPND 系**：保留版权声明与许可文本，随分发物提供。→ 本文件 + `third_party_licenses/` 已覆盖。
- **Apache-2.0**：保留版权与许可声明，附 `NOTICE` 文件，随附许可文本。→ langdetect 的 `NOTICE` 已附。
- **MPL-2.0**：声明使用了受 MPL 覆盖的文件，并告知如何获取其源代码。→ 见第四节。未修改该组件时，不要求公开本软件自身的代码。
- **GPLv2+ 例外（PyInstaller）**：例外条款允许把非自由程序打包分发；无需开源本软件。→ 见第一节说明。

## 七、外部程序与外部服务（不打包，不在此声明范围内）

本软件**不打包**下列程序，只调用你自行安装的版本；它们的许可与合规由你自行遵守：

| 外部程序 | 常见许可 | 备注 |
| --- | --- | --- |
| FFmpeg / FFprobe | LGPL-2.1+ 或 GPL-2.0+ | 含 libx264 的常见 Windows 构建为 GPL。再分发这些二进制需遵守 GPL。 |
| Ollama | MIT | 仅在你导入本地 GGUF 权重时使用 |
| GPT-SoVITS | MIT（以上游声明为准） | 本机配音 |
| Ego Lite | 第三方浏览器，按其自身条款 | 抖音选题 |
| Codex CLI / DeepSeek Harness | 按其自身条款 | 模型通道 |

你自行配置的云端服务（OpenAI 兼容接口、Anthropic、Google Gemini、ElevenLabs、SiliconFlow 等）按其各自条款处理你发送的数据，详见 README 的「数据会去哪儿」一节。

---

维护提示：新增或升级依赖后，先看该包的 `*.dist-info/licenses/`，把文本复制进 `third_party_licenses/`，再在第一节表格补一行。构建期依赖与运行期依赖要分清——只有进 exe 的才必须随包声明。
