# 03 RBAC 角色权限

## 1. 功能范围

RBAC 控制“用户能执行什么管理动作”，资源归属控制“用户能操作哪条数据”。会员等级只影响额度、价格和功能权益，不能授予管理员权限。

内置角色：

- `user`：普通工作台权限。
- `operator`：用户、任务和资产运营处理，不可改价格和密钥。
- `finance`：会员、积分和价格只读/调整权限。
- `auditor`：只读后台和审计日志。
- `super_admin`：拥有全部权限，但仍看不到密钥明文。

## 2. 权限点

```text
studio.use
assets.read_own / assets.write_own / assets.delete_own
tasks.read_own / tasks.create / tasks.cancel_own
points.read_own
membership.read_own

admin.dashboard.read
users.read / users.manage
roles.read / roles.manage
memberships.read / memberships.manage
points.read / points.adjust
pricing.read / pricing.manage
tasks.read / tasks.manage
assets.read / assets.manage
config.read / config.manage / config.test
audit.read / audit.export
security.events.read / security.policies.manage
system.health.read / system.diagnostics.read / system.maintenance.execute
```

## 3. 权限矩阵

| 模块 | user | operator | finance | auditor | super_admin |
|---|---:|---:|---:|---:|---:|
| 使用图片工具 | 是 | 是 | 是 | 可选 | 是 |
| 查看本人数据 | 是 | 是 | 是 | 是 | 是 |
| 用户管理 | 否 | 是 | 只读 | 只读 | 是 |
| 会员调整 | 否 | 可选 | 是 | 只读 | 是 |
| 积分调整 | 否 | 否 | 是 | 只读 | 是 |
| 价格管理 | 否 | 否 | 是 | 只读 | 是 |
| 任务处置 | 否 | 是 | 只读 | 只读 | 是 |
| 密钥覆盖/测试 | 否 | 否 | 否 | 否 | 是 |
| 角色管理 | 否 | 否 | 否 | 否 | 是 |

实际授权以权限点为准，表格只是内置角色默认模板。

## 4. 数据库结构

```text
roles
- id uuid PK
- code varchar UNIQUE
- name varchar
- description varchar
- is_system boolean
- created_at timestamptz
- updated_at timestamptz

permissions
- id uuid PK
- code varchar UNIQUE
- module varchar
- description varchar

role_permissions
- role_id uuid FK roles
- permission_id uuid FK permissions
- PRIMARY KEY (role_id, permission_id)

user_roles
- user_id uuid FK users
- role_id uuid FK roles
- assigned_by uuid FK users
- expires_at timestamptz NULL
- created_at timestamptz
- PRIMARY KEY (user_id, role_id)
```

权限定义随代码迁移发布，管理员可组合角色，但不能创建代码未识别的权限点。

## 5. 接口设计

```text
GET  /api/v1/admin/permissions
GET  /api/v1/admin/roles
POST /api/v1/admin/roles
PATCH /api/v1/admin/roles/{role_id}
PUT  /api/v1/admin/roles/{role_id}/permissions
GET  /api/v1/admin/users/{user_id}/roles
PUT  /api/v1/admin/users/{user_id}/roles
```

所有业务查询必须先按 `owner_id = current_user.id` 限定，再读取数据。后台跨用户访问必须使用 `/admin` 仓储方法并验证对应权限，禁止通过传入 `user_id` 绕过归属过滤。

## 6. 缓存与失效

- 权限可缓存到 Redis 1-5 分钟，缓存键包含用户 `permission_version`。
- 角色变更时增加用户权限版本并删除缓存。
- 高风险接口（角色、积分、密钥）可直接读取数据库或使用极短缓存。

## 7. 验收标准

- 对每个权限点至少有允许与拒绝两组集成测试。
- 替换 URL 中的用户/任务/资产 ID 不能读取他人数据。
- 修改角色后旧会话无需重新登录即可在短时间内失效。
- `super_admin` 无法通过任何 GET 接口取得密钥明文。

## 8. 实施结果

- [x] 建立 34 个代码定义的权限点，以及 `user/operator/finance/auditor/super_admin` 五个系统角色的精确 P0 权限种子。
- [x] 新增 `permissions` 与 `role_permissions`，沿用 02 已建立的 `roles/user_roles`；`20260908_0003` 迁移可兼容已有角色并为存量账号补齐永久 `user` 角色。
- [x] 会话鉴权每次请求通过 `user_roles -> roles -> role_permissions -> permissions` 读取有效权限，已移除超级管理员通配符和硬编码角色判断。
- [x] 当前不启用 Redis 权限缓存，角色与权限修改在下一请求立即生效；所有变更仍增加受影响用户的 `permission_version`，保留后续缓存键与主动失效契约。
- [x] 支持权限目录、角色列表、自定义角色创建/修改、角色权限整集替换，以及用户角色查询/整集替换接口。
- [x] 权限只能从代码发布的目录中选择；系统角色不可通过 API 修改，自定义角色代码不可重复。
- [x] 所有账号必须保留永不过期的 `user` 基础角色；禁止移除或禁用最后一个有效 `super_admin`，并通过角色事务锁避免并发绕过；过期角色不会参与授权。
- [x] 用户读取与管理权限已拆分为 `users.read/users.manage`，系统 Worker 状态改用冻结矩阵中的 `system.health.read`。
- [x] 角色创建、角色修改、权限替换和用户角色替换均在同一事务写入安全审计事件，载荷不包含密钥明文。
- [x] 已通过五类内置角色的精确矩阵测试，并对全部 34 个权限点逐项验证允许与拒绝；同时覆盖权限即时生效、角色过期和跨用户会话 ID 越权拒绝。

资源归属仓储将在文档 06、07 建立任务和素材表时按 `owner_id = current_user.id` 固化；当前生产模式不加载旧同步工作流，现有跨用户会话操作已按所有者过滤并返回不可枚举的 `404`。
