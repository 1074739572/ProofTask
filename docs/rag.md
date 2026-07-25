# RAG 仿写工作流

本地 RAG 服务 **长文档仿写**（技术报告 / 结题 / 实施方案），**不**用于联网查论文（那是 lookup mode + fetch）。

原版与当前架构的差异、技术选型和取舍见 [RAG 演进说明](./rag-evolution.md)。

---

## 1. 放文档（语料）

在 **agent 工作目录**（`python main.py` 的 cwd）下建：

```text
files/
  样例/              # 格式参考 docx/md
  指标与交付要求.md   # 硬性指标与交付
```

支持后缀：`.md`、`.txt`、`.docx`、`.pdf`。PDF 使用 PyMuPDF 提取页内文本、检测表格并导出嵌入图片。
`files/` 默认 gitignore，语料只放本机。

### PDF 图表检索

PDF 入库后，图片原件存放在 `.rag/assets/`，而索引中只保存资产 URI、图注、同页文本提示和视觉描述；不会把图片 base64 写入向量库。表格转成 Markdown 表格后作为独立可检索块。

要让系统真正理解图像内容（图表趋势、坐标轴、图例、图片文字），配置一个 OpenAI 兼容的视觉模型，然后**重新索引**：

```sh
export HARNESS_RAG_VLM_MODEL=gpt-4o-mini
export HARNESS_RAG_VLM_API_KEY=...
# 可选：兼容服务地址；未设置时使用 OPENAI_BASE_URL
export HARNESS_RAG_VLM_BASE_URL=https://your-compatible-endpoint/v1
python main.py rag index files
```

未配置视觉模型时，图片仍会被保存和索引到（图注及同页可提取文本），但结果会明确标注“未生成视觉描述”，不会错误声称已识别图片细节。

扫描型 PDF 没有文本层时可选 VLM、Tesseract 或混合 OCR。`hybrid` 先使用已配置的 VLM 做保留布局的整页转录，失败、未配置或超预算时再回退本机 Tesseract：

```sh
export HARNESS_RAG_PDF_OCR_MODE=hybrid  # vlm | tesseract | hybrid | off
export HARNESS_RAG_PDF_OCR_LANGUAGE=chi_sim+eng
python main.py rag index files
```

`vlm` 模式不需要安装 Tesseract，但会把扫描页发送到视觉模型并消耗调用额度。模型转录会保留标题、数值和单位，将表格转换为 Markdown，并为图表补充简短说明。

---

## 2. 自动流程（writing mode）

