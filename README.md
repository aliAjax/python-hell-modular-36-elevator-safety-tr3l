# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/shifts`：创建调度员值班班次。
- `POST /api/shifts/<id>/handover`：交班时生成/读取交接单并冻结当前未闭环报警清单。
- `POST /api/alarms/<id>/takeover`：当班调度员接管一条报警，body需包含`shift_id`。
- `POST /api/handovers/<id>/takeover`：下一班批量接管交接单内全部未闭环报警，body需包含`shift_id`。
- `POST /api/shifts/<id>/end`：尝试收班；仍无人接管的报警会生成升级记录。
- `GET /api/alarm-takeovers`：查询接管记录，可用`?alarm_id=`、`?shift_id=`、`?status=`过滤。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。`supervisor`角色可作为上级查看或处理异常升级。## 核心流程

创建设备后安排检验、维保和困人报警；调度员值班后可接管本区域报警。交班时生成交接单，下一班必须接管全部未闭环报警；全部接管后交接单完成，上一班自动结束。报警解除或误报会失效当前接管并立即重算交接单数量；收班时仍未被下一班接管的报警会生成上级升级记录。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 调度员值班后接管同区域未解除报警；数据库唯一活动接管保证一条报警同时只有一个当班接管人。
- 并发接管由SQLite `BEGIN IMMEDIATE`事务和部分唯一索引仲裁，先提交者成功，后提交者得到409冲突。
- 交接单记录未闭环总数、已接管数、待接管数；只有待接管数为0，上一班才能结束。
- 报警解除或标记误报时，活动接管变为`invalidated`并同步重算交接单。
- 收班时仍没有下一班接管的报警生成`escalation`开放记录提醒上级；后续被接管或关闭会自动解除升级。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
