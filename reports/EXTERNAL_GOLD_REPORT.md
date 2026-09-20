# 外部专家金标对照（CUAD）

生成时间：2026-09-20T14:11:16.770418+00:00
评分模型：**deepseek-flash**
条款检索：keyword×8（请求 hybrid，实际已降级——嵌入模型不可达时引擎回落到 keyword；运行日志中 huggingface.co 连接被重置 WinError 10054/10060）
（单条规则最多可见 6000 字符）
语料：CUAD v1（510 份真实商业协议，律师标注）中取 8 份
映射规则：7 条 contract-compliance 规则；CUAD 只标「有没有、在哪一段」，不含严重度，因此本报告不评判红黄绿档位

## 这轮能得出什么结论

**能成立的**：
- 「缺失」这个判断引擎一次都没做对：专家认定不存在的 9 个规则位，9 个都给出了引用（缺失一致 0/9）。
  集中在 知识产权归属×4、终止条款×2、违约补偿条款×2、定价清晰度×1。机制成因见下文「检索兜底」一条。
- 检索预算是硬瓶颈：≤24k 字符的定位召回 22.2%，>24k 字符跌到 8.6%，长度差 2.6 倍。
- 存在一致 72.3%（34/47）——13 次专家明明找到了条款、引擎却称文档没有：
  适用法律×6、审计权×4、终止条款×2、违约补偿条款×1。
- 真实花费 $0.2382（120 次调用、581,748 tokens，token_source=api）。

**不能成立的**：
- 不能把 12.8% 定位召回读成「引擎定位能力差」。本轮是中文规则 × 英文语料 × keyword 降级检索三者叠加，
  跨语言词面重叠本身接近随机，这个数衡量的是「这套规则配这份语料」的下限，不是引擎在真实评审中的上限。
- 时延列不可用：2,173,554 ms 里绝大部分是嵌入模型联网重试的等待。

## 总体

| 指标 | 数值 | 命中/总数 | 含义 |
|---|---:|---|---|
| 定位召回 | 12.8% | 15/117 | 专家标出的片段中，被我们引用命中的比例 |
| 存在即不称缺 | 72.3% | 34/47 | 专家找到该条款时，我们没有声称文档缺失 |
| 缺失判定一致 | 0.0% | 0/9 | 专家没找到时，我们也如实报告缺失 |
| 无中生有引用 | 9 | — | 专家认定不存在，我们却引用了内容（幻觉/过度报警信号） |
| 检索受限的文档 | 8/8 | 正文 > 6000 字符 | 单条规则看不全整份合同 |
| 定位召回 · >24k 字符 | 8.6% | 7/81（4 份） | 长度对定位能力的直接影响 |
| 定位召回 · ≤24k 字符 | 22.2% | 8/36（4 份） | 长度对定位能力的直接影响 |
| 时延 | 2173554 ms | 8 docs | 引擎端到端 |
| 成本 | $0.2382 | 120 次调用 | tokens 581748（api） |

## 逐份

| 合同 | 字符 | 规则 | 定位召回 | 存在一致 | 缺失一致 | 无中生有 | 引用 | 判定数 | 时延 | 预估 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IVILLAGEINC_03_17_1999-EX-10.16-SP | 23723 | 7 | 18.2% | 5/6 | 0/1 | 1 | 11 | 15 | 532131 ms | $0.0239 |
| AMERICASSHOPPINGMALLINC_12_10_1999 | 19351 | 7 | 42.9% | 6/6 | 0/1 | 1 | 12 | 15 | 475221 ms | $0.0301 |
| InnerscopeHearingTechnologiesInc_2 | 22396 | 7 | 11.1% | 5/5 | 0/2 | 2 | 12 | 15 | 327121 ms | $0.0256 |
| ScansourceInc_20190822_10-K_EX-10. | 13330 | 7 | 22.2% | 3/5 | 0/2 | 2 | 8 | 15 | 167240 ms | $0.0336 |
| KitovPharmaLtd_20190326_20-F_EX-4. | 57436 | 7 | 8.3% | 4/7 | 0/0 | 0 | 8 | 15 | 126762 ms | $0.0302 |
| NETGEAR,INC_04_21_2003-EX-10.16-DI | 41501 | 7 | 15.4% | 3/6 | 0/1 | 1 | 7 | 15 | 114640 ms | $0.0257 |
| VISIUMTECHNOLOGIES,INC_10_20_2004- | 49784 | 7 | 8.7% | 4/6 | 0/1 | 1 | 8 | 15 | 179306 ms | $0.0369 |
| LegacyEducationAllianceInc_2020033 | 46255 | 7 | 0.0% | 4/6 | 0/1 | 1 | 10 | 15 | 251133 ms | $0.0323 |

## 逐条不一致明细

