# 全屋定制AI销售助手 - 话术引擎 v2.0

一个纯Python实现的全屋定制客服话术引擎，基于关键词匹配 + 模板渲染，支持8大类50+场景的自动回复。

---

## 目录结构

```
customer_service/
├── engine.py            # 核心引擎（关键词匹配 + 工艺判断 + 议价状态机 + LLM兜底）
├── templates.yaml       # 话术模板库（8大类约100个模板）
├── shop_config.yaml     # 商家配置（店铺信息/工艺能力/付款方式/工期/品牌信任）
├── test_cli.py          # 命令行测试工具
└── README.md            # 本文件
```

---

## 快速开始

### 环境要求
- Python 3.8+
- 依赖：`pyyaml`、`jinja2`、`requests`（LLM兜底用）

```bash
pip install pyyaml jinja2 requests
```

### 交互模式
```bash
cd customer_service
python test_cli.py
```

输入客户的话，回车得到AI回复。支持指令：
- `status` — 查看当前对话状态
- `clear` — 清空上下文
- `quit` — 退出

### 自动测试
```bash
python test_cli.py --test
```

一键运行所有自测用例，覆盖8大类场景、工艺判断、议价状态机、变量渲染等。

---

## 架构说明

### 回复主流程
```
用户输入 → 高频关键词命中 → 工艺问题判断 → 议价状态机 → LLM意图识别 → 模板渲染 → 返回
```

优先级从高到低，匹配到就直接返回，不继续往下走。

### 核心模块

#### 1. 关键词前置命中（hot_questions）
- 8大类50+场景，约100个话术模板
- 纯关键词匹配，不走LLM，响应快、精准、零成本
- 每个场景2-4个模板，随机抽取避免重复

#### 2. 工艺能力判断
- 6个特殊工艺场景（圆弧、榻榻米、开放式厨房、异形、非标、自带材料）
- 根据 `shop_config.yaml` 中 `process_capability` 配置自动判断
- 能做 → 走"能做话术"（自信 + 案例 + 留钩子）
- 不能做 → 走"替代方案话术"（不说做不了，说建议用XX替代 + 留钩子）

#### 3. 议价多轮状态机
- 四步走架构：**摸底 → 分档（小/中/大） → 升级**
- 同一客户连续问价格，逐步深入，不重复
- 单值大小靠关键词判断（全屋=大单，1-2个=小单，3-5个=中单，判断不出默认中单）
- 状态随对话结束自动重置

#### 4. LLM意图识别（兜底）
- 关键词没命中时，调用 DeepSeek 做4分类（consult/complain/bargain/chat）
- 失败自动降级为 chat
- 对应 top-level 的4组兜底模板

---

## 8大类话术场景一览

| 分类 | 场景数 | 模板数 | 说明 |
|------|--------|--------|------|
| **议价体系** | 6 | 20+ | 摸底/小单/中单/大单/升级/定金 |
| **材料环保** | 7 | 16 | 环保等级/E0vsENF/检测报告/入住/味道/封边/真假 |
| **计价方式** | 7 | 15 | 投影vs展开/包含什么/抽屉加价/别家便宜/隐形消费/免费服务/报价差 |
| **品牌信任** | 5 | 11 | 没听过/比大牌/小作坊/跑路/朋友出问题 |
| **工期安装** | 7 | 14 | 总工期/加急/延期/工人/弄坏/垃圾/补件 |
| **售后质保** | 7 | 14 | 质保几年/五金坏/开胶/找谁/过质保/店搬走/终身维护 |
| **决策犹豫** | 6 | 12 | 再看看/商量/没装修/等活动/方案/不急 |
| **特殊工艺** | 6 | 24 | 圆弧/榻榻米/开放式厨房/异形/非标/自带材料（能做+不能做各2个） |

**总计：约50个场景，约120个模板**

---

## 配置说明（shop_config.yaml）

### 店铺基础信息
```yaml
shop_name: 佳美全屋定制
boss_name: 王师傅
years_in_business: 15
city: 杭州
shop_location: 杭州市余杭区建材市场三楼A区
wechat_id: "13800138000"   # 留资用微信号
```

### 产品硬参数
```yaml
board_brand: 兔宝宝颗粒板ENF级
edge_band: PUR封边
hardware_brand: DTC五金
eco_level: ENF级
```

### 工艺能力（process_capability）
12项工艺开关，全部 bool 类型。客户问对应的工艺时，自动判断走"能做"还是"替代方案"话术。

