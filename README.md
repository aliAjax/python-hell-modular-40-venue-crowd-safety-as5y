# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务。

### 区域拆并

管理员可在同一场馆内拆分或合并区域。拆分会把原区域标记为`superseded`并创建两个新区域；合并会把两个旧区域标记为`superseded`并创建一个新区域。旧区域不会删除，`GET /api/entities/<旧编号>`和关联审计仍可用于历史查询。

拆分请求示例：

```json
{
  "zone_id": "north",
  "expected_version": 3,
  "new_zones": [
    {"id": "north-a", "name": "North A", "capacity": 60, "current_occupancy": 30},
    {"id": "north-b", "name": "North B", "capacity": 40, "current_occupancy": 20}
  ],
  "gate_targets": {"gate-1": ["north-a", "north-b"]},
  "task_targets": {"task-1": "north-b"}
}
```

未显式指定的入场口默认同时关联两个新区域；未完成任务必须通过`task_targets`或`task_zone_id`指定归属。两个新区域容量之和、人数之和必须分别等于原区域。

合并请求示例：

```json
{
  "zone_ids": ["north-a", "north-b"],
  "expected_versions": {"north-a": 2, "north-b": 1},
  "target_id": "north",
  "name": "North Stand"
}
```

合并后容量和人数为两个旧区域之和，关联入场口改挂新区域，未完成现场任务统一改挂新区域；已结束事件保留原区域编号。区域正在疏散或存在未结束事件（`reported`、`triaged`、`dispatched`、`reopened`）时拒绝拆并。整次拆并在一个SQLite事务中提交，写入失败会回滚。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/zone/split`（管理员拆分区域）
- `POST /api/zone/merge`（管理员合并同场馆两个区域）
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
