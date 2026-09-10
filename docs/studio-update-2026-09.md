# 图片任务与编辑器更新

本次修复不需要新增数据库迁移。需同时更新 Web、Worker 和 Scheduler，前端已包含在应用镜像内。

## 修复内容

- 修正积分流水与任务关联的写入顺序。原来扣费流水尚未 INSERT，任务就先 UPDATE 关联流水，在 PostgreSQL 外键检查下失败，并被错误地提示为“任务状态已变化”。现在先写入流水，再建立关联；全过程仍处于同一事务内，失败整体回滚。
- 扣费、退款、管理员调账共用修正后的流水写入逻辑。数据库异常返回真实的保存失败提示和请求编号，服务端记录 SQLSTATE 与约束名称。
- 同一报价重试使用相同幂等标识，网络异常保留报价与输入；任务结束后读取真实余额。
- 结果发布稍晚于任务完成时继续获取结果；预览链接自动续期，支持手动重新载入。
- 重做工作台和编辑器布局：参数独立滚动、提交区固定可见、画布展开、原图对比、结果继续编辑、移动端布局。
- 局部修复/文字修正支持画笔涂抹、撤销与清空，导出与原图同尺寸的透明 PNG 遮罩。更换来源图时清除旧遮罩。

## 服务器更新

先提交并推送本地改动，等待 GitHub 的 **Quality gates** 和 **Publish production image** 成功。尚未发布新镜像时，仅在服务器拉取 `latest` 不会得到本次修复。

在服务器当前项目目录执行以下命令。保持原有 `.env` 与密钥；建议把 `IMAGE_TAG` 设为这次发布的 `sha-完整40位提交ID`，以便确认版本和回滚。

```bash
# 把下面的示例标签替换为实际发布的镜像标签
export IMAGE_TAG=sha-实际完整40位提交ID
docker compose -f docker-compose.server.yml pull web worker scheduler migrate
docker compose -f docker-compose.server.yml run --rm migrate
docker compose -f docker-compose.server.yml up -d web worker scheduler
docker compose -f docker-compose.server.yml ps
```

确认版本后，将同一个 `IMAGE_TAG` 写回服务器 `.env`，避免以后在新终端重建容器时退回旧标签。

更新后浏览器强制刷新一次。分别验证一项本地处理（如颜色处理）与一项 AI 操作，检查报价、积分、任务完成、结果预览与下载。实际 AI 调用还依赖服务器的 Sub2API/R2 配置与服务可用性。

如仍报错，保留页面的请求编号，并在服务器查看对应日志：

```bash
docker compose -f docker-compose.server.yml logs --since=10m web worker scheduler
```

`IMAGE_JOB_SAVE_FAILED` 是数据库保存失败，日志包含约束名称；上游执行失败会显示具体原因，并通过正常失败流程退款。不要清空数据库或更换应用加密密钥来处理此问题。

## 验证范围

本地完整质量检查：134 项后端测试、16 项前端测试通过，类型检查与生产构建通过。任务、积分、素材测试启用 SQLite 外键约束，覆盖重复提交、事务回滚、免费任务、退款与结果素材。

新增两项 PostgreSQL 集成用例，在 CI 的迁移后测试数据库运行真实的任务扣费、重复提交和退款。开发机未安装 PostgreSQL/Docker，本地未执行这两项外部集成测试。浏览器验证使用临时 SQLite、内存对象存储和模拟 AI 上游，不访问生产数据。