```yaml
process_capability:
  arc_curved: true         # 圆弧工艺
  door_wall_cabinet: true  # 门墙柜一体
  hole_board: true         # 洞洞板
  wall_panel: true         # 护墙板
  interior_door: true      # 室内门
  glass_door: true         # 玻璃门
  aluminum_frame: true     # 铝框门
  ox_horn_handle: false    # 牛角拉手
  hidden_handle: true      # 隐藏拉手
  rebound_device: true     # 反弹器
  tatami: true             # 榻榻米
  open_kitchen: true       # 开放式厨房
```

### 付款方式（payment_terms）
```yaml
payment_terms:
  deposit: "50%"              # 定金比例
  production_pay: "45%"       # 生产前付款比例
  final_pay: "5%"             # 安装验收后尾款比例
  deposit_amount: "500"       # 测量定金金额
  refundable: true            # 定金是否可退
  warranty_years: "5"         # 质保年限
  after_sales_phone: "400-888-6666"
```

### 工期配置（production_cycle）
```yaml
production_cycle:
  design_days: "7"        # 设计周期
  production_days: "30-45"  # 生产周期
  install_days: "1-2"     # 安装周期
  rush_available: true    # 是否可以加急
  rush_days: "25"         # 加急最快天数
  repair_days: "7"        # 补件周期
```

### 品牌信任（trust_points）
```yaml
trust_points:
  factory_location: "余杭区仓前工业园"
  factory_size: "占地10亩，3条生产线"
  community_cases: ["未来科技城", "良渚文化村", "闲林山水", "西溪湿地"]
  boss_is_local: true
```

### 随机池配置
- `concessions` — 让利池（送东西，每次抽1个）
- `urgency_factors` — 紧迫感池（每次抽1个）
- `selling_points` — 卖点池（每次抽1个）
- `lead_hooks` — 留资钩子池（每次抽1个，自动带微信号）

---

## 模板变量说明

模板用 jinja2 语法，变量名和配置一一对应：

| 变量 | 说明 | 示例 |
|------|------|------|
| `{{shop_name}}` | 店铺名称 | 佳美全屋定制 |
| `{{boss_name}}` | 老板称呼 | 王师傅 |
| `{{wechat_id}}` | 微信号 | 13800138000 |
| `{{board_brand}}` | 板材品牌 | 兔宝宝颗粒板ENF级 |
| `{{eco_level}}` | 环保等级 | ENF级 |
| `{{edge_band}}` | 封边工艺 | PUR封边 |
| `{{hardware_brand}}` | 五金品牌 | DTC五金 |
| `{{concessions}}` | 随机让利 | 送5平米背景墙 |
| `{{urgency}}` | 随机紧迫感 | 板材马上要涨价 |
| `{{selling_point}}` | 随机卖点 | 五金终身质保... |
| `{{hook}}` | 随机留资钩子 | 加我微信138...，我把报价单发你 |
| `{{payment_terms.xxx}}` | 付款方式嵌套 | {{payment_terms.deposit}} |
| `{{production_cycle.xxx}}` | 工期嵌套 | {{production_cycle.production_days}} |
| `{{trust_points.xxx}}` | 信任点嵌套 | {{trust_points.factory_location}} |

**bool 变量用条件判断：**
```jinja2
{% if payment_terms.refundable %}不满意全额退{% else %}定了就不退了哦{% endif %}
```

---

## 扩展话术

### 新增一个场景
在 `templates.yaml` 的 `hot_questions` 数组里加一条：

```yaml
- category: 你的分类名
  keywords:
    - 关键词1
    - 关键词2
  templates:
    - "模板1 {{变量名}}"
    - "模板2 {{变量名}}"
```

### 新增工艺类场景
加 `process_key` 字段，模板分成 `yes_templates` 和 `no_templates`：

```yaml
- category: process_xxx
  process_key: xxx   # 对应 shop_config 里 process_capability 的 key
  keywords:
    - 关键词1
  yes_templates:
    - "能做的话术..."
  no_templates:
    - "不能做的替代方案话术..."
```

---

## 版本历史

### v2.0（2026-07-31）— 话术体系大扩展
- 新增 shop_config 4大配置块：工艺能力/付款方式/工期/品牌信任
- 话术模板从4大类15个扩展到8大类50+场景约120个模板
- 新增工艺能力判断（能做/不能做自动切换话术）
- 新增议价多轮状态机（摸底→分档→升级）
- 留资钩子自动带微信号
- 全部走关键词前置命中，省LLM成本

### v1.1
- 关键词前置命中 + LLM四分类兜底
- jinja2 模板渲染 + 随机池抽取
- 3轮上下文记忆 + 对话结束判断
