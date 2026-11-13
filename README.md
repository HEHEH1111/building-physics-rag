# 建筑热湿 RAG 知识库

在本机按问题检索建筑热湿 PDF。点击引用即可回到原文页。核心流程包含领域判断、中英术语扩展、SQLite FTS5 检索和公式原版裁切。

## 运行一页示例

在项目根目录使用 Python 3.12。以下命令适用于 Windows PowerShell，建议在新建的虚拟环境中运行。

```powershell
python -m pip install -r requirements.txt
python make_demo.py
Copy-Item 资料路径.example.json 资料路径.json
python indexer.py
python app.py --open
```

在本机 8784 端口的页面输入“含湿量”，查看示例中干重 100 g、湿重 105 g 的计算和第 1 页引用。示例由脚本生成，可随时重建。

## 使用自己的 PDF

在本地的 `资料路径.json` 中，把目录列在“实验”、“EnergyPlus”或“COMSOL”分组下，然后重新运行索引。数据库会保存 PDF 正文和本机路径。建议把自有资料放入已排除的 `private-docs/`。原生文本页直接索引，扫描页的 OCR 取决于系统可用的识别组件。

默认展示带页码的检索原文。如需启用生成式中文回答，可在 Windows 上自行配置兼容的 llama.cpp 服务端及其依赖到 `answer-runtime/`，并将兼容的 GGUF 模型保存为 `answer-model/qwen.gguf`。离线翻译另需 ctranslate2、sentencepiece，以及 `translation-model/translate-{en_zh,zh_en}-1_9/` 下对应方向的 sentencepiece.model 和 model 目录。