- **无中生有** IVILLAGEINC_03_17_1999-EX-10.16-SPONSORS · 终止条款（Termination For Convenience/Notice Period To Terminate Renewal）：引擎判 green、引用 3 条、命中专家片段 0/0
- **谎称缺失** IVILLAGEINC_03_17_1999-EX-10.16-SPONSORS · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1
- **无中生有** AMERICASSHOPPINGMALLINC_12_10_1999-EX-10 · 知识产权归属（Ip Ownership Assignment/Joint Ip Ownership）：引擎判 red、引用 2 条、命中专家片段 0/0
- **无中生有** InnerscopeHearingTechnologiesInc_2018110 · 终止条款（Termination For Convenience/Notice Period To Terminate Renewal）：引擎判 amber、引用 2 条、命中专家片段 0/0
- **无中生有** InnerscopeHearingTechnologiesInc_2018110 · 违约补偿条款（Liquidated Damages/Warranty Duration）：引擎判 red、引用 2 条、命中专家片段 0/0
- **无中生有** ScansourceInc_20190822_10-K_EX-10.38_117 · 定价清晰度（Price Restrictions/Minimum Commitment/Revenue/Profit Sharing）：引擎判 red、引用 3 条、命中专家片段 0/0
- **无中生有** ScansourceInc_20190822_10-K_EX-10.38_117 · 知识产权归属（Ip Ownership Assignment/Joint Ip Ownership）：引擎判 green、引用 2 条、命中专家片段 0/0
- **谎称缺失** ScansourceInc_20190822_10-K_EX-10.38_117 · 终止条款（Termination For Convenience/Notice Period To Terminate Renewal）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** ScansourceInc_20190822_10-K_EX-10.38_117 · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** KitovPharmaLtd_20190326_20-F_EX-4.15_115 · 审计权（Audit Rights）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** KitovPharmaLtd_20190326_20-F_EX-4.15_115 · 终止条款（Termination For Convenience/Notice Period To Terminate Renewal）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** KitovPharmaLtd_20190326_20-F_EX-4.15_115 · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBU · 审计权（Audit Rights）：引擎判 red、引用 0 条、命中专家片段 0/2
- **无中生有** NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBU · 知识产权归属（Ip Ownership Assignment/Joint Ip Ownership）：引擎判 red、引用 1 条、命中专家片段 0/0
- **谎称缺失** NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBU · 违约补偿条款（Liquidated Damages/Warranty Duration）：引擎判 red、引用 0 条、命中专家片段 0/8
- **谎称缺失** NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBU · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** VISIUMTECHNOLOGIES,INC_10_20_2004-EX-10. · 审计权（Audit Rights）：引擎判 red、引用 0 条、命中专家片段 0/2
- **无中生有** VISIUMTECHNOLOGIES,INC_10_20_2004-EX-10. · 知识产权归属（Ip Ownership Assignment/Joint Ip Ownership）：引擎判 green、引用 2 条、命中专家片段 0/0
- **谎称缺失** VISIUMTECHNOLOGIES,INC_10_20_2004-EX-10. · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1
- **谎称缺失** LegacyEducationAllianceInc_20200330_10-K · 审计权（Audit Rights）：引擎判 red、引用 0 条、命中专家片段 0/1
- **无中生有** LegacyEducationAllianceInc_20200330_10-K · 违约补偿条款（Liquidated Damages/Warranty Duration）：引擎判 red、引用 2 条、命中专家片段 0/0
- **谎称缺失** LegacyEducationAllianceInc_20200330_10-K · 适用法律（Governing Law）：引擎判 red、引用 0 条、命中专家片段 0/1

## 说明与局限

- **语言与剧本不匹配**：我们的规则是中文合同规则，CUAD 全是英文 SEC 协议。keyword 检索的中英跨语言
  重叠接近随机，因此定位召回衡量的是「这套规则配这份语料」，不能直接读成引擎在真实评审中的定位能力。
- **检索兜底会制造引用（本轮成立，现已修）**：`select_clauses` 当时无论如何至少返回 3 段条款（哪怕相关度为
  0），模型只能从填充段落里挑，所以「专家没找到、引擎却引用了」是结构性结果，缺失一致率 0% 首先指向这个
  下限设计而非幻觉。该兜底已在检索层修复中移除，因此本轮的 9 次无中生有读数混入了已知的机制噪声，
  需要重跑才能当作幻觉率来读。
- CUAD 的类别与我们的规则不是同一套语义，映射表见 `eval_cases/cuad_rule_map.json`，其中「定价清晰度」「违约补偿条款」是近似映射，读数时应降权看待。
- 定位命中按归一化后的最长公共片段判定（≥60 字符），因为 CUAD 的偏移基于另一条文本提取链。
- 一份合同里同一规则可能对应多个 CUAD 类别（例如责任上限既有 Cap 也有 Uncapped），命中任一专家片段即算该规则定位成功。
- CUAD 仓库未附 LICENSE 文件，本语料仅限内部评测，不得再分发或并入发布物。
- 本脚本强制 `EMAIL_ENABLED=false`，评测运行不会给团队外发通知邮件。
