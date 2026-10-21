# 模型目录

模型文件不打进 Git。两种方式获取：

1. 自动下载（需联网）：
   ```bash
   ./download-model.sh
   ```
2. 使用预下载的目录：
   把 `model.int8.onnx` 和 `tokens.txt` 放到
   `models/sensevoice-small-int8/`，然后：
   ```bash
   .venv/bin/python -m app.model_store --check
   ```

`config.yaml` 里的 `asr.model_dir` 默认指向 `./models/sensevoice-small-int8`。

模型来源：sherpa-onnx 官方发布
`csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17` 的
`model.int8.onnx` + `tokens.txt`。下载脚本会校验文件大小与 SHA256。
