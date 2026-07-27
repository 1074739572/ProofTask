"""
基于「基于深度学习的对流云识别与外推算法研究」论文的 RAG 测试问题
所有问题均可从该论文的 RAG 索引中检索到答案。
"""

RAG_TEST_QUESTIONS = [
    {
        "id": 1,
        "question": "本文提出的深对流云识别算法叫什么？它的核心架构是什么？",
        "ground_truth": "DIF-UNet，基于Swin Transformer和ResNet的双编码器U型分割网络。",
    },
    {
        "id": 2,
        "question": "本文使用的主要卫星数据来源是什么？",
        "ground_truth": "FY-4A静止气象卫星多通道扫描数据、Himawari-8云分类产品、太阳角度辅助信息。",
    },
    {
        "id": 3,
        "question": "CLFIM模块在DIF-UNet中的作用是什么？",
        "ground_truth": "跨层特征交互模块，充当编码器和解码器之间的桥梁，整合高级和低级特征，减少特征信息丢失。",
    },
    {
        "id": 4,
        "question": "本文的深对流云识别算法有哪些局限性？",
        "ground_truth": "依赖可见光通道，夜间适用受限；存在误检漏检问题；推理成本较高。",
    },
    {
        "id": 5,
        "question": "本文外推算法采用什么训练策略来减缓误差积累？",
        "ground_truth": "多步自回归的多阶段训练方法。",
    },
    {
        "id": 6,
        "question": "本文识别算法中对比了哪些经典模型？",
        "ground_truth": "FCN、UNet、CMTFNet、DeepLab v3+、Swin Transformer、SCNET、Swin-unet、ST-UNet。",
    },
    {
        "id": 7,
        "question": "本文使用深对流云识别任务中采用了哪些评价指标？",
        "ground_truth": "论文中使用了精确率（Precision）、召回率（Recall）、F1分数、交并比（IoU）等语义分割常用评价指标。",
    },
    {
        "id": 8,
        "question": "本文提出的两个主要创新点是什么？",
        "ground_truth": "1. 融合Swin Transformer和ResNet的双编码器U型分割网络；2. 基于Swin Transformer的卫星通道外推模型结合深对流云识别算法的临近外推预测方案。",
    },
    {
        "id": 9,
        "question": "SIFM模块在DIF-UNet中有什么功能？",
        "ground_truth": "SIFM用于融合Swin Transformer编码器和ResNet编码器的特征。",
    },
    {
        "id": 10,
        "question": "本文用于外推预测展示的台风案例是什么？",
        "ground_truth": "台风苏拉（2023年8月）。",
    },
    {
        "id": 11,
        "question": "DIF-UNet中Patch Embedding模块的patch尺寸和步幅是多少？",
        "ground_truth": "每个patch尺寸为4×4×16，滑动窗口步幅与patch大小一致。",
    },
    {
        "id": 12,
        "question": "本文外推算法的误差积累问题是如何产生的？作者提出了什么未来改进方向？",
        "ground_truth": "外推算法采用单步自回归方式进行多步预测，误差随步数增加而放大。未来可探索更优方案减小误差传播。",
    },
]