用户消息含 **仿写 / 撰写 / 技术报告 / files/** 等 → 自动：

1. `[writing mode]` 追加仿写约束  
2. **`rag_index` 等价**：对 `files/` 建索引（`.rag/`）  
3. ephemeral 注入索引状态  
4. **WritingGuard**：`write_file` 到 `output/*.md` 前必须有本会话 `rag_search`

也可显式：

```text
/rag add D:\docs\样例报告.docx     # CLI：拷入 files/ 并建索引
/rag index files                   # CLI：重建索引
rag_index path="files"             # Agent 工具
load_skill("thesis-writing")       # 同样会触发索引
rag_search query="引言 编写目的 段落结构"
write_file path="output/01_引言.md" ...
```

不启动 Agent 时，可在项目根目录：

```sh
python main.py rag add path\to\报告.docx
python main.py rag index files
python main.py rag docs
python main.py rag select 1,3
python main.py rag ask "技术路线分几个阶段？"
python main.py rag status
```

### 文件模式（`/mode file`）

与 **direct**（普通 Agent）相对：进文件模式后，**每条普通消息都走「检索 → 回答」**，不再进 coding agent loop。

```text
/rag                     # 总览：本地 files/ 有哪些、是否已索引
/rag files               # 只看本地上传（✓已索引 / ·未索引 / ↻需刷新）
/rag index files         # 有未索引或过期时先建索引
/mode file               # 默认搜索全部已索引文档
技术路线是什么？         # 直接问，自动 RAG
/rag select 1,3          # 可选：按 /rag docs 编号限定范围
/rag pick                # TUI/终端交互多选（Space 勾选，Enter 保存）
搜全部                   # 恢复全部范围
/mode direct             # 退出，回到普通对话
```

| 模式 | 行为 |
|------|------|
| `direct` | 普通 Agent，不自动做文档 RAG 问答 |
| `file` | 句句 grounded 问答；范围 = 指定文档或全部 |
| `plan` / `orchestrate` | 原有规划 / 编排 |

Classic 提示符在文件模式下为 `[model|file] >`；Textual TUI 使用同一条 file-mode 短路，不进入 coding agent loop。提问时会先显示「检索中 / 命中 N 条」，最终回答走 assistant 气泡（含来源引用），不会被误渲染成灰色 step 行。索引为空或文件变化时，提问会先列出本地 `files/` 并提示 `/rag index files`（不会静默漏检）。

### 选文档提问（仍可用 `/rag ask`）

```text
/rag docs
/rag pick
/rag ask 报告的技术路线是什么？
```

选择与范围保存在 `.rag/selection.json`（`scope: all|selected`）。
该范围同时作用于 file 问答与 Agent 的 `rag_search`；显式传入 `source` 时以该 source 为准。

环境变量：`HARNESS_RAG_QA_LLM=0` 只返回检索摘录不调用模型；`HARNESS_RAG_QA_CONTEXT_CHARS` 控制送入模型的上下文长度。

---

## 3. 架构

### 父子块（Parent-Child）

每个标题节（如 `1.1 编写目的`）生成：

| 类型 | `level` | 作用 |
|------|---------|------|
| **父块** | `parent` | 整节全文（最多 8000 字），不进入检索 |
| **子块** | `child` | 按 ~600 字切分，带 `parent_id`，**只检索子块** |

检索命中子块后，`rag_search` 自动附上父块整节上下文（默认最多 4000 字）。

```text
## 1.1 编写目的          ← 标题栈
├── 父块 parent_id=p_xxx   整节：「本报告旨在…项目背景…」
├── 子块 child#0           「本报告旨在…」          parent_id=p_xxx
└── 子块 child#1           「项目面向风云四号…」    parent_id=p_xxx
```

### 流水线

```text
ingest (parse) → chunking (parent+child, merge short) → chunks/*.jsonl
                    ↓
   PDF: 文本 + Markdown 表格 + 图片资产/VLM 描述
                    ↓
         parents.json + BM25/Chroma（仅可检索子块）
                    ↓
 中文分词/查询扩展 → hybrid search (RRF) → rerank
                    ↓
       图表模态加权 + 同页去重/多样化 → 附父块 → 输出
```

存储：

```text
.rag/
  chunks/*.jsonl       # 父块 + 子块
  assets/<source-hash>/ # PDF 导出的原图
  index/corpus.json    # 可检索子块（BM25）
  index/parents.json   # 父块查找表
  chroma/              # 子块向量
```

| 工具 | 作用 |
|------|------|
| `rag_index path="files"` | 解析语料并重建 BM25 + 向量索引 |
| `rag_status` | sources / 父块·子块数 / embedding / 检索模式 |
| `rag_search` | 子块混合检索 + 重排 + 父块上下文 |

### 环境变量

| 变量 | 默认 | 含义 |
|------|------|------|
| `HARNESS_WRITING_GUARD` | `1` | 写 output 前必须 rag_search |
| `HARNESS_RAG_HIT_CHARS` | `1500` | 每条命中子块最大字符 |
| `HARNESS_RAG_PARENT_CHARS` | `4000` | 附带的父块上下文最大字符 |
| `HARNESS_RAG_MODE` | `hybrid` | `hybrid` / `bm25` / `vector` |
| `HARNESS_RAG_EMBEDDING` | `auto` | `auto` / `hash` / `openai` / `bge-m3` |
| `HARNESS_RAG_RERANK` | `1` | 检索后 lexical 重排（`0` 关闭） |
| `HARNESS_RAG_RERANK_MODEL` | *(空)* | 设 cross-encoder 模型名启用深度重排 |
| `HARNESS_RAG_FETCH_K` | `20` | 融合前候选数 |
| `HARNESS_RAG_RRF_K` | `60` | RRF 常数 |
| `HARNESS_RAG_MODALITY_BONUS` | `0.2` | 问题命中图/表意图时对应模态加分 |
| `HARNESS_RAG_MIN_RELATIVE_SCORE` | `0.1` | 相对最佳结果的最低分数比例 |
| `HARNESS_RAG_MAX_PER_PAGE` | `3` | 同一 PDF 页同一模态最多保留结果数 |
| `HARNESS_RAG_VLM_MODEL` | *(空)* | OpenAI 兼容视觉模型；设置后为 PDF 图片生成检索描述 |
| `HARNESS_RAG_VLM_API_KEY` | `OPENAI_API_KEY` | 视觉模型 API Key |
| `HARNESS_RAG_VLM_BASE_URL` | `OPENAI_BASE_URL` | 可选视觉模型兼容 API 地址 |
| `HARNESS_RAG_VLM_RENDER_VECTOR_PAGES` | `1` | 将 PDF 矢量图形区域渲染后送 VLM；`0` 关闭 |
| `HARNESS_RAG_PDF_OCR_MODE` | `off` | 扫描页 OCR：`vlm` / `tesseract` / `hybrid` / `off` |
| `HARNESS_RAG_PDF_OCR_LANGUAGE` | `chi_sim+eng` | Tesseract/hybrid 回退使用的语言包 |
| `HARNESS_RAG_PDF_OCR_DPI` | `200` | OCR 分辨率；越高越慢 |

**Embedding 自动选择（`auto`）**：有 `OPENAI_API_KEY` → OpenAI；否则用本地 hash（CI/无网）；设 `HARNESS_RAG_EMBEDDING=bge-m3` 且安装 `sentence-transformers` 可走本地语义模型。

BM25 使用 `jieba` 中文分词，并补充中文二元字组以覆盖技术名词和短查询。趋势、数量、流程等查询会做保守同义词扩展；向量查询仍使用原始问题，避免扩展词改变语义。

**可选深度重排**：

```sh
pip install sentence-transformers
export HARNESS_RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

---

## 4. 评测

完整的分层指标、金标 schema 和 CI 建议见 [RAG 分层测试与评测](./rag-evaluation.md)。

迷你语料：`evals/rag/fixtures/tiny_corpus/`  
金标查询：`evals/rag/gold_queries.yaml`

```sh
python main.py rag eval
python main.py rag eval --output evals/results/rag/latest.json
pytest tests/test_rag_*.py tests/test_file_mode.py tests/test_tui_m1.py -q
```

Python API：

```python
from harness.rag.eval import run_eval
report = run_eval("evals/rag/fixtures/tiny_corpus")
# report["metrics"]["recall_at_5"], report["metrics"]["ndcg_at_5"]
```

---

## 5. 与 lookup 的分工

| 模式 | 场景 | 数据源 |
|------|------|--------|
| **lookup** | 有没有某论文、联网检索 | fetch / OpenAlex |
| **writing** | 仿写报告章节 | 本地 `files/` RAG |

同一条消息若像「查找论文」则只走 lookup，不走 writing。
