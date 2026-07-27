"""
RAG 评估脚本 —— 用 DashScope（通义千问）做评判 LLM
计算三个关键指标：Faithfulness、Answer Relevancy、Context Precision

指标算法与 RAGAS 一致，但不依赖 ragas 库。
"""

import json
import os
import time
from typing import Any

import openai
from rag_eval_data import EVAL_SAMPLES

# ── 用 DashScope ──
client = openai.OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)


def llm_judge(prompt: str) -> str:
    """调用通义千问做评判。"""
    resp = client.chat.completions.create(
        model="qwen-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,  # 评判需要确定性
        max_tokens=1000,
    )
    return resp.choices[0].message.content


# ═══════════════════════════════════════════════════════════════
# 指标 1: Faithfulness（忠实度）
# 算法：LLM 从 answer 提取 claims → 逐条检查是否被 contexts 支持
# 分数 = 可证实的 claims / 总 claims
# ═══════════════════════════════════════════════════════════════

FAITHFULNESS_PROMPT = """你的任务是评判 RAG 系统生成的答案是否忠实于检索到的文档。

## 上下文（检索到的文档片段）：
{contexts}

## 生成的答案：
{answer}

## 要求：
1. 将"生成的答案"拆解为若干独立的声明（claims/statements）。
2. 对每一条声明，检查它是否可以被"上下文"中的信息支持或推导。
3. 输出 JSON，格式如下：
{{
  "claims": [
    {{"statement": "声明内容", "supported": true/false, "reason": "简短说明"}}
  ],
  "faithfulness_score": 0.0  // supported==true 的声明数 / 总声明数
}}

注意：
- 如果声明在上下文中完全没有依据而是答案自己编造的，标记为 false。
- 如果声明对上下文做了合理的概括或转述，标记为 true。
- 如果答案没有任何可拆解的声明（比如只有 "我不知道"），返回空 claims 数组，faithfulness_score = 1.0
"""


def _extract_json(raw: str) -> str:
    """从 LLM 返回中提取 JSON 字符串。"""
    # 优先提取 ```json ... ``` 块
    if "```json" in raw:
        parts = raw.split("```json", 1)
        if len(parts) > 1:
            inner = parts[1].split("```", 1)
            return inner[0].strip()
    if "```" in raw:
        parts = raw.split("```", 1)
        if len(parts) > 1:
            inner = parts[1].split("```", 1)
            return inner[0].strip()
    # 否则尝试找到第一个 { 和最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start:end + 1]
    return raw.strip()


