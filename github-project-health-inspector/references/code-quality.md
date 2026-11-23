# 代码质量与 Bug 稽查（code-quality）

目标不是"列出所有可疑行"，而是**找出会让用户遇到故障的那几处**，并给出可验证的修复。

---

## 1. 问题分类（报告里必须分开）

| 类型 | 定义 | 报告标记 |
|---|---|---|
| 已确认缺陷 | 读代码即可证明会出错（除零、必现空指针、逻辑反了） | `[确认]` |
| 疑似缺陷 | 依赖运行条件，需构造输入才能触发 | `[疑似]` |
| 设计缺陷 | 代码能跑但结构会持续产生 bug | `[设计]` |
| 改进建议 | 风格/可读性/微优化 | `[建议]` |

禁止把 `[建议]` 写成 `[确认]` 来凑问题数量。

---

## 2. 通用缺陷模式

**空值与边界**
- 可能为 `None/null/nil` 的返回值直接取属性（`resp.json()["data"]["items"]`）
- 空数组取 `[0]`、字典取不存在的 key
- 分页最后一页、`len==0`、单元素集合
- 整数除法/取模为 0、负数索引、超大数溢出
- 时间边界：时区混用、跨月/跨年、夏令时、`datetime.now()` 无 tz

**异常处理**
- `except: pass` / `catch (e) {}` 吞掉错误 → 故障静默
- `except Exception` 包住整个业务流程，导致部分失败被当成成功
- 捕获后只 `print`，不记录上下文、不重试、不向上抛
- `finally` 里 `return` 覆盖异常
- 资源清理放在 `try` 里而非 `finally`/`with`

**资源与泄漏**
- 文件/连接/游标/lock 未关闭（Python 缺 `with`，Java 缺 try-with-resources，Go 缺 `defer Close`）
- 循环内创建连接或线程池
- 缓存无上限（dict 只增不减）、事件监听器注册后不解绑
- 前端：`setInterval` 未清、`addEventListener` 未移除、订阅未取消

**并发**
- 共享可变状态无锁（全局 dict/list 在多线程/协程中写）
- 检查后使用（check-then-act）：`if not exists: create` 无唯一约束
- 死锁：两把锁顺序不一致
- 异步里做阻塞 IO（`requests` 在 async 函数里）
- Go：goroutine 泄漏（channel 无缓冲且无人读）、`for` 里 `defer` 堆积

**数据处理**
- 编码假设（默认 utf-8 读 GBK 文件）
- 浮点比较 `==`、金额用 float
- 字符串拼接 SQL/路径/正则未转义
- 正则回溯爆炸（`(a+)+`）
- 大文件一次性读入内存

---

## 3. 语言专项

**Python**
- 可变默认参数 `def f(a=[])`
- 类属性当实例状态
- 闭包延迟绑定（循环里创建 lambda）
- `is` 比较字面量
- 导入时执行副作用（模块级 IO/网络）
- `requests` 无 timeout（会永久挂起）
- 用 `assert` 做生产校验（`-O` 会被移除）

**JS/TS**
- `==` 隐式转换
- 未 `await` 的 Promise（错误变成 unhandled rejection）
- `this` 丢失（回调传方法）
- 数组 `forEach` 里 `await` 不生效
- `JSON.parse` 无 try
- 正则 `g` 标志导致 `test` 状态残留
- `any` 掩盖类型错误

**Go**
- `err` 被忽略或覆盖
- 循环变量捕获（1.22 之前）
- map 并发读写 panic
- `time.After` 在循环里泄漏定时器
- slice 共享底层数组导致意外修改

**Java/C#**
- `equals`/`hashCode` 不成对
- 线程池未 shutdown
- `SimpleDateFormat` 非线程安全
- `async void`
- `IDisposable` 未 dispose

**C/C++**
- `strcpy/strcat/sprintf` 无边界
- `new[]` 配 `delete`
- 迭代器失效
- 未初始化指针

---

## 4. 度量口径（写进报告时要说明方法）

| 指标 | 方法 | 判读 |
|---|---|---|
| 函数长度 | Python 用 `ast` 的 `end_lineno - lineno`；JS 用大括号配对（启发式） | >80 行需关注，>200 行必拆分 |
| 圈复杂度 | `if/for/while/except/with/and/or` 计数 +1 | >15 高风险，>30 必重构 |
| 重复代码 | 归一化后 6 行滑窗哈希 | >5% 说明抽象缺失；样例必须给文件行号 |
| 裸 except | AST `ExceptHandler.type is None` | 1 个可接受，>5 个是习惯问题 |
| TODO 密度 | 正则计数 | 结合"存在时长"看：老 TODO 是技术债 |
| 测试比例 | 测试文件数 / 代码文件数 | <20% 说明回归风险高（不是覆盖率！） |

**必须声明**：这些是静态度量，**不等于**测试覆盖率，也**不等于**运行时性能。

---

## 5. 输出模板（每个问题）

```
#### [P1] 未处理的空响应导致首页 500
- 类型：[确认]
- 位置：`src/api/user.ts:88` → `getUser()`；调用点 `src/pages/Home.tsx:34`
- 证据：
  ```ts
  const data = await res.json();      // 后端 204 时返回 undefined
  return data.profile.name;           // TypeError
  ```
- 触发条件：用户未完善资料时接口返回 `{profile: null}`
- 影响：新用户首次进入首页白屏，错误上报中占比 12%（若可查 issue 请附链接）
- 修复：
  ```ts
  const profile = data?.profile ?? DEFAULT_PROFILE;
  ```
- 优先级理由：影响所有新用户的主路径
- 验证方式：`npm test -- Home.test.tsx`，补充 `profile: null` 用例
```

---

## 6. 覆盖度自检

交付前问自己：
1. 我读过承担核心业务的目录了吗？（不是只看 `utils/`）
2. 我给出的每个 `[确认]` 都能指出具体行号吗？
3. 有没有"疑似"被我写成了"确认"？
4. 有没有把生成代码/vendored 代码当成手写代码评价？
5. 我是否说明了"未读哪些模块"？
