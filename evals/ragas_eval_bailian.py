"""
RAGAS v0.1.22 评估：百炼专属版操作手册
指标：Faithfulness / Answer Relevancy / Context Precision

用法：
  set API_KEY=your_key
  set BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  set EVAL_MODEL=qwen-turbo
  python evals\ragas_eval_bailian.py
"""

import json, os, sys, warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from datasets import Dataset
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision
from langchain_openai import ChatOpenAI
from harness.rag.tools import run_rag_search

API_KEY = os.getenv("API_KEY", "sk-xxx")
BASE_URL = os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EVAL_MODEL = os.getenv("EVAL_MODEL", "qwen-turbo")

QUESTIONS = [
    "在百炼应用平台中，怎么创建一个知识库问答智能体？",
    "瑶光平台的智能体任务节点可以配置哪些模型参数？",
    "大模型任务节点的高级配置有哪些开关选项？",
    "玉衡数据加工平台中，PDF解析并进行chunk切分的流程是什么？",
    "玉衡的语义结构树解析构建有什么作用？",
    "天璇训推平台怎么部署三方模型？",
    "天璇平台中怎么完成一个大语言模型的LoRA训练？",
    "瑶光的任务规划套件节点怎么配置？",
    "聚合节点有什么作用？怎么配置？",
    "瑶光平台怎么实现多路检索增强问答？",
]


def main():
    print("=" * 70)
    print(f"RAGAS v0.1.22 评估：百炼专属版操作手册")
    print(f"评判模型: {EVAL_MODEL}")
    print(f"评测问题: {len(QUESTIONS)} 条")
    print("=" * 70)

    # Step 1: 检索上下文，组装评估数据
    eval_data = {"question": [], "answer": [], "contexts": []}

    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n  [{i}/{len(QUESTIONS)}] 检索: {q[:50]}...")
        ctx_text = run_rag_search(q, top_k=5)
        chunks = [p.strip() for p in ctx_text.split("\n\n") if p.strip()]
        if not chunks:
            print("    ⚠ 未检索到内容，跳过")
            continue
        eval_data["question"].append(q)
        eval_data["answer"].append(chunks[0][:600])
        eval_data["contexts"].append(chunks)
        print(f"    ✓ 检索到 {len(chunks)} chunks")

    if len(eval_data["question"]) < 3:
        print("❌ 有效样本不足")
        return

    # Step 2: RAGAS 评估
    ds = Dataset.from_dict(eval_data)
    print(f"\n{'=' * 70}")
    print(f"运行 RAGAS evaluate() — {len(ds)} 条样本...")

    llm = ChatOpenAI(
        model=EVAL_MODEL,
        openai_api_key=API_KEY,
        openai_api_base=BASE_URL,
        temperature=0,
    )

    result = evaluate(
        ds,
        metrics=[faithfulness, answer_relevancy, context_precision],
        llm=llm,
    )

    # Step 3: 输出结果
    print(f"\n{'=' * 70}")
    print("📊 评估结果")
    print(f"{'=' * 70}")
    print(result)

    df = result.to_pandas() if hasattr(result, "to_pandas") else result
    out = ROOT / "evals" / "results" / "ragas_bailian_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        if isinstance(df, dict):
            json.dump(df, f, ensure_ascii=False, indent=2)
        else:
            df.to_json(f, orient="records", force_ascii=False)
    print(f"\n详细报告: {out}")


if __name__ == "__main__":
    main()