def evaluate_faithfulness(question: str, answer: str, contexts: list[str]) -> dict:
    prompt = FAITHFULNESS_PROMPT.format(
        contexts="\n---\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts)),
        answer=answer,
    )
    try:
        raw = llm_judge(prompt)
        return json.loads(_extract_json(raw))
    except Exception as e:
        print(f"  [WARN] Faithfulness parse failed: {e}")
        return {"claims": [], "faithfulness_score": 0.0, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 指标 2: Answer Relevancy（答案相关性）
# 算法：LLM 根据 answer 反推假设问题 → 计算与原始 question 的语义相似度
# 简化做法：用 LLM 直接打 0-1 分（与 RAGAS 反推 + 余弦相似度等价方向）
# ═══════════════════════════════════════════════════════════════

ANSWER_RELEVANCY_PROMPT = """你的任务是评判生成的答案是否直接、完整地回答了用户的问题。

## 用户问题：
{question}

## 生成的答案：
{answer}

## 要求：
请从以下三个维度评判答案的相关性，每项 0-1 分：

1. **相关性（relevant）**：答案内容是否在回答这个问题？有没有跑题或答非所问？
2. **完整性（complete）**：答案是否覆盖了问题的核心要点？有没有遗漏关键信息？
3. **紧致性（concise）**：答案是否有大量无关内容或冗余？

输出 JSON：
{{
  "relevant": 0.0,  // 是否切题
  "complete": 0.0,  // 是否覆盖要点
  "concise": 0.0,   // 是否紧致
  "overall_relevancy": 0.0  // 综合评分 = (relevant + complete + concise) / 3
}}
"""


def evaluate_answer_relevancy(question: str, answer: str) -> dict:
    prompt = ANSWER_RELEVANCY_PROMPT.format(question=question, answer=answer)
    try:
        raw = llm_judge(prompt)
        return json.loads(_extract_json(raw))
    except Exception as e:
        print(f"  [WARN] Answer Relevancy parse failed: {e}")
        return {"overall_relevancy": 0.0, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 指标 3: Context Precision（上下文精度）
# 算法：逐条判断 context 是否与问题相关，按位次加权
# 相关 chunk 越靠前分数越高
# ═══════════════════════════════════════════════════════════════

CONTEXT_PRECISION_PROMPT = """你的任务是判断检索到的文档片段是否与用户问题相关。

## 用户问题：
{question}

## 检索到的文档片段：
{context}

## 要求：
判断这个文档片段是否包含能帮助回答用户问题的信息。
输出 JSON：
{{
  "relevant": true/false,
  "reason": "简短说明"
}}
"""


def evaluate_context_precision(question: str, contexts: list[str]) -> dict:
    if not contexts:
        return {"precision": 0.0, "per_context": []}

    results = []
    for i, ctx in enumerate(contexts):
        prompt = CONTEXT_PRECISION_PROMPT.format(question=question, context=ctx)
        try:
            raw = llm_judge(prompt)
            r = json.loads(_extract_json(raw))
        except Exception:
            r = {"relevant": False, "reason": "parse error"}

        r["rank"] = i + 1
        results.append(r)

    # 计算 weighted precision：相关 chunk 按 rank 加权
    # precision@k = (前 k 个中相关的数量) → averaged over relevant positions
    relevant_count = sum(1 for r in results if r.get("relevant"))
    if relevant_count == 0:
        precision = 0.0
    else:
        weighted_sum = 0.0
        for k, r in enumerate(results, 1):
            if r.get("relevant"):
                # 前 k 个中相关的比例
                relevant_in_top_k = sum(
                    1 for r2 in results[:k] if r2.get("relevant")
                )
                weighted_sum += relevant_in_top_k / k
        precision = weighted_sum / relevant_count

    return {
        "precision": round(precision, 4),
        "per_context": results,
    }


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════


def run_evaluation(samples: list[dict], start_idx: int = 0, end_idx: int | None = None):
    results = []
    subset = samples[start_idx:end_idx]

    print(f"{'='*70}")
    print(f"RAG 评估开始 — 共 {len(subset)} 条样本")
    print(f"评判 LLM: qwen-turbo (DashScope)")
    print(f"{'='*70}\n")

    for i, s in enumerate(subset):
        sid = s["id"]
        question = s["question"]
        answer = s["answer"]
        contexts = s["contexts"]
        ground_truth = s.get("ground_truth", "")

        print(f"[{i+1}/{len(subset)}] 样本 #{sid}: {question[:50]}...")
        time.sleep(0.3)  # API 限速

        faith = evaluate_faithfulness(question, answer, contexts)
        relevancy = evaluate_answer_relevancy(question, answer)
        ctx_precision = evaluate_context_precision(question, contexts)

        results.append(
            {
                "id": sid,
                "question": question,
                "answer": answer,
                "ground_truth": ground_truth,
                "faithfulness": faith,
                "answer_relevancy": relevancy,
                "context_precision": ctx_precision,
            }
        )

        print(
            f"  Faithfulness: {faith.get('faithfulness_score', 0):.2f}  "
            f"Relevancy: {relevancy.get('overall_relevancy', 0):.2f}  "
            f"Context Precision: {ctx_precision.get('precision', 0):.2f}"
        )

        # 打印 claim 详情（如果有幻觉）
        claims = faith.get("claims", [])
        unsupported = [c for c in claims if not c.get("supported")]
        if unsupported:
            for c in unsupported:
                print(f"  [!] 幻觉: {c['statement'][:80]}")

        # 打印无关 context
        for c in ctx_precision.get("per_context", []):
            if not c.get("relevant"):
                print(f"  [x] 无关检索: [{c['rank']}] {c.get('reason', '')}")

        print()

    # ── 汇总 ──
    avg_faith = sum(r["faithfulness"].get("faithfulness_score", 0) for r in results) / len(results)
    avg_relevancy = sum(r["answer_relevancy"].get("overall_relevancy", 0) for r in results) / len(results)
    avg_precision = sum(r["context_precision"].get("precision", 0) for r in results) / len(results)

    print(f"{'='*70}")
    print(f"评估汇总（{len(results)} 条）")
    print(f"{'='*70}")
    print(f"  Faithfulness (忠实度)      : {avg_faith:.2f}")
    print(f"  Answer Relevancy (答案相关性): {avg_relevancy:.2f}")
    print(f"  Context Precision (上下文精度): {avg_precision:.2f}")
    print(f"{'='*70}")

    return results


if __name__ == "__main__":
    results = run_evaluation(EVAL_SAMPLES)
    # 保存完整结果
    output_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已保存到: {output_path}")
