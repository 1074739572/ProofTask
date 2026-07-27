"""
RAG 评估测试集：15 条模拟样本
覆盖正常、边界、幻觉、无关检索、部分相关等场景
每条包含: question, answer, contexts, ground_truth (可选)
"""

EVAL_SAMPLES = [
    # ── 正常用例 ──
    {
        "id": 1,
        "question": "什么是 Transformer 中的自注意力机制？",
        "answer": "自注意力机制（Self-Attention）是 Transformer 的核心组件，它让每个输入 token 都能与序列中所有其他 token 计算相关性权重，从而捕获全局上下文信息。其计算过程为 Q·K^T / √d_k 后接 Softmax，再乘以 V。",
        "contexts": [
            "自注意力机制允许每个位置关注序列中的所有位置，通过 Query、Key、Value 三个矩阵计算注意力权重。",
            "Transformer 由 Vaswani 等人在 2017 年提出，核心创新是用自注意力替代 RNN 的循环结构。",
            "多头注意力是将多个自注意力头的输出拼接，使模型能关注不同表示子空间的信息。",
        ],
        "ground_truth": "自注意力机制是 Transformer 的核心，通过 Q、K、V 矩阵计算每个 token 与所有 token 的相关性权重，公式为 softmax(QK^T/√d_k)·V，能并行捕获全局上下文。",
    },
    {
        "id": 2,
        "question": "Python 的 GIL 是什么？为什么会影响多线程性能？",
        "answer": "GIL（Global Interpreter Lock，全局解释器锁）是 CPython 中的互斥锁，保证同一时刻只有一个线程执行 Python 字节码。因此 CPU 密集型任务无法利用多核并行，多线程反而可能因锁竞争变慢。IO 密集型任务不受明显影响，因为 IO 操作会释放 GIL。",
        "contexts": [
            "GIL 是 CPython 解释器中的全局解释器锁，同一进程内同一时刻只允许一个线程执行 Python 字节码。",
            "CPU 密集型任务受 GIL 影响大，多线程无法利用多核。IO 密集型任务在等待 IO 时释放 GIL，多线程仍有效。",
            "Python 多进程（multiprocessing）可以绕过 GIL，每个进程有独立的 Python 解释器。",
        ],
        "ground_truth": "GIL 是 CPython 的全局解释器锁，确保同一时刻仅一个线程执行 Python 字节码，导致 CPU 密集型任务无法利用多核但 IO 密集型任务不受影响。",
    },
    {
        "id": 3,
        "question": "数据库索引的 B+ 树和 B 树有什么区别？",
        "answer": "B+ 树与 B 树的区别主要有三点：第一，B+ 树所有数据都存在叶子节点，内部节点只存键值；第二，B+ 树叶子节点通过指针连成有序链表，方便范围查询；第三，B+ 树查询效率更稳定，每次查询都必须走到叶子节点。",
        "contexts": [
            "B+ 树是 B 树的变体，所有数据记录都存储在叶子节点，内部节点仅作为索引。",
            "B+ 树的叶子节点通过指针串联成有序链表，范围查询效率高。",
            "B 树的数据分布在所有节点上，查找可能在非叶子节点就结束。",
        ],
        "ground_truth": "B+ 树数据全在叶子节点且叶子节点有链表连接，范围查询更高效且查询性能稳定；B 树数据分布所有节点，单点查找可能更快但不适合范围查询。",
    },
    # ── 部分相关（answer 部分跑偏）──
    {
        "id": 4,
        "question": "Kubernetes 中 Pod 和 Deployment 的关系是什么？",
        "answer": "Deployment 是 Kubernetes 中管理 Pod 的控制器，负责声明期望的副本数、滚动更新和回滚。而 Service 则负责 Pod 的负载均衡和服务发现。",
        "contexts": [
            "Deployment 是 Pod 之上的抽象层，管理 Pod 副本数、滚动更新策略和回滚。",
            "Pod 是 K8s 最小调度单元，一个 Pod 可包含一个或多个容器。",
        ],
        "ground_truth": "Deployment 是一种 Pod 控制器，管理 Pod 副本数、更新策略和回滚，Pod 是具体运行的实例。",
    },
    {
        "id": 5,
        "question": "React 中 useMemo 和 useCallback 的区别？",
        "answer": "useMemo 缓存计算结果，useCallback 缓存函数引用。两者的核心区别在于返回值不同：useMemo 返回计算后的值，useCallback 返回记忆化的回调函数本身。此外 useMemo 还可以用于优化子组件中引用类型的比较。",
        "contexts": [
            "useMemo 返回记忆化的计算结果，依赖数组变化时重新计算。",
            "useCallback 是 useMemo 的特例，专门记忆化回调函数，避免子组件不必要的重渲染。",
        ],
        "ground_truth": "useMemo 缓存计算值，useCallback 缓存函数引用。useCallback 本质是 useMemo(() => fn, deps) 的语法糖。",
    },
    # ── 幻觉用例（answer 编造了 contexts 中没有的内容）──
    {
        "id": 6,
        "question": "Redis 的持久化策略有哪些？",
        "answer": "Redis 主要有两种持久化策略：RDB（快照）和 AOF（追加日志）。RDB 定期生成内存快照，AOF 记录每次写操作。此外 Redis 7.0 引入了 RDB-AOF 混合模式，结合了两者优点，并且支持基于 CPU 核数的自动分片持久化以提升大内存场景的写入效率。",
        "contexts": [
            "RDB 持久化：在指定时间间隔内生成数据集的时间点快照并保存到磁盘。",
            "AOF 持久化：记录每次写操作命令，重启时重放以重建数据。",
            "Redis 4.0 起支持混合持久化，RDB 做全量 + AOF 做增量。",
        ],
        "ground_truth": "Redis 持久化包括 RDB（快照）、AOF（追加日志）和混合模式，分别以不同方式保证数据恢复。",
    },
    {
        "id": 7,
        "question": "TCP 三次握手的过程是怎样的？",
        "answer": "TCP 三次握手：客户端发送 SYN=1, seq=x；服务端回复 SYN=1, ACK=1, seq=y, ack=x+1；客户端再发送 ACK=1, seq=x+1, ack=y+1。之后连接建立，双方开始用 HTTP/3 协议传输数据。",
        "contexts": [
            "第一次握手：客户端发送 SYN 包（SYN=1, seq=x），进入 SYN_SENT 状态。",
            "第二次握手：服务端回复 SYN+ACK 包（SYN=1, ACK=1, seq=y, ack=x+1），进入 SYN_RCVD 状态。",
            "第三次握手：客户端发送 ACK 包（ACK=1, seq=x+1, ack=y+1），双方进入 ESTABLISHED 状态。",
        ],
        "ground_truth": "TCP 三次握手通过 SYN → SYN+ACK → ACK 三个报文建立可靠连接，各自进入 ESTABLISHED 状态。",
    },
    # ── 检索无关（contexts 与问题不匹配）──
    {
        "id": 8,
        "question": "Docker 镜像和容器的区别是什么？",
        "answer": "镜像是一个只读的模板，容器是镜像的运行实例。镜像就像类的定义，容器就像类的实例对象。每个容器有自己的可写层，不同容器之间相互隔离。",
        "contexts": [
            "VMware 虚拟机通过 Hypervisor 在物理硬件上运行多个操作系统实例。",
            "虚拟机快照可以保存系统状态并在需要时恢复到该状态。",
        ],
        "ground_truth": "镜像是只读模板，容器是镜像的运行实例，容器有可写层且相互隔离。",
    },
    {
        "id": 9,
        "question": "GraphQL 和 REST API 的主要区别？",
        "answer": "GraphQL 允许客户端精确指定需要的数据字段，避免 REST 的过度获取（over-fetching）和获取不足（under-fetching）问题。GraphQL 只有一个端点，而 REST 通常有多个端点对应不同资源。",
        "contexts": [
            "Python FastAPI 是一个高性能的 REST API 框架，支持自动 Swagger 文档生成。",
            "Django REST Framework 提供了序列化器、视图集和认证等组件用于构建 RESTful API。",
        ],
        "ground_truth": "GraphQL 单一端点、按需获取、无需多轮请求；REST 多端点、固定数据结构、可能过度或不足获取。",
    },
    # ── 边界用例 ──
    {
        "id": 10,
        "question": "如何在 Linux 下查找大于 100MB 的文件？",
        "answer": "使用 `find / -type f -size +100M` 命令可以查找大于 100MB 的文件。",
        "contexts": [
            "find 命令用于在指定目录下搜索文件。-type f 限定搜索文件类型，-size 按文件大小过滤。",
            "du -sh * 可以查看当前目录下各文件和文件夹的大小。",
        ],
        "ground_truth": "find / -type f -size +100M 可查找大于 100MB 的文件。",
    },
    # ── 答案过度精简（丢失关键信息）──
    {
        "id": 11,
        "question": "什么是 CAP 定理？在分布式系统中如何取舍？",
        "answer": "CAP 定理说分布式系统无法同时满足一致性、可用性和分区容错性这三个属性。实际系统中通常根据业务需求在一致性和可用性之间做取舍。",
        "contexts": [
            "CAP 定理（Brewer's Theorem）指出：在存在网络分区的情况下，分布式系统只能在一致性（Consistency）和可用性（Availability）之间选择其一。分区容错性（Partition Tolerance）是必须接受的前提。",
            "CP 系统：发生网络分区时选择一致性，暂时拒绝服务（如 ZooKeeper）。AP 系统：发生网络分区时选择可用性，允许短暂不一致（如 Cassandra）。",
            "在现实中，很多系统采用最终一致性（Eventual Consistency）的折衷方案。",
        ],
        "ground_truth": "CAP 定理指分布式系统无法同时满足一致性、可用性和分区容错性；由于网络分区不可避免，系统必须在 C 和 A 之间权衡。",
    },
    # ── 较好案例 ──
    {
        "id": 12,
        "question": "Git merge 和 Git rebase 有什么区别？",
        "answer": "merge 创建一个合并提交保留完整的分支历史，rebase 将当前分支的提交移到目标分支顶部使历史变成线性。merge 适合公共分支，rebase 适合整理个人分支的提交历史。",
        "contexts": [
            "git merge 会创建一个新的合并提交（merge commit），保留两个分支的历史记录。",
            "git rebase 将当前分支的提交重新应用到目标分支的最新提交之上，产生线性历史。",
            "黄金法则：不要在公共分支上使用 rebase，因为它会改写提交历史。",
        ],
        "ground_truth": "merge 保留完整分支历史并产生合并提交；rebase 使历史线性但不保留分支分叉，不应在公共分支使用。",
    },
    # ── 检索部分遗漏（关键 context 缺失）──
    {
        "id": 13,
        "question": "Go 语言的 goroutine 和操作系统线程有什么区别？",
        "answer": "goroutine 是 Go 运行时管理的轻量级协程，初始栈只有 2KB 且可动态伸缩。goroutine 通过 GMP 调度模型与操作系统线程映射，一个线程上可运行多个 goroutine。",
        "contexts": [
            "goroutine 是 Go 语言中的轻量级执行单元，由 Go 运行时调度管理。",
        ],
        "ground_truth": "goroutine 是用户态轻量线程，初始栈小（2KB），由 Go 运行时 GMP 模型调度，一个 OS 线程上可运行数千 goroutine；OS 线程由内核调度，栈空间大（1-8MB），切换成本高。",
    },
    # ── 检索丰富但答案编造细节 ──
    {
        "id": 14,
        "question": "SQL 中 LEFT JOIN 和 INNER JOIN 的区别？",
        "answer": "INNER JOIN 只返回两表匹配的行，LEFT JOIN 返回左表所有行，右表无匹配时填充 NULL。在 MySQL 8.0.33 中，LEFT JOIN 比 INNER JOIN 平均慢 15%，因为需要扫描更多行。",
        "contexts": [
            "INNER JOIN（内连接）只返回两个表中满足连接条件的行。",
            "LEFT JOIN（左外连接）返回左表所有行，右表不匹配的列值以 NULL 填充。",
            "RIGHT JOIN（右外连接）与 LEFT JOIN 对称，返回右表所有行。",
        ],
        "ground_truth": "INNER JOIN 只返回匹配行，LEFT JOIN 返回左表全部行且不匹配的右表列填 NULL。",
    },
    # ── 完美案例 ──
    {
        "id": 15,
        "question": "什么是向量数据库？它和传统数据库有什么不同？",
        "answer": "向量数据库专为高维向量数据的相似性检索而设计。它存储的是 embedding 向量，查询方式是基于向量间距离（如余弦相似度、欧氏距离）的近似最近邻（ANN）搜索，而非传统数据库的精确匹配或范围查询。代表产品有 Milvus、Pinecone、Weaviate 等。",
        "contexts": [
            "向量数据库存储高维向量数据，通过 ANN（近似最近邻）算法实现相似性检索。",
            "与传统数据库不同，向量数据库的查询基于向量间的距离度量（余弦相似度、欧氏距离、点积），而非精确字段匹配。",
            "主流向量数据库包括 Milvus（开源）、Pinecone（云服务）、Weaviate、Qdrant、Chroma 等。",
            "向量数据库广泛应用于 RAG、推荐系统、图像搜索、语义搜索等 AI 场景。",
        ],
        "ground_truth": "向量数据库存储 embedding 向量，通过 ANN 向量距离搜索实现相似性检索，不同于传统数据库的精确匹配查询，常用于 RAG 和 AI 场景。",
    },
]
